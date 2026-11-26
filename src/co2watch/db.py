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
    # Several pods on the same node may open the file (scorer, CronJobs,
    # toolbox). SQLite's file locks serialise the writers; wait instead of
    # failing when another one holds the lock. Never put this file on NFS/SMB.
    con = sqlite3.connect(str(path), timeout=30.0)
    con.execute("PRAGMA busy_timeout = 30000")
    con.executescript(SCHEMA)
    return con


def save_anchors(con, anchors: pd.DataFrame) -> int:
    """Upsert daily anchors (output of calibration.extract_daily_anchors)."""
    def _ms_or_none(t):
        return None if t is None or pd.isna(t) else ms(t)

    rows = [(str(r.local_date), _f(r.night_hue), _ms_or_none(r.night_ts), int(r.night_n),
             _f(r.plateau_hue), _ms_or_none(r.plateau_ts), int(r.plateau_n), _f(r.clear_night),
             _f(r.coverage), int(bool(r.valid))) for r in anchors.itertuples(index=False)]
    con.executemany("INSERT OR REPLACE INTO anchors VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return len(rows)


def _f(x):
    return None if x is None or pd.isna(x) else float(x)


def backup(con, directory: str | Path, keep: int = 14) -> Path:
    """Consistent online copy (SQLite backup API), keeping the newest ``keep``."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
    dest = d / f"co2watch-{stamp}.sqlite"
    with sqlite3.connect(str(dest)) as out:
        con.backup(out)
    old = sorted(d.glob("co2watch-*.sqlite"))[:-keep] if keep > 0 else []
    for f in old:
        f.unlink()
    return dest


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


def upsert_alert(con, start, end, score: float, kind: str, reason: str,
                 model_version: str | None = None, details: dict | None = None,
                 merge_gap_min: int = 30) -> tuple[int, bool]:
    """Extend an existing alert of the same kind that overlaps (or ends within
    ``merge_gap_min`` of) this one, else insert. Returns (id, created)."""
    s_ms, e_ms = ms(start), ms(end)
    row = con.execute(
        "SELECT id, start_ms, end_ms, score FROM alerts WHERE kind = ? AND end_ms >= ? "
        "AND start_ms <= ? ORDER BY start_ms DESC LIMIT 1",
        (kind, s_ms - merge_gap_min * 60_000, e_ms)).fetchone()
    if row:
        i, s0, e0, sc0 = row
        con.execute("UPDATE alerts SET start_ms=?, end_ms=?, score=?, reason=?, details_json=? "
                    "WHERE id=?", (min(s0, s_ms), max(e0 or e_ms, e_ms), max(sc0 or 0, score),
                                   reason, json.dumps(details, default=str) if details else None,
                                   i))
        con.commit()
        return int(i), False
    return log_alert(con, start, end, score, kind, reason, model_version, details), True


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
