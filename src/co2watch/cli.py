"""Command line entry point: ``python -m co2watch <command>``.

Commands
  simulate     write synthetic readings + labels (CSV) and optionally load a DB
  import-csv   backfill readings/context from CSV into the DB
  export-csv   export readings + context from the DB to CSV
  calibrate    compute daily anchors and the self-calibration fit
  mark-calibration  record a fluid change / sensor move
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from . import db
from .calibration import drift_report, extract_daily_anchors, fit_calibration
from .features import add_basic_features, to_regular_grid
from .simulator import default_scenario, simulate


def _cmd_simulate(a):
    res = simulate(days=a.days, seed=a.seed, start_local=a.start,
                   anomalies=default_scenario(a.days) if a.anomalies else [])
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    r = res.readings.reset_index()
    r.to_csv(out / "sim_readings.csv", index=False)
    res.labels.to_csv(out / "sim_labels.csv", index=False)
    res.truth.reset_index().to_csv(out / "sim_truth.csv", index=False)
    print(f"wrote {len(r)} readings, {len(res.labels)} labels to {out}")
    if a.db:
        con = db.connect(a.db)
        n = db.import_csv(con, out / "sim_readings.csv", source="sensor")
        for _, lab in res.labels.iterrows():
            if lab["category"] == "calibration_event":
                db.add_calibration_event(con, lab["start"], "fluid_change", "simulated")
        print(f"loaded {n} rows into {a.db}")


def _cmd_import(a):
    con = db.connect(a.db)
    print(f"inserted {db.import_csv(con, a.csv)} readings")


def _cmd_export(a):
    con = db.connect(a.db)
    print(f"exported {db.export_csv(con, a.csv, a.start, a.end)} rows to {a.csv}")


def _load_features(con) -> pd.DataFrame:
    raw = db.read_readings(con)
    grid = to_regular_grid(raw)
    for e in ("co2_on", "airstone_on", "light_on"):
        s = db.context_on_grid(con, e, grid.index)
        if s.notna().any():
            grid[e] = s
    return add_basic_features(grid)


def _cmd_calibrate(a):
    con = db.connect(a.db)
    df = _load_features(con)
    anchors = extract_daily_anchors(df)
    events = db.calibration_events(con)
    fit = fit_calibration(anchors, calibration_events=events)
    drift = drift_report(anchors, calibration_events=events)
    plat = anchors.loc[anchors["valid"], "plateau_hue"].tail(7).median()
    night = anchors.loc[anchors["valid"], "night_hue"].tail(7).median()
    print(anchors.tail(a.show).to_string(index=False))
    print(json.dumps({"fit": fit.to_dict(), "drift": drift}, indent=2, default=str))
    if pd.notna(plat):
        print(f"typical daily peak: {fit.describe(plat)}")
        print(f"typical night low : {fit.describe(night)}")


def _cmd_mark(a):
    con = db.connect(a.db)
    ts = pd.Timestamp(a.when, tz="Europe/Amsterdam") if a.when else pd.Timestamp.now(tz="UTC")
    db.add_calibration_event(con, ts, a.kind, a.note)
    print(f"calibration event {a.kind} at {ts.tz_convert('UTC')}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="co2watch")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("simulate")
    s.add_argument("--days", type=int, default=42)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--start", default="2026-10-12", help="local start date")
    s.add_argument("--no-anomalies", dest="anomalies", action="store_false")
    s.add_argument("--out", default="data/sim")
    s.add_argument("--db", default=None)
    s.set_defaults(func=_cmd_simulate)

    s = sub.add_parser("import-csv"); s.add_argument("csv"); s.add_argument("--db", required=True)
    s.set_defaults(func=_cmd_import)

    s = sub.add_parser("export-csv"); s.add_argument("csv"); s.add_argument("--db", required=True)
    s.add_argument("--start"); s.add_argument("--end")
    s.set_defaults(func=_cmd_export)

    s = sub.add_parser("calibrate"); s.add_argument("--db", required=True)
    s.add_argument("--show", type=int, default=7)
    s.set_defaults(func=_cmd_calibrate)

    s = sub.add_parser("mark-calibration"); s.add_argument("--db", required=True)
    s.add_argument("--kind", default="fluid_change",
                   choices=["fluid_change", "sensor_moved", "other"])
    s.add_argument("--when", help="local time, default now"); s.add_argument("--note")
    s.set_defaults(func=_cmd_mark)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
