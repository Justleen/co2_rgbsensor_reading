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
    print(anchors.tail(a.show).to_string(index=False))
    print(json.dumps({"fit": fit.to_dict(), "drift": drift}, indent=2, default=str))
    if len(valid):
        print(f"typical daily peak: {fit.describe(valid['plateau_hue'].tail(7).median())}")
        print(f"typical night low : {fit.describe(valid['night_hue'].tail(7).median())}")


def _cmd_label(a):
    con = db.connect(a.db)
    i = db.add_label(con, _local(a.start), _local(a.end), a.label, band=a.band, note=a.note)
    print(f"label #{i} stored")


def _cmd_mark(a):
    con = db.connect(a.db)
    ts = _local(a.when, pd.Timestamp.now(tz="UTC"))
    db.add_calibration_event(con, ts, a.kind, a.note)
    print(f"calibration event {a.kind} at {ts.tz_convert('UTC')}")


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
    s.set_defaults(func=_cmd_calibrate)

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

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
