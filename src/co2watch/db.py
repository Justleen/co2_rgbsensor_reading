"""SQLite for the small, hand-made or model-made records only.

Raw sensor data lives in VictoriaMetrics (see ``vm.py``). This file holds
labels, calibration events, derived daily anchors, the alert log and the model
registry. Timestamps are integer epoch milliseconds (UTC).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

LABELS = ("normal", "real_problem", "sensor_issue", "ignore", "eye_check")

SCHEMA = """
PRAGMA journal_mode = WAL;

-- Your feedback on a time window, entered with the CLI or the notebook.
-- eye_check labels carry the colour band seen by eye in `band`.
CREATE TABLE IF NOT EXISTS labels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ms    INTEGER NOT NULL,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,
    label         TEXT    NOT NULL,
    band          TEXT,
    alert_id      INTEGER,                -- alert that prompted the label, if any
    model_version TEXT,
    features_json TEXT,                   -- feature snapshot at labelling time
    note          TEXT
);

-- Fluid replaced, sensor moved, etc. Starts a fresh baseline.
CREATE TABLE IF NOT EXISTS calibration_events (
    ts_ms  INTEGER PRIMARY KEY,
    kind   TEXT NOT NULL,                 -- fluid_change | sensor_moved | other
    note   TEXT
);

-- Daily self-calibration anchors (derived from VM data, recomputable cache).
CREATE TABLE IF NOT EXISTS anchors (
    local_date   TEXT PRIMARY KEY,
    night_hue    REAL, night_ms INTEGER, night_n INTEGER,
    plateau_hue  REAL, plateau_ms INTEGER, plateau_n INTEGER,
    clear_night  REAL, coverage REAL, valid INTEGER
);

-- Alert log. No notifications for now: alerts are only written here.
CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER,
    score         REAL,
    kind          TEXT,                   -- which detector / rule fired
    reason        TEXT,                   -- templated plain-language reason
    model_version TEXT,
    details_json  TEXT
);

-- Model registry: every trained model, with metrics and an active flag.
CREATE TABLE IF NOT EXISTS models (
    version      TEXT PRIMARY KEY,
    created_ms   INTEGER NOT NULL,
    kind         TEXT NOT NULL,           -- calibration | baseline | iforest | ...
    path         TEXT,
    params_json  TEXT,
    metrics_json TEXT,
    active       INTEGER NOT NULL DEFAULT 0
);
"""


def ms(ts) -> int:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(round(t.timestamp() * 1000))


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    return con


def add_label(con, start, end, label: str, band: str | None = None, note: str | None = None,
              alert_id=None, model_version=None, features: dict | None = None) -> int:
    if label not in LABELS:
        raise ValueError(f"label must be one of {LABELS}")
    if label == "eye_check" and not band:
        raise ValueError("eye_check needs a band (blue, blue-green, teal-green, green, lime)")
    cur = con.execute(
        "INSERT INTO labels (created_ms,start_ms,end_ms,label,band,alert_id,model_version,"
        "features_json,note) VALUES (?,?,?,?,?,?,?,?,?)",
        (ms(pd.Timestamp.now(tz="UTC")), ms(start), ms(end), label, band, alert_id,
         model_version, json.dumps(features) if features else None, note))
    con.commit()
    return int(cur.lastrowid)


def read_labels(con, label: str | None = None) -> pd.DataFrame:
    q, args = "SELECT * FROM labels", []
    if label:
        q += " WHERE label = ?"; args.append(label)
    df = pd.read_sql_query(q + " ORDER BY start_ms", con, params=args)
    for c in ("created_ms", "start_ms", "end_ms"):
        df[c.replace("_ms", "")] = pd.to_datetime(df.pop(c), unit="ms", utc=True)
    return df


def add_calibration_event(con, ts, kind: str = "fluid_change", note: str | None = None) -> None:
    con.execute("INSERT OR REPLACE INTO calibration_events VALUES (?,?,?)", (ms(ts), kind, note))
    con.commit()


def calibration_events(con) -> list[pd.Timestamp]:
    rows = con.execute("SELECT ts_ms FROM calibration_events ORDER BY ts_ms").fetchall()
    return [pd.Timestamp(r[0], unit="ms", tz="UTC") for r in rows]


def log_alert(con, start, end, score: float, kind: str, reason: str,
              model_version: str | None = None, details: dict | None = None) -> int:
    cur = con.execute(
        "INSERT INTO alerts (start_ms,end_ms,score,kind,reason,model_version,details_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (ms(start), ms(end) if end is not None else None, score, kind, reason, model_version,
         json.dumps(details, default=str) if details else None))
    con.commit()
    return int(cur.lastrowid)


def read_alerts(con) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM alerts ORDER BY start_ms", con)
    for c in ("start_ms", "end_ms"):
        df[c.replace("_ms", "")] = pd.to_datetime(df.pop(c), unit="ms", utc=True)
    return df


def eye_checks(con, hue_at) -> pd.DataFrame:
    """Eye-check labels joined with the sensor hue at that time.

    ``hue_at`` is a callable (start, end) -> median sensor hue, so this module
    does not need to know where the readings come from.
    """
    lab = read_labels(con, "eye_check")
    if lab.empty:
        return pd.DataFrame(columns=["ts", "sensor_hue", "band"])
    lab["sensor_hue"] = [hue_at(s, e) for s, e in zip(lab["start"], lab["end"])]
    return lab.rename(columns={"start": "ts"})[["ts", "sensor_hue", "band"]]
