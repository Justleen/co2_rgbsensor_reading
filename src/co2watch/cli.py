"""Command line entry point: ``co2watch <command>`` (or ``python -m co2watch``).

Readings come from VictoriaMetrics (``--vm URL``) or a CSV file (``--csv PATH``).
Labels, calibration events and the alert log go to SQLite (``--db PATH``).

Commands
  simulate          write synthetic two-frame readings + labels to CSV
                    (optionally import them into VM with source="sim")
  backfill          import a CSV into VM under a source label
  export-csv        export readings + context from VM to CSV
  calibrate         daily anchors, self-calibration fit and drift report
  label             store a label for a time window (normal, real_problem, ...)
  mark-calibration  record a fluid change / sensor move
  score             M2 detection (rules, baseline, daily summaries); logs alerts
  alerts            list logged alerts
  archive           nightly long-term store: hourly/daily aggregates and raw
                    windows around alerts and labels
  evaluate          recall / false alarms on simulated anomalies
  init-db           create the SQLite schema
  backup            consistent copy of the SQLite file, keeping the newest N
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from . import db, vm
from .calibration import drift_report, extract_daily_anchors, fit_calibration
from .config import BAND_NAMES, TZ
from .features import prepare
from .simulator import default_scenario, simulate


def _local(s: str | None, default=None) -> pd.Timestamp | None:
    if s is None:
        return default
    t = pd.Timestamp(s)
    return t.tz_localize(TZ) if t.tzinfo is None else t


def _load(a) -> pd.DataFrame:
    if a.csv:
        return vm.frames_from_csv(a.csv)
    end = _local(a.end, pd.Timestamp.now(tz="UTC"))
    start = _local(a.start, end - pd.Timedelta(days=a.days))
    return vm.read_all(a.vm, start, end, source=a.source)


def _cmd_simulate(a):
    res = simulate(days=a.days, seed=a.seed, start_local=a.start,
                   anomalies=default_scenario(a.days) if a.anomalies else [])
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    vm.frames_to_csv(res.readings, out / "sim_readings.csv")
    res.labels.to_csv(out / "sim_labels.csv", index=False)
    res.truth.reset_index().to_csv(out / "sim_truth.csv", index=False)
    print(f"wrote {len(res.readings)} readings, {len(res.labels)} labels to {out}")
    if a.vm:
        n = vm.import_frames(a.vm, res.readings, source="sim")
        print(f"imported {n} samples into {a.vm} with source=\"sim\"")
    if a.db:
        con = db.connect(a.db)
        for _, lab in res.labels.iterrows():
            if lab["category"] == "calibration_event":
                db.add_calibration_event(con, lab["start"], "fluid_change", "simulated")


def _cmd_backfill(a):
    df = vm.frames_from_csv(a.csv_in)
    print(f"imported {vm.import_frames(a.vm, df, source=a.as_source)} samples "
          f"with source=\"{a.as_source}\"")


def _cmd_export(a):
    df = _load(a)
    print(f"exported {vm.frames_to_csv(df, a.out)} rows to {a.out}")


def _cmd_calibrate(a):
    df = prepare(_load(a), a.saturation)
    anchors = extract_daily_anchors(df)
    con = db.connect(a.db) if a.db else None
    events = db.calibration_events(con) if con else []

    def hue_at(s, e):
        return float(df.loc[s:e, "hue"].median())

    eye = db.eye_checks(con, hue_at) if con else None
    fit = fit_calibration(anchors, eye, calibration_events=events)
    drift = drift_report(anchors, calibration_events=events)
    valid = anchors[anchors["valid"]]
    if con is not None and a.save:
        print(f"saved {db.save_anchors(con, anchors)} daily anchors to {a.db}")
    print(anchors.tail(a.show).to_string(index=False))
    print(json.dumps({"fit": fit.to_dict(), "drift": drift}, indent=2, default=str))
    if len(valid):
        print(f"typical daily peak: {fit.describe(valid['plateau_hue'].tail(7).median())}")
        print(f"typical night low : {fit.describe(valid['night_hue'].tail(7).median())}")
    return (f"{len(valid)} of {len(anchors)} days valid; fit {fit.status} "
            f"(a={fit.a:.1f}, b={fit.b:.2f}); drift {drift.get('pattern', 'n/a')}")


def _cmd_score(a):
    from .detect import DetectConfig, detect
    con = db.connect(a.db)
    raw = _load(a)
    if a.now:  # replay: pretend later data does not exist yet
        raw = raw[raw.index < _local(a.now)]
    df = prepare(raw, a.saturation)
    if df.empty:
        print("no readings in the selected window")
        return "no readings in the selected window"
    events = db.calibration_events(con)
    det = detect(df, DetectConfig(threshold=a.threshold), events)
    now = _local(a.now) if a.now else df.index[-1] + pd.Timedelta(minutes=5)
    since = now - pd.Timedelta(hours=a.since_hours)
    new = 0
    for _, al in det.alerts.iterrows():
        if al["end"] < since or al["known_at"] > now:
            continue
        i, created = db.upsert_alert(con, al["start"], al["end"], al["score"], al["kind"],
                                     al["reason"], model_version="m2-rules-baseline",
                                     details=al["details"])
        new += created
        print(f"{'NEW ' if created else 'open'} #{i} [{al['kind']}] score {al['score']:.2f}: "
              f"{al['reason']}")
    last = det.scores.iloc[-1]
    msg = (f"scored {len(df)} slots up to {df.index[-1].tz_convert(TZ):%Y-%m-%d %H:%M}; "
           f"latest score {last['score']:.2f}; {new} new alert(s)")
    print(msg)
    return msg


def _cmd_archive(a):
    from . import archive
    from .detect import DetectConfig, detect
    from .features import FRAME_COLS
    con = db.connect(a.db)
    raw = _load(a)
    now = _local(a.now) if a.now else pd.Timestamp.now(tz="UTC")
    raw = raw[raw.index < now]
    df = prepare(raw, a.saturation)
    events = db.calibration_events(con)
    score = detect(df, DetectConfig(), events).scores["score"] if len(df) else None
    first = (now.tz_convert(TZ).normalize() - pd.Timedelta(days=a.agg_days))
    keep = archive.complete_days(df[df.index >= first], now)
    nh = archive.save_hourly(con, archive.hourly(keep, score)) if len(keep) else 0
    nd = archive.save_daily(con, archive.daily(keep, db.read_alerts(con), events, score)) \
        if len(keep) else 0
    print(f"aggregates: {nh} hours, {nd} days (complete local days since {first:%Y-%m-%d})")
    nw = 0
    for source, s, e in archive.event_windows(con, a.pad_hours):
        if e > now:
            continue  # window not finished yet; archived on a later run
        if a.vm:
            frames = vm.read_frames(a.vm, s, e, source=a.source)
        else:
            frames = raw.loc[s:e, FRAME_COLS]
        frames = frames.dropna(how="all")
        if frames.empty:
            print(f"raw window {source}: no data left in the source (older than retention?)")
            continue
        n = archive.save_raw_window(con, source, frames)
        nw += 1
        print(f"raw window {source}: {n} samples {s.tz_convert(TZ):%Y-%m-%d %H:%M} .. "
              f"{e.tz_convert(TZ):%Y-%m-%d %H:%M}")
    return f"{nh} hours, {nd} days aggregated; {nw} raw window(s) stored"


def _cmd_alerts(a):
    con = db.connect(a.db)
    al = db.read_alerts(con).tail(a.last)
    if al.empty:
        print("no alerts logged")
    for _, r in al.iterrows():
        print(f"#{r['id']} {r['start'].tz_convert(TZ):%Y-%m-%d %H:%M} [{r['kind']}] "
              f"score {r['score']:.2f}\n    {r['reason']}")


def _cmd_evaluate(a):
    from .evaluate import evaluate, to_markdown
    r = evaluate(seeds=tuple(range(1, a.seeds + 1)), days=a.days)
    md = to_markdown(r)
    print(md)
    if a.out:
        Path(a.out).write_text(md + "\n")
        print(f"written to {a.out}")


def _cmd_label(a):
    con = db.connect(a.db)
    i = db.add_label(con, _local(a.start), _local(a.end), a.label, band=a.band, note=a.note)
    print(f"label #{i} stored")


def _cmd_backup(a):
    con = db.connect(a.db)
    dest = db.backup(con, a.dir, a.keep)
    print(f"backup written to {dest}")
    return f"wrote {Path(dest).name}"


def _cmd_init(a):
    db.connect(a.db).close()
    print(f"database ready at {a.db}")


def _cmd_mark(a):
    con = db.connect(a.db)
    ts = _local(a.when, pd.Timestamp.now(tz="UTC"))
    db.add_calibration_event(con, ts, a.kind, a.note)
    print(f"calibration event {a.kind} at {ts.tz_convert('UTC')}")


def _cmd_serve(a):
    from .web import serve
    serve(a.host, a.port, a.db, vm_url=a.vm, source=a.source, csv=a.csv,
          cache_s=a.cache_seconds, history_days=a.days)


# Scheduled commands whose runs are recorded in job_runs (shown on the status page).
JOBS = ("calibrate", "score", "archive", "backup")


def _run_job(a):
    con = db.connect(a.db)
    run = db.job_started(con, a.cmd)
    try:
        summary = a.func(a)
    except BaseException as e:  # record the failure, then fail as before
        db.job_finished(con, run, False, f"{type(e).__name__}: {e}")
        raise
    db.job_finished(con, run, True, summary if isinstance(summary, str) else None)


def _source_args(p, need_db=False):
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--vm", help="VictoriaMetrics base URL, e.g. http://victoria:8428")
    g.add_argument("--csv", help="read readings from this CSV instead of VM")
    p.add_argument("--source", default=None,
                   help='VM source label to read (default: scraped data; "sim" for simulated)')
    p.add_argument("--start", help="local time, default: --days before --end")
    p.add_argument("--end", help="local time, default: now")
    p.add_argument("--days", type=float, default=21)
    p.add_argument("--saturation", type=float, default=None,
                   help="channel saturation limit, (256-ATIME)*1024 capped at 65535")
    p.add_argument("--db", required=need_db)


def main(argv=None):
    p = argparse.ArgumentParser(prog="co2watch")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("simulate")
    s.add_argument("--days", type=int, default=42)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--start", default="2026-10-12", help="local start date")
    s.add_argument("--no-anomalies", dest="anomalies", action="store_false")
    s.add_argument("--out", default="data/sim")
    s.add_argument("--vm", default=None, help='also import into VM with source="sim"')
    s.add_argument("--db", default=None, help="record simulated fluid changes here")
    s.set_defaults(func=_cmd_simulate)

    s = sub.add_parser("backfill")
    s.add_argument("csv_in"); s.add_argument("--vm", required=True)
    s.add_argument("--as-source", required=True, help='e.g. "backfill"; never empty')
    s.set_defaults(func=_cmd_backfill)

    s = sub.add_parser("export-csv"); s.add_argument("out"); _source_args(s)
    s.set_defaults(func=_cmd_export)

    s = sub.add_parser("calibrate"); _source_args(s)
    s.add_argument("--show", type=int, default=7)
    s.add_argument("--save", action="store_true", help="store daily anchors in --db")
    s.set_defaults(func=_cmd_calibrate)

    s = sub.add_parser("score", help="run M2 detection and log alerts")
    _source_args(s, need_db=True)
    s.set_defaults(days=16)
    s.add_argument("--threshold", type=float, default=1.0)
    s.add_argument("--since-hours", type=float, default=2.0,
                   help="only log alerts that were active in this many recent hours")
    s.add_argument("--now", help="local time to treat as now (replays, tests)")
    s.set_defaults(func=_cmd_score)

    s = sub.add_parser("archive", help="nightly: aggregates + raw windows around events to SQLite")
    _source_args(s, need_db=True)
    s.set_defaults(days=16)
    s.add_argument("--agg-days", type=int, default=3,
                   help="(re)write aggregates for this many complete local days")
    s.add_argument("--pad-hours", type=float, default=6.0,
                   help="raw data kept around each alert/label")
    s.add_argument("--now", help="local time to treat as now (replays, tests)")
    s.set_defaults(func=_cmd_archive)

    s = sub.add_parser("alerts", help="list logged alerts"); s.add_argument("--db", required=True)
    s.add_argument("--last", type=int, default=20)
    s.set_defaults(func=_cmd_alerts)

    s = sub.add_parser("evaluate", help="score detectors on simulated anomalies")
    s.add_argument("--seeds", type=int, default=5); s.add_argument("--days", type=int, default=42)
    s.add_argument("--out", help="write the markdown table here")
    s.set_defaults(func=_cmd_evaluate)

    s = sub.add_parser("init-db"); s.add_argument("--db", required=True)
    s.set_defaults(func=_cmd_init)

    s = sub.add_parser("backup"); s.add_argument("--db", required=True)
    s.add_argument("--dir", required=True); s.add_argument("--keep", type=int, default=14)
    s.set_defaults(func=_cmd_backup)

    s = sub.add_parser("label"); s.add_argument("--db", required=True)
    s.add_argument("label", choices=db.LABELS)
    s.add_argument("--start", required=True, help="local time")
    s.add_argument("--end", required=True, help="local time")
    s.add_argument("--band", choices=BAND_NAMES, help="for eye_check: colour seen by eye")
    s.add_argument("--note")
    s.set_defaults(func=_cmd_label)

    s = sub.add_parser("mark-calibration"); s.add_argument("--db", required=True)
    s.add_argument("--kind", default="fluid_change",
                   choices=["fluid_change", "sensor_moved", "other"])
    s.add_argument("--when", help="local time, default now"); s.add_argument("--note")
    s.set_defaults(func=_cmd_mark)

    s = sub.add_parser("serve", help="small status web page (alerts, normal band, job runs)")
    s.add_argument("--db", required=True)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--vm", help="VictoriaMetrics base URL; without it the page shows SQLite only")
    g.add_argument("--csv", help="read readings from this CSV instead (local mock)")
    s.add_argument("--source", default=None, help='VM source label ("sim" for simulated)')
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--days", type=float, default=16,
                   help="history read for the normal band (needs 14+ days)")
    s.add_argument("--cache-seconds", type=float, default=120,
                   help="recompute the page data at most this often")
    s.set_defaults(func=_cmd_serve)

    a = p.parse_args(argv)
    if a.cmd in JOBS and getattr(a, "db", None):
        _run_job(a)
    else:
        a.func(a)


if __name__ == "__main__":
    main()
