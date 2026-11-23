"""SQLite storage. One file holds raw readings, context, labels, calibration
and model metadata. Raw readings are never modified; derived values are
recomputed from them.

Timestamps are stored as integer epoch milliseconds (UTC) so there is no
timezone ambiguity in the database at all.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

SCHEMA = """
PRAGMA journal_mode = WAL;

-- Raw sensor readings exactly as received (counts with fixed gain/integration).
CREATE TABLE IF NOT EXISTS readings (
    ts_ms      INTEGER NOT NULL,          -- UTC epoch ms
    source     TEXT    NOT NULL DEFAULT 'sensor',
    red        REAL, green REAL, blue REAL, clear REAL,
    hue_device REAL,                      -- hue if the ESP publishes one (optional)
    raw_json   TEXT,                      -- original payload
    PRIMARY KEY (ts_ms, source)
);

-- Context entity changes (light, co2_on, airstone_on, temperature, ph, ...).
-- Stored as state changes, not resampled.
CREATE TABLE IF NOT EXISTS context (
    ts_ms   INTEGER NOT NULL,
    entity  TEXT    NOT NULL,
    value   TEXT,
    PRIMARY KEY (ts_ms, entity)
);

-- User feedback on a time window. label in: normal, real_problem,
-- sensor_issue, ignore, eye_check (with band in note), synthetic.
CREATE TABLE IF NOT EXISTS labels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ms    INTEGER NOT NULL,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,
    label         TEXT    NOT NULL,
    event_id      INTEGER,                -- anomaly event that triggered the question
    model_version TEXT,
    features_json TEXT,                   -- features snapshot at labelling time
    note          TEXT
);

-- Fluid replaced, sensor moved, etc. Starts a fresh baseline.
CREATE TABLE IF NOT EXISTS calibration_events (
    ts_ms  INTEGER PRIMARY KEY,
    kind   TEXT NOT NULL,                 -- fluid_change | sensor_moved | other
    note   TEXT
);

-- Daily self-calibration anchors (derived, recomputable).
CREATE TABLE IF NOT EXISTS anchors (
    local_date   TEXT PRIMARY KEY,
    night_hue    REAL, night_ms INTEGER, night_n INTEGER,
    plateau_hue  REAL, plateau_ms INTEGER, plateau_n INTEGER,
    clear_night  REAL, coverage REAL, valid INTEGER
);

-- Fired anomalies (step 3 fills this).
CREATE TABLE IF NOT EXISTS anomaly_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER,
    score         REAL,
    kind          TEXT,
    model_version TEXT,
    details_json  TEXT,
    explanation   TEXT
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


def _ms(ts) -> int:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(t.tz_convert("UTC").value // 1_000_000)


def _ts(ms) -> pd.Series:
    return pd.to_datetime(ms, unit="ms", utc=True)


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    return con


def insert_readings(con: sqlite3.Connection, df: pd.DataFrame, source: str = "sensor") -> int:
    """Insert readings (tz-aware index or ``ts`` column). Existing rows with
    the same timestamp are kept (raw data is append-only)."""
    d = df.reset_index() if "ts" not in df.columns else df.copy()
    rows = [
        (_ms(r.ts), source, _f(r.red), _f(r.green), _f(r.blue), _f(r.clear),
         _f(getattr(r, "hue_device", None)), getattr(r, "raw_json", None))
        for r in d.itertuples(index=False)
    ]
    cur = con.executemany(
        "INSERT OR IGNORE INTO readings VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return cur.rowcount


def _f(x):
    return None if x is None or pd.isna(x) else float(x)


def insert_context_changes(con: sqlite3.Connection, series: pd.Series, entity: str) -> int:
    """Store only the changes of a context series (tz-aware index)."""
    s = series.dropna()
    changed = s.ne(s.shift())
    rows = [(_ms(t), entity, str(v)) for t, v in s[changed].items()]
    cur = con.executemany("INSERT OR IGNORE INTO context VALUES (?,?,?)", rows)
    con.commit()
    return cur.rowcount


def read_readings(con: sqlite3.Connection, start=None, end=None,
                  source: str = "sensor") -> pd.DataFrame:
    q = "SELECT ts_ms, red, green, blue, clear FROM readings WHERE source = ?"
    args: list = [source]
    if start is not None:
        q += " AND ts_ms >= ?"; args.append(_ms(start))
    if end is not None:
        q += " AND ts_ms < ?"; args.append(_ms(end))
    df = pd.read_sql_query(q + " ORDER BY ts_ms", con, params=args)
    df["ts"] = _ts(df.pop("ts_ms"))
    return df.set_index("ts")


def read_context(con: sqlite3.Connection, entity: str) -> pd.Series:
    df = pd.read_sql_query("SELECT ts_ms, value FROM context WHERE entity = ? ORDER BY ts_ms",
                           con, params=[entity])
    s = pd.Series(df["value"].to_numpy(), index=_ts(df["ts_ms"]), name=entity)
    low = s.str.lower()
    if low.isin(["true", "false", "on", "off", "1", "0"]).all():
        return low.isin(["true", "on", "1"])
    return pd.to_numeric(s, errors="coerce")


def context_on_grid(con: sqlite3.Connection, entity: str, index: pd.DatetimeIndex) -> pd.Series:
    """Forward-fill a context entity onto a reading grid."""
    s = read_context(con, entity)
    if s.empty:
        return pd.Series(pd.NA, index=index, name=entity)
    return s.reindex(s.index.union(index)).ffill().reindex(index)


def add_label(con, start, end, label: str, note: str | None = None, event_id=None,
              model_version=None, features: dict | None = None) -> int:
    cur = con.execute(
        "INSERT INTO labels (created_ms,start_ms,end_ms,label,event_id,model_version,"
        "features_json,note) VALUES (?,?,?,?,?,?,?,?)",
        (_ms(pd.Timestamp.now(tz="UTC")), _ms(start), _ms(end), label, event_id, model_version,
         json.dumps(features) if features else None, note))
    con.commit()
    return int(cur.lastrowid)


def add_calibration_event(con, ts, kind: str = "fluid_change", note: str | None = None) -> None:
    con.execute("INSERT OR REPLACE INTO calibration_events VALUES (?,?,?)", (_ms(ts), kind, note))
    con.commit()


def calibration_events(con) -> list[pd.Timestamp]:
    rows = con.execute("SELECT ts_ms FROM calibration_events ORDER BY ts_ms").fetchall()
    return [pd.Timestamp(r[0], unit="ms", tz="UTC") for r in rows]


def export_csv(con: sqlite3.Connection, path: str | Path, start=None, end=None) -> int:
    """Export raw readings plus forward-filled context to CSV (UTC and local time)."""
    df = read_readings(con, start, end)
    entities = [r[0] for r in con.execute("SELECT DISTINCT entity FROM context")]
    for e in entities:
        df[e] = context_on_grid(con, e, df.index)
    out = df.reset_index()
    out.insert(1, "local_time", out["ts"].dt.tz_convert("Europe/Amsterdam").astype(str))
    out.to_csv(path, index=False)
    return len(out)


def import_csv(con: sqlite3.Connection, path: str | Path, source: str = "sensor",
               context_cols: tuple[str, ...] = ("light_on", "co2_on", "airstone_on",
                                                 "temperature", "ph")) -> int:
    """Backfill from a CSV with a ``ts`` column (ISO, with offset or UTC)."""
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts")
    n = insert_readings(con, df, source=source)
    for c in context_cols:
        if c in df.columns:
            insert_context_changes(con, df[c], c)
    return n
