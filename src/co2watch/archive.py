"""Long-term storage in SQLite: aggregates, plus raw windows around events.

VictoriaMetrics keeps the full-resolution readings for 90 days. What should
outlive that goes into SQLite, nightly:

* ``agg_hourly``: per UTC hour, robust summaries of hue, net (LED) signal,
  stray light, data coverage, dimmer level, the firmware CO2 index and the
  highest detection score;
* ``agg_daily``: per local day, the daily shape (night low, peak, swing, when
  the rise reached 50 % and 90 % of the swing, minutes near the peak), stray
  light, coverage and alert counts;
* ``raw_windows`` / ``raw_samples``: full-resolution two-frame readings, only
  around logged alerts and labels (padded), so interesting events can be
  re-analysed after VictoriaMetrics has dropped them.

Everything is idempotent: re-running over the same days replaces the same
rows, and a window is archived once.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import db
from .calibration import extract_daily_anchors
from .config import TZ
from .features import FRAME_COLS

SCHEMA = """
CREATE TABLE IF NOT EXISTS agg_hourly (
    hour_ms        INTEGER PRIMARY KEY,   -- UTC start of the hour, epoch ms
    local_date     TEXT, local_hour INTEGER,
    n              INTEGER, coverage REAL,
    hue_min REAL, hue_p10 REAL, hue_med REAL, hue_p90 REAL, hue_max REAL,
    net_clear_med  REAL, ambient_med REAL, ambient_max REAL,
    saturated_n    INTEGER,
    light_pct_med  REAL, co2_index_med REAL, co2_index_max REAL,
    score_max      REAL
);
CREATE TABLE IF NOT EXISTS agg_daily (
    local_date     TEXT PRIMARY KEY,
    n              INTEGER, coverage REAL,
    hue_min REAL, hue_med REAL, hue_max REAL,
    night_hue REAL, peak_hue REAL, swing REAL,
    rise_t50_local TEXT,                  -- HH:MM the hue first got halfway from night to peak
    rise_t90_local TEXT,                  -- HH:MM it reached 90 % of the swing
    peak_minutes   INTEGER,               -- minutes within 10 % of the swing from the peak
    net_clear_med  REAL, ambient_max REAL,
    co2_index_max  REAL, light_pct_max REAL,
    alerts_n       INTEGER, score_max REAL,
    calibration_epoch_ms INTEGER          -- latest calibration event before the day
);
CREATE TABLE IF NOT EXISTS raw_windows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL UNIQUE,      -- 'alert:<id>' or 'label:<id>'
    start_ms   INTEGER NOT NULL,
    end_ms     INTEGER NOT NULL,
    created_ms INTEGER NOT NULL,
    n          INTEGER
);
CREATE TABLE IF NOT EXISTS raw_samples (
    window_id  INTEGER NOT NULL REFERENCES raw_windows(id),
    ts_ms      INTEGER NOT NULL,
    red_off REAL, green_off REAL, blue_off REAL, clear_off REAL,
    red_on REAL, green_on REAL, blue_on REAL, clear_on REAL,
    PRIMARY KEY (window_id, ts_ms)
);
"""


def ensure_schema(con) -> None:
    con.executescript(SCHEMA)


def _f(x):
    return None if x is None or (isinstance(x, float) and not np.isfinite(x)) or pd.isna(x) \
        else float(x)


def hourly(df: pd.DataFrame, scores: pd.Series | None = None) -> pd.DataFrame:
    """Hourly aggregates of a prepared frame (features.prepare output)."""
    g = df.groupby(df.index.floor("h"))
    out = pd.DataFrame({
        "n": g["hue"].count(),
        "coverage": g["missing"].apply(lambda m: float((~m.astype(bool)).mean())),
        "hue_min": g["hue"].min(), "hue_p10": g["hue"].quantile(0.1),
        "hue_med": g["hue"].median(), "hue_p90": g["hue"].quantile(0.9),
        "hue_max": g["hue"].max(),
        "net_clear_med": g["clear"].median(),
        "ambient_med": g["ambient_clear"].median() if "ambient_clear" in df else np.nan,
        "ambient_max": g["ambient_clear"].max() if "ambient_clear" in df else np.nan,
        "saturated_n": g["saturated"].sum() if "saturated" in df else 0,
    })
    for col, name, how in (("light_pct", "light_pct_med", "median"),
                           ("co2_index_pct", "co2_index_med", "median"),
                           ("co2_index_pct", "co2_index_max", "max")):
        out[name] = getattr(g[col], how)() if col in df else np.nan
    out["score_max"] = scores.groupby(scores.index.floor("h")).max() if scores is not None \
        else np.nan
    local = out.index.tz_convert(TZ)
    out["local_date"] = [str(d) for d in local.date]
    out["local_hour"] = local.hour
    return out


def _cross_time(day: pd.DataFrame, level: float) -> str | None:
    """First local HH:MM the 30-min median hue went at or below ``level``."""
    h = day["hue"].rolling("30min", min_periods=3).median()
    hit = h[h <= level]
    return None if hit.empty else hit.index[0].tz_convert(TZ).strftime("%H:%M")


def daily(df: pd.DataFrame, alerts: pd.DataFrame | None = None,
          events: list[pd.Timestamp] | None = None, scores: pd.Series | None = None
          ) -> pd.DataFrame:
    anchors = extract_daily_anchors(df).set_index("local_date")
    rows = []
    for date, day in df.groupby("local_date"):
        a = anchors.loc[date] if date in anchors.index else None
        night = a["night_hue"] if a is not None else np.nan
        peak = a["plateau_hue"] if a is not None else np.nan
        swing = night - peak
        rec = {"local_date": str(date), "n": int(day["hue"].count()),
               "coverage": float((~day["missing"].astype(bool)).sum() / 288.0),
               "hue_min": day["hue"].min(), "hue_med": day["hue"].median(),
               "hue_max": day["hue"].max(), "night_hue": night, "peak_hue": peak,
               "swing": swing, "rise_t50_local": None, "rise_t90_local": None,
               "peak_minutes": None,
               "net_clear_med": day["clear"].median(),
               "ambient_max": day["ambient_clear"].max() if "ambient_clear" in day else np.nan,
               "co2_index_max": day["co2_index_pct"].max() if "co2_index_pct" in day else np.nan,
               "light_pct_max": day["light_pct"].max() if "light_pct" in day else np.nan}
        if np.isfinite(swing) and swing > 5:
            rec["rise_t50_local"] = _cross_time(day, night - 0.5 * swing)
            rec["rise_t90_local"] = _cross_time(day, night - 0.9 * swing)
            near = day["hue"] <= peak + 0.1 * swing
            rec["peak_minutes"] = int(near.sum() * 5)
        start, end = day.index[0], day.index[-1]
        if alerts is not None and len(alerts):
            hit = alerts[(alerts["start"] <= end) & (alerts["end"] >= start)]
            rec["alerts_n"] = int(len(hit))
        else:
            rec["alerts_n"] = 0
        rec["score_max"] = float(scores.loc[start:end].max()) if scores is not None else None
        ev = [e for e in (events or []) if e <= end]
        rec["calibration_epoch_ms"] = db.ms(max(ev)) if ev else None
        rows.append(rec)
    return pd.DataFrame(rows)


def complete_days(df: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    """Keep only local days that are over (so a partial today is not stored)."""
    today = now.tz_convert(TZ).date()
    return df[df["local_date"] < today]


def save_hourly(con, h: pd.DataFrame) -> int:
    cols = ["n", "coverage", "hue_min", "hue_p10", "hue_med", "hue_p90", "hue_max",
            "net_clear_med", "ambient_med", "ambient_max", "saturated_n", "light_pct_med",
            "co2_index_med", "co2_index_max", "score_max"]
    rows = [(db.ms(t), r["local_date"], int(r["local_hour"]), *[_f(r[c]) for c in cols])
            for t, r in h.iterrows()]
    con.executemany(f"INSERT OR REPLACE INTO agg_hourly VALUES ({','.join('?' * (3 + len(cols)))})",
                    rows)
    con.commit()
    return len(rows)


def save_daily(con, d: pd.DataFrame) -> int:
    cols = ["local_date", "n", "coverage", "hue_min", "hue_med", "hue_max", "night_hue",
            "peak_hue", "swing", "rise_t50_local", "rise_t90_local", "peak_minutes",
            "net_clear_med", "ambient_max", "co2_index_max", "light_pct_max", "alerts_n",
            "score_max", "calibration_epoch_ms"]
    text = {"local_date", "rise_t50_local", "rise_t90_local"}
    rows = [tuple(r[c] if c in text else _f(r[c]) for c in cols) for _, r in d.iterrows()]
    con.executemany(f"INSERT OR REPLACE INTO agg_daily ({','.join(cols)}) VALUES "
                    f"({','.join('?' * len(cols))})", rows)
    con.commit()
    return len(rows)


def event_windows(con, pad_h: float = 6.0) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    """Alerts and labels that need a raw window, padded by ``pad_h``."""
    pad = pd.Timedelta(hours=pad_h)
    out = []
    for _, a in db.read_alerts(con).iterrows():
        end = a["end"] if pd.notna(a["end"]) else a["start"]
        out.append((f"alert:{a['id']}", a["start"] - pad, end + pad))
    for _, lab in db.read_labels(con).iterrows():
        out.append((f"label:{lab['id']}", lab["start"] - pad, lab["end"] + pad))
    done = {r[0] for r in con.execute("SELECT source FROM raw_windows")}
    return [w for w in out if w[0] not in done]


def save_raw_window(con, source: str, frames: pd.DataFrame) -> int:
    """Store one window of raw frames (index UTC, FRAME_COLS)."""
    if frames.empty:
        return 0
    cur = con.execute("INSERT INTO raw_windows (source,start_ms,end_ms,created_ms,n) "
                      "VALUES (?,?,?,?,?)",
                      (source, db.ms(frames.index[0]), db.ms(frames.index[-1]),
                       db.ms(pd.Timestamp.now(tz="UTC")), len(frames)))
    wid = cur.lastrowid
    rows = [(wid, db.ms(t), *[_f(v) for v in r]) for t, r in
            zip(frames.index, frames[FRAME_COLS].to_numpy())]
    con.executemany("INSERT OR IGNORE INTO raw_samples VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return len(rows)


def read_raw_window(con, source: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT s.* FROM raw_samples s JOIN raw_windows w ON w.id = s.window_id "
        "WHERE w.source = ? ORDER BY ts_ms", con, params=[source])
    df["ts"] = pd.to_datetime(df.pop("ts_ms"), unit="ms", utc=True)
    return df.drop(columns=["window_id"]).set_index("ts")
