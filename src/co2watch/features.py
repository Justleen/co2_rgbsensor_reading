"""Feature engineering from raw TCS34725 channels.

All timestamps are stored and processed in UTC. Local (Europe/Amsterdam) time
is derived only for features that depend on the daily schedule, so daylight
saving switches never create duplicate or missing rows.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import SAMPLE_PERIOD_MIN, TZ


def hue_from_rgb(r, g, b) -> np.ndarray:
    """HSV hue in degrees [0, 360) from raw counts (any common scale).

    Vectorised. Returns NaN where all channels are 0 or any is NaN.
    """
    r = np.asarray(r, dtype=float)
    g = np.asarray(g, dtype=float)
    b = np.asarray(b, dtype=float)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    d = mx - mn
    with np.errstate(invalid="ignore", divide="ignore"):
        h = np.where(
            mx == r,
            ((g - b) / d) % 6,
            np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4),
        )
    h = h * 60.0
    h = np.where((d <= 0) | ~np.isfinite(h), np.nan, h)
    return h % 360.0


def saturation_from_rgb(r, g, b) -> np.ndarray:
    r, g, b = (np.asarray(x, dtype=float) for x in (r, g, b))
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    with np.errstate(invalid="ignore", divide="ignore"):
        s = np.where(mx > 0, (mx - mn) / mx, np.nan)
    return s


FRAME_COLS = [f"{c}_{led}" for led in ("off", "on") for c in ("red", "green", "blue", "clear")]


def combine_frames(df: pd.DataFrame, saturation: float | None = None) -> pd.DataFrame:
    """Turn the two raw frames per reading into the signal used downstream.

    Input columns ``red_off .. clear_off`` (LEDs off: ambient only) and
    ``red_on .. clear_on`` (LEDs on: ambient + LED light through the checker).

    Output adds ``red, green, blue, clear`` = on - off (ambient subtracted,
    clipped at 0), ``ambient_clear`` = clear_off and ``saturated`` = any on-frame
    channel at the sensor limit. A saturated reading is not usable because the
    subtraction is then wrong.
    """
    out = df.copy()
    for c in ("red", "green", "blue", "clear"):
        out[c] = (out[f"{c}_on"] - out[f"{c}_off"]).clip(lower=0)
    out["ambient_clear"] = out["clear_off"]
    out["ambient_frac"] = out["clear_off"] / out["clear_on"].replace(0, np.nan)
    if saturation is not None:
        on = out[[f"{c}_on" for c in ("red", "green", "blue", "clear")]]
        out["saturated"] = (on >= saturation).any(axis=1)
        out.loc[out["saturated"], ["red", "green", "blue", "clear"]] = np.nan
    else:
        out["saturated"] = False
    return out


def to_regular_grid(df: pd.DataFrame, period_min: int = SAMPLE_PERIOD_MIN) -> pd.DataFrame:
    """Put readings on a regular UTC grid. Missing slots become NaN rows with
    ``missing=True``; they are never interpolated here (gaps are information).

    ``df`` must have a tz-aware ``ts`` column or index.
    """
    if "ts" in df.columns:
        df = df.set_index("ts")
    if df.index.tz is None:
        raise ValueError("timestamps must be timezone-aware (UTC)")
    df = df.sort_index()
    df.index = df.index.tz_convert("UTC")
    df = df[~df.index.duplicated(keep="last")]
    rule = f"{period_min}min"
    grid = df.resample(rule).last()
    cols = [c for c in FRAME_COLS if c in grid.columns] or ["red", "green", "blue", "clear"]
    grid["missing"] = grid[cols].isna().any(axis=1)
    grid.index.name = "ts"
    return grid


def add_basic_features(df: pd.DataFrame) -> pd.DataFrame:
    """Per-reading features: hue, saturation, channel ratios, local time."""
    out = df.copy()
    c = out["clear"].astype(float).replace(0, np.nan)
    out["r_c"] = out["red"] / c
    out["g_c"] = out["green"] / c
    out["b_c"] = out["blue"] / c
    out["hue"] = hue_from_rgb(out["red"], out["green"], out["blue"])
    out["sat"] = saturation_from_rgb(out["red"], out["green"], out["blue"])
    local = out.index.tz_convert(TZ)
    out["local_hour"] = local.hour + local.minute / 60.0
    out["local_date"] = local.date
    out["utc_offset_h"] = [t.utcoffset().total_seconds() / 3600 for t in local]
    return out


def add_window_features(df: pd.DataFrame, window_min: int = 30) -> pd.DataFrame:
    """Rolling features used by the detectors (computed on the regular grid)."""
    out = df.copy()
    w = f"{window_min}min"
    hue = out["hue"]
    out["hue_med"] = hue.rolling(w, min_periods=2).median()
    out["hue_std"] = hue.rolling(w, min_periods=3).std()
    # slope in deg/hour via difference of rolling medians
    lag = max(1, window_min // SAMPLE_PERIOD_MIN)
    out["hue_slope_h"] = (out["hue_med"] - out["hue_med"].shift(lag)) / (window_min / 60.0)
    out["clear_med"] = out["clear"].rolling(w, min_periods=2).median()
    clear_ref = out["clear"].rolling("7D", min_periods=12).median()
    out["clear_rel"] = out["clear_med"] / clear_ref
    if "ambient_clear" in out:
        # light leak / moved sensor: ambient (LEDs-off) level vs the same hour
        # over the last week is done by the baseline; here just the window level
        out["ambient_med"] = out["ambient_clear"].rolling(w, min_periods=2).median()
    # flat-line indicator: all raw channels unchanged over the window
    raw_cols = [c for c in FRAME_COLS if c in out.columns] or ["red", "green", "blue", "clear"]
    raw = out[raw_cols]
    changed = raw.diff().abs().sum(axis=1) > 0
    out["n_changes"] = changed.astype(float).rolling(w, min_periods=1).sum()
    return out


def minutes_since_state_on(index: pd.DatetimeIndex, states: pd.Series | None) -> pd.Series:
    """Minutes since a boolean context entity (e.g. CO2 solenoid) last turned
    on; NaN while off or unknown. ``states`` is a bool Series indexed by UTC
    change time.
    """
    if states is None or states.empty:
        return pd.Series(np.nan, index=index)
    s = states.sort_index().astype(bool)
    aligned = s.reindex(s.index.union(index)).ffill().reindex(index)
    on_times = s.index[(s & ~s.shift(1, fill_value=False)).to_numpy()]
    last_on = pd.Series(on_times, index=on_times).reindex(
        pd.DatetimeIndex(on_times).union(index)
    ).ffill().reindex(index)
    mins = (index.to_series() - last_on).dt.total_seconds() / 60.0
    return mins.where(aligned.fillna(False).astype(bool))


def prepare(df: pd.DataFrame, saturation: float | None = None) -> pd.DataFrame:
    """Raw two-frame readings (+ optional context columns) -> regular grid with
    ambient-subtracted channels and per-reading features."""
    return add_basic_features(combine_frames(to_regular_grid(df), saturation))
