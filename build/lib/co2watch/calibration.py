"""Self-calibration: map the sensor's hue onto the leaflet chart without a test kit.

Model
-----
The sensor sees a different hue than the printed chart, but the relation is
smooth. We assume a straight line between them:

    chart_hue = a + b * sensor_hue

and learn ``a`` and ``b`` from *anchors* (points where we believe we know the
chart position):

1. Night anchors (automatic, every day). After a night of air stone the drop
   checker sits near its lowest CO2. We assume that is the leaflet's lowest
   band (3 mg/l). This pins the offset ``a``.
2. Eye checks (optional, a few taps). "By eye the checker looks green" gives a
   chart band for the current sensor hue. Two or more eye checks at different
   colours pin the scale ``b``.
3. Without eye checks ``b`` stays near its prior (1.0, i.e. the chart spacing),
   and the CO2 estimate gets a wide uncertainty band. We report that honestly
   rather than pretend.

The daytime plateau is *measured*, not assumed: it is the tank's normal daily
peak, which the detectors compare against. It is never forced to "green".

After a calibration event (fluid replaced, sensor moved) only anchors after the
event are used, and the fit reports ``status='settling'`` for a few days.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from .config import CHART_KH4, CalibrationConfig

_CHART_HUE = np.array([b.chart_hue_deg for b in CHART_KH4])  # decreasing with CO2
_CHART_LOGC = np.log10([b.co2_mg_l for b in CHART_KH4])


def chart_hue_to_co2(chart_hue: np.ndarray | float) -> np.ndarray:
    """Leaflet chart hue -> CO2 mg/l, interpolated in log space, linear
    extrapolation past the ends (clipped to 0.5-150 mg/l)."""
    h = np.asarray(chart_hue, dtype=float)
    xs, ys = _CHART_HUE[::-1], _CHART_LOGC[::-1]  # ascending hue
    logc = np.interp(h, xs, ys)
    lo_slope = (ys[1] - ys[0]) / (xs[1] - xs[0])
    hi_slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
    logc = np.where(h < xs[0], ys[0] + (h - xs[0]) * lo_slope, logc)
    logc = np.where(h > xs[-1], ys[-1] + (h - xs[-1]) * hi_slope, logc)
    return np.clip(10 ** logc, 0.5, 150.0)


def co2_to_chart_hue(co2: float) -> float:
    return float(np.interp(math.log10(co2), _CHART_LOGC, _CHART_HUE))


def band_for_chart_hue(chart_hue: float) -> str:
    i = int(np.argmin(np.abs(_CHART_HUE - chart_hue)))
    return CHART_KH4[i].name


BAND_CHART_HUE = {b.name: b.chart_hue_deg for b in CHART_KH4}


# --------------------------------------------------------------------------
# Anchor extraction
# --------------------------------------------------------------------------

def _stable_extreme(day: pd.DataFrame, mask: np.ndarray, cfg: CalibrationConfig,
                    pick: str) -> tuple[float, pd.Timestamp | None, int]:
    """Most extreme stable rolling-median hue inside ``mask``."""
    sub = day.loc[mask]
    if sub["hue"].notna().sum() < 6:
        return np.nan, None, 0
    w = f"{cfg.plateau_window_min}min"
    med = sub["hue"].rolling(w, min_periods=6).median()
    lag = max(1, cfg.plateau_window_min // 5)
    slope = (med - med.shift(lag)).abs() / (cfg.plateau_window_min / 60.0)
    stable = med.where(slope <= cfg.max_stable_slope_deg_h)
    if stable.notna().sum() == 0:
        stable = med  # fall back: most extreme value, flagged by low n
        n = 0
    else:
        n = int(stable.notna().sum())
    t = stable.idxmax() if pick == "max" else stable.idxmin()
    if pd.isna(t):
        return np.nan, None, 0
    return float(stable.loc[t]), t, n


def extract_daily_anchors(df: pd.DataFrame, cfg: CalibrationConfig | None = None) -> pd.DataFrame:
    """One row per local date: overnight low (bluest stable hue) and daytime
    plateau (greenest stable hue), plus the clear-channel level.

    ``df`` needs ``hue``, ``clear``, ``local_hour``, ``local_date`` (see
    features.add_basic_features). Context columns ``co2_on`` and
    ``airstone_on`` are used when present.
    """
    cfg = cfg or CalibrationConfig()
    rows = []
    has_ctx = {"co2_on", "airstone_on"} <= set(df.columns)
    for date, day in df.groupby("local_date"):
        h = day["local_hour"].to_numpy()
        if has_ctx and day["co2_on"].notna().any():
            co2 = day["co2_on"].fillna(False).astype(bool).to_numpy()
            air = day["airstone_on"].fillna(False).astype(bool).to_numpy()
            night = air & ~co2 & (h < 12)
            on_hours = h[co2]
            day_mask = (h >= on_hours.min()) if on_hours.size else (h >= 10)
        else:
            lo, hi = cfg.night_window_local
            night = (h >= lo) & (h < hi)
            day_mask = h >= 10
        night_hue, night_t, night_n = _stable_extreme(day, night, cfg, "max")
        plat_hue, plat_t, plat_n = _stable_extreme(day, day_mask, cfg, "min")
        rows.append({
            "local_date": date,
            "night_hue": night_hue, "night_ts": night_t, "night_n": night_n,
            "plateau_hue": plat_hue, "plateau_ts": plat_t, "plateau_n": plat_n,
            "clear_night": float(day.loc[night, "clear"].median()) if night.any() else np.nan,
            "coverage": float(day["hue"].notna().mean() * len(day) / 288.0),
        })
    out = pd.DataFrame(rows)
    out["valid"] = (out["night_n"] >= 3) & (out["coverage"] >= 0.8)
    return out


# --------------------------------------------------------------------------
# Fit
# --------------------------------------------------------------------------

@dataclass
class CalibrationFit:
    a: float
    b: float
    cov: np.ndarray  # 2x2 posterior covariance of (a, b)
    n_night: int
    n_eye: int
    status: str  # "prior_only" | "settling" | "ok"
    since: pd.Timestamp | None  # last calibration event used as start

    def chart_hue(self, sensor_hue):
        return self.a + self.b * np.asarray(sensor_hue, dtype=float)

    def chart_hue_sd(self, sensor_hue):
        x = np.asarray(sensor_hue, dtype=float)
        var = self.cov[0, 0] + 2 * x * self.cov[0, 1] + x ** 2 * self.cov[1, 1]
        return np.sqrt(np.maximum(var, 0))

    def co2_estimate(self, sensor_hue) -> dict:
        """Estimated CO2 with a ~80 % interval. Higher chart hue = less CO2."""
        ch = self.chart_hue(sensor_hue)
        sd = self.chart_hue_sd(sensor_hue)
        est = chart_hue_to_co2(ch)
        lo = chart_hue_to_co2(ch + 1.28 * sd)
        hi = chart_hue_to_co2(ch - 1.28 * sd)
        return {"co2_mg_l": est, "low": lo, "high": hi}

    def describe(self, sensor_hue: float) -> str:
        e = self.co2_estimate(sensor_hue)
        band = band_for_chart_hue(float(self.chart_hue(sensor_hue)))
        conf = {"ok": "", "settling": " (calibration still settling)",
                "prior_only": " (rough: no anchors yet)"}[self.status]
        return (f"looks like the '{band}' band, roughly {e['co2_mg_l']:.0f} mg/l "
                f"(plausible {e['low']:.0f}-{e['high']:.0f}){conf}")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cov"] = self.cov.tolist()
        d["since"] = None if self.since is None else self.since.isoformat()
        return d


def fit_calibration(
    anchors: pd.DataFrame,
    eye_checks: pd.DataFrame | None = None,
    calibration_events: list[pd.Timestamp] | None = None,
    cfg: CalibrationConfig | None = None,
    now: pd.Timestamp | None = None,
) -> CalibrationFit:
    """Bayesian straight-line fit chart_hue = a + b * sensor_hue.

    anchors: output of extract_daily_anchors.
    eye_checks: columns ``sensor_hue`` and ``band`` (a CHART_KH4 band name).
    calibration_events: UTC timestamps; only data after the latest is used.
    """
    cfg = cfg or CalibrationConfig()
    since = max(calibration_events) if calibration_events else None

    a_df = anchors[anchors["valid"]].copy()
    if since is not None:
        a_df = a_df[a_df["night_ts"].notna()]
        a_df = a_df[pd.to_datetime(a_df["night_ts"], utc=True) > since]
    if now is not None and len(a_df):
        a_df = a_df[pd.to_datetime(a_df["night_ts"], utc=True) <= now]
    a_df = a_df.tail(cfg.anchor_history_days)

    xs, ys, ws = [], [], []
    night_y = co2_to_chart_hue(cfg.night_anchor_co2_mg_l)
    night_sd = 8.0  # deg; how sure we are the night low sits on the 3 mg/l band
    for x in a_df["night_hue"].dropna():
        xs.append(x); ys.append(night_y); ws.append(1.0 / night_sd ** 2)

    n_eye = 0
    if eye_checks is not None and len(eye_checks):
        ec = eye_checks
        if since is not None and "ts" in ec:
            ec = ec[pd.to_datetime(ec["ts"], utc=True) > since]
        for _, r in ec.iterrows():
            if r["band"] in BAND_CHART_HUE and np.isfinite(r["sensor_hue"]):
                xs.append(float(r["sensor_hue"])); ys.append(BAND_CHART_HUE[r["band"]])
                ws.append(1.0 / 12.0 ** 2)  # an eye is coarse: about one band
                n_eye += 1

    # Night anchors are all at nearly the same x, so they are strongly
    # correlated evidence; cap their total weight to that of ~3 independent ones.
    xs, ys, ws = np.array(xs), np.array(ys), np.array(ws)
    n_night = len(a_df["night_hue"].dropna())
    if n_night > 3:
        ws[:n_night] *= 3.0 / n_night

    # Prior: b ~ N(b0, sb), a centred so that the prior maps the typical
    # sensor night hue (or the chart's own) to the night band.
    b0, sb = cfg.scale_prior_mean, cfg.scale_prior_sd
    x_ref = float(np.median(xs[:n_night])) if n_night else night_y
    a0, sa = night_y - b0 * x_ref, 60.0
    P0 = np.diag([1 / sa ** 2, 1 / sb ** 2])
    m0 = np.array([a0, b0])
    X = np.column_stack([np.ones_like(xs), xs]) if len(xs) else np.zeros((0, 2))
    W = np.diag(ws) if len(ws) else np.zeros((0, 0))
    P = P0 + X.T @ W @ X
    cov = np.linalg.inv(P)
    mean = cov @ (P0 @ m0 + (X.T @ W @ ys if len(ys) else 0))

    if n_night == 0 and n_eye == 0:
        status = "prior_only"
    elif n_night < cfg.settle_days:
        status = "settling"
    else:
        status = "ok"
    return CalibrationFit(a=float(mean[0]), b=float(mean[1]), cov=cov, n_night=n_night,
                          n_eye=n_eye, status=status, since=since)


# --------------------------------------------------------------------------
# Drift
# --------------------------------------------------------------------------

def _theil_sen(y: np.ndarray) -> float:
    x = np.arange(len(y), dtype=float)
    ok = np.isfinite(y)
    x, y = x[ok], y[ok]
    if len(y) < 4:
        return np.nan
    i, j = np.triu_indices(len(y), 1)
    return float(np.median((y[j] - y[i]) / (x[j] - x[i])))


def drift_report(anchors: pd.DataFrame, days: int = 14,
                 calibration_events: list[pd.Timestamp] | None = None) -> dict:
    """Slow-drift summary in degrees of hue per day.

    Fluid ageing moves the whole scale, so night and plateau drift together.
    An injection problem (diffuser, bubble counter, regulator) moves mainly the
    daytime plateau toward the night level. This is a *description* for the
    detectors and their templated alert reasons, not a verdict.
    """
    a = anchors[anchors["valid"]].copy()
    if calibration_events:
        since = max(calibration_events)
        a = a[pd.to_datetime(a["night_ts"], utc=True) > since]
    a = a.tail(days)
    night = _theil_sen(a["night_hue"].to_numpy(dtype=float))
    plat = _theil_sen(a["plateau_hue"].to_numpy(dtype=float))
    span = (a["night_hue"] - a["plateau_hue"]).to_numpy(dtype=float)
    span_slope = _theil_sen(span)
    if not np.isfinite(night) or not np.isfinite(plat):
        pattern = "not_enough_data"
    elif abs(span_slope) < 0.5 and abs(night) < 0.05:
        pattern = "stable"
    elif abs(span_slope) < 0.5:
        pattern = "whole_scale_shift"  # consistent with fluid ageing
    elif span_slope < 0:
        pattern = "daily_swing_shrinking"  # plateau creeping toward night level
    else:
        pattern = "daily_swing_growing"
    return {"days": int(len(a)), "night_slope_deg_day": night, "plateau_slope_deg_day": plat,
            "swing_slope_deg_day": span_slope, "pattern": pattern}
