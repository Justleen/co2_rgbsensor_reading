"""VictoriaMetrics access: raw readings in and out.

VictoriaMetrics scrapes the D1's ``/metrics``; this module only reads from it,
except ``import_frames`` which is used for backfill and for loading simulated
data. Imported data always carries a ``source`` label (e.g. ``source="sim"``),
so it can never be confused with the scraped series, which have no such label.

Only the standard library is used for HTTP so the container stays small.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from .config import (CHANNELS, CONTEXT_METRICS, LED_STATES, METRIC_RAW, SOURCE_LABEL, TZ)
from .features import FRAME_COLS


def _sel(metric: str, source: str | None, extra: str = "") -> str:
    # source=None -> scraped data (no source label); VM treats source="" as "absent"
    src = source or ""
    inner = f'{SOURCE_LABEL}="{src}"' + (f",{extra}" if extra else "")
    return f"{metric}{{{inner}}}"


def _get(url: str, timeout: float = 60.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (local URL)
        return r.read()


def parse_export(lines: bytes | str) -> list[dict]:
    """Parse VM ``/api/v1/export`` JSON-lines output."""
    if isinstance(lines, bytes):
        lines = lines.decode()
    return [json.loads(l) for l in lines.splitlines() if l.strip()]


def export_series(base_url: str, match: str, start, end) -> list[dict]:
    q = urllib.parse.urlencode({
        "match[]": match,
        "start": int(pd.Timestamp(start).timestamp()),
        "end": int(pd.Timestamp(end).timestamp()),
    })
    return parse_export(_get(f"{base_url.rstrip('/')}/api/v1/export?{q}"))


def series_to_frames(series: list[dict]) -> pd.DataFrame:
    """Raw-metric series -> wide DataFrame with ``red_off .. clear_on`` columns,
    indexed by sample time (UTC). Samples are kept exactly as stored."""
    cols = {}
    for s in series:
        m = s["metric"]
        name = f"{m.get('channel')}_{m.get('led')}"
        if name not in FRAME_COLS:
            continue
        idx = pd.to_datetime(s["timestamps"], unit="ms", utc=True)
        cols[name] = pd.Series(s["values"], index=idx, dtype=float)
    df = pd.DataFrame(cols).sort_index()
    for c in FRAME_COLS:
        if c not in df:
            df[c] = np.nan
    df.index.name = "ts"
    return df[FRAME_COLS]


def read_frames(base_url: str, start, end, source: str | None = None) -> pd.DataFrame:
    """Raw two-frame readings between start and end (tz-aware)."""
    return series_to_frames(export_series(base_url, _sel(METRIC_RAW, source), start, end))


def read_context(base_url: str, start, end, source: str | None = None) -> pd.DataFrame:
    """Controller states that exist in VM (missing ones are simply absent)."""
    out = {}
    for col, metric in CONTEXT_METRICS.items():
        ser = export_series(base_url, _sel(metric, source), start, end)
        if ser:
            s = ser[0]
            out[col] = pd.Series(s["values"], index=pd.to_datetime(s["timestamps"], unit="ms",
                                                                    utc=True), dtype=float)
    df = pd.DataFrame(out).sort_index()
    for c in ("light_on", "co2_on", "airstone_on"):
        if c in df:
            df[c] = df[c].map(lambda v: np.nan if pd.isna(v) else bool(v > 0.5))
    df.index.name = "ts"
    return df


def read_all(base_url: str, start, end, source: str | None = None) -> pd.DataFrame:
    """Frames plus context forward-filled onto the frame timestamps."""
    frames = read_frames(base_url, start, end, source)
    ctx = read_context(base_url, start, end, source)
    if not ctx.empty and not frames.empty:
        ctx = ctx.reindex(ctx.index.union(frames.index)).ffill().reindex(frames.index)
        frames = frames.join(ctx)
    return frames


# ---------------------------------------------------------------- write (backfill / sim)

def to_prometheus_lines(df: pd.DataFrame, source: str) -> list[str]:
    """Wide frames (+ context) -> Prometheus text lines with ms timestamps, as
    accepted by VM ``/api/v1/import/prometheus``."""
    if not source:
        raise ValueError("imports need a source label so they never mix with scraped data")
    ts_ms = df.index.tz_convert("UTC").as_unit("ms").asi8.astype(np.int64)
    lines = []
    for ch in CHANNELS:
        for led in LED_STATES:
            col = f"{ch}_{led}"
            if col not in df:
                continue
            lab = f'{{channel="{ch}",led="{led}",{SOURCE_LABEL}="{source}"}}'
            for t, v in zip(ts_ms, df[col].to_numpy(dtype=float)):
                if np.isfinite(v):
                    lines.append(f"{METRIC_RAW}{lab} {v:g} {t}")
    for col, metric in CONTEXT_METRICS.items():
        if col in df:
            lab = f'{{{SOURCE_LABEL}="{source}"}}'
            for t, v in zip(ts_ms, df[col].to_numpy(dtype=float)):
                if np.isfinite(v):
                    lines.append(f"{metric}{lab} {v:g} {t}")
    return lines


def import_frames(base_url: str, df: pd.DataFrame, source: str, chunk: int = 50_000) -> int:
    lines = to_prometheus_lines(df, source)
    url = f"{base_url.rstrip('/')}/api/v1/import/prometheus"
    for i in range(0, len(lines), chunk):
        body = ("\n".join(lines[i:i + chunk]) + "\n").encode()
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310
            r.read()
    return len(lines)


# ---------------------------------------------------------------- CSV

def frames_to_csv(df: pd.DataFrame, path: str | Path) -> int:
    out = df.reset_index()
    out.insert(1, "local_time", out["ts"].dt.tz_convert(TZ).astype(str))
    out.to_csv(path, index=False)
    return len(out)


def frames_from_csv(path: str | Path) -> pd.DataFrame:
    """CSV with ``ts`` (ISO with offset, or UTC) and ``red_off .. clear_on``;
    optional context columns ``light_on, co2_on, airstone_on, temperature, ph``."""
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.drop(columns=[c for c in ("local_time",) if c in df]).set_index("ts")
    missing = [c for c in FRAME_COLS if c not in df]
    if missing:
        raise ValueError(f"CSV is missing columns: {missing}")
    for c in ("light_on", "co2_on", "airstone_on"):
        if c in df and df[c].dtype == object:
            df[c] = df[c].astype(str).str.lower().isin(["true", "1", "on"])
    return df
