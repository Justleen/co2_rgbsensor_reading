"""Synthetic drop checker data with injectable anomalies.

The simulator has a tiny physical core so that the anomalies behave like the
real thing rather than like painted-on spikes:

    tank CO2 (mg/l)  --first-order lag 1-2 h-->  drop checker CO2
    drop checker CO2 --(KH4 formula)--> pH --(leaflet chart)--> chart hue
    chart hue --(hidden sensor transform + fluid ageing)--> sensor hue
    sensor hue + LED + ambient light + noise --> two raw frames per reading
        (LEDs off: red_off..clear_off, LEDs on: red_on..clear_on)

The hidden sensor transform is deliberately different from the chart so the
self-calibration has something real to recover. All schedule logic runs in
Europe/Amsterdam local time; output timestamps are UTC.

Every injected anomaly comes back as a row in a labels table
(start, end, kind, category) so detection can be scored.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import CHART_KH4, REFERENCE_KH, TZ, TankSchedule
from .features import FRAME_COLS

# kind -> category used in labels / evaluation
ANOMALY_CATEGORY = {
    "missed_co2": "real_problem",
    "overdose": "real_problem",
    "solenoid_stuck_open": "real_problem",  # never drops overnight
    "diffuser_decline": "real_problem",  # gradual, not ageing
    "stuck": "sensor_issue",
    "dropout": "sensor_issue",
    "jump": "sensor_issue",
    "case_slipped": "sensor_issue",
    "light_leak": "sensor_issue",
    "fluid_change": "calibration_event",  # not an anomaly; resets ageing
}


@dataclass
class AnomalySpec:
    kind: str
    day: int  # day index from the start of the simulation
    duration_days: float = 1.0  # used by multi-day anomalies
    hour: float = 12.0  # local start hour for sensor faults
    duration_h: float = 3.0  # duration for sensor faults
    magnitude: float = 1.0


@dataclass
class SensorModel:
    # chart_hue = offset + scale * sensor_hue  (hidden from the detector)
    offset: float = 18.0
    scale: float = 0.85
    ageing_deg_per_day: float = -0.12  # fluid ageing shifts the hue slowly
    saturation: float = 0.35
    led_clear: float = 2000.0  # counts from the sensor's own LED
    tank_light_clear: float = 450.0  # extra counts while tank lights are on
    sun_clear: float = 350.0  # peak stray sunlight
    noise_frac: float = 0.006
    flicker_frac: float = 0.02  # ambient change between the off and on frame
    dark_clear: float = 15.0  # dark counts (sensor offset), cancels in on - off


@dataclass
class TankModel:
    c_ambient: float = 2.5  # mg/l the tank settles to with air stone
    inject_rate: float = 6.0  # mg/l/h while the solenoid is open
    degas_day: float = 0.24  # 1/h without air stone
    degas_air: float = 0.6  # 1/h with air stone
    checker_lag_h: float = 1.5
    water_change_frac: float = 0.10
    water_change_co2: float = 3.0  # mg/l in the fresh water
    # Day-to-day variation of a healthy tank: CO2 injection (bubble rate, plant
    # uptake) varies by this fraction (1 sd) from day to day. Real tanks vary;
    # without it every normal day is identical and detection looks too easy.
    day_jitter: float = 0.08


@dataclass
class SimResult:
    readings: pd.DataFrame  # ts (UTC index) red green blue clear + context
    labels: pd.DataFrame  # start end kind category
    truth: pd.DataFrame = field(default_factory=pd.DataFrame)  # hidden state


def _chart_hue_from_ph(ph: np.ndarray) -> np.ndarray:
    phs = np.array([b.ph for b in CHART_KH4])[::-1]  # ascending pH
    hues = np.array([b.chart_hue_deg for b in CHART_KH4])[::-1]
    # linear extrapolation beyond the chart ends, clipped to a sane range
    h = np.interp(ph, phs, hues)
    lo, hi = phs[0], phs[-1]
    slope_lo = (hues[1] - hues[0]) / (phs[1] - phs[0])
    slope_hi = (hues[-1] - hues[-2]) / (phs[-1] - phs[-2])
    h = np.where(ph < lo, hues[0] + (ph - lo) * slope_lo, h)
    h = np.where(ph > hi, hues[-1] + (ph - hi) * slope_hi, h)
    return np.clip(h, 30.0, 240.0)


def _in_window(hour: np.ndarray, start: float, end: float) -> np.ndarray:
    return (hour >= start) & (hour < end)


def simulate(
    days: int = 28,
    start_local: str = "2026-10-12",
    seed: int = 0,
    anomalies: list[AnomalySpec] | None = None,
    schedule: TankSchedule | None = None,
    tank: TankModel | None = None,
    sensor: SensorModel | None = None,
    period_min: int = 5,
) -> SimResult:
    anomalies = anomalies or []
    sch = schedule or TankSchedule()
    tank = tank or TankModel()
    sensor = sensor or SensorModel()
    rng = np.random.default_rng(seed)

    start = pd.Timestamp(start_local, tz=TZ)
    end = start + pd.DateOffset(days=days)
    idx = pd.date_range(start.tz_convert("UTC"), end.tz_convert("UTC"), freq=f"{period_min}min",
                        inclusive="left")
    local = idx.tz_convert(TZ)
    hour = np.asarray(local.hour + local.minute / 60.0)
    day_idx = np.asarray((local.normalize() - start.normalize()).days)
    n = len(idx)
    dt_h = period_min / 60.0

    lights = _in_window(hour, sch.lights_on, sch.lights_off)
    solenoid = _in_window(hour, sch.co2_on, sch.co2_off)
    airstone = ~_in_window(hour, sch.co2_on, sch.lights_off)
    inject_mult = np.ones(n)
    day_factor = 1 + tank.day_jitter * rng.standard_normal(days + 2)
    inject_mult *= np.clip(day_factor, 0.6, 1.4)[day_idx]

    labels: list[dict] = []
    ageing_reset_at: list[int] = []

    def day_mask(d0: float, ndays: float) -> np.ndarray:
        return (day_idx >= d0) & (day_idx < d0 + ndays)

    def local_ts(day: float, h: float) -> pd.Timestamp:
        return (start.normalize() + pd.Timedelta(days=day)).tz_localize(None).tz_localize(TZ) \
            + pd.Timedelta(hours=h)

    for a in anomalies:
        cat = ANOMALY_CATEGORY[a.kind]
        if a.kind == "missed_co2":
            m = day_mask(a.day, a.duration_days)
            inject_mult[m & solenoid] = 0.0
            s, e = local_ts(a.day, sch.co2_on), local_ts(a.day + a.duration_days - 1, sch.lights_off)
        elif a.kind == "overdose":
            m = day_mask(a.day, a.duration_days)
            inject_mult[m] *= 1.0 + 1.2 * a.magnitude
            s, e = local_ts(a.day, sch.co2_on + 1.5), local_ts(a.day + a.duration_days - 1,
                                                             sch.co2_off + 3)
        elif a.kind == "solenoid_stuck_open":
            # solenoid keeps injecting overnight (from this day's CO2 off on)
            t0 = local_ts(a.day, sch.co2_off)
            t1 = local_ts(a.day + a.duration_days, sch.co2_on)
            m = (local >= t0) & (local < t1)
            solenoid = solenoid | np.asarray(m)
            s, e = local_ts(a.day, sch.lights_off + 2), t1 + pd.Timedelta(hours=2)
        elif a.kind == "diffuser_decline":
            m = day_mask(a.day, a.duration_days)
            frac = np.clip((day_idx - a.day + hour / 24) / max(a.duration_days, 1e-9), 0, 1)
            decline = 1.0 - 0.55 * a.magnitude * frac
            inject_mult[m] *= decline[m]
            after = day_idx >= a.day + a.duration_days
            inject_mult[after] *= 1.0 - 0.55 * a.magnitude
            s = local_ts(a.day, sch.co2_on)
            e = local_ts(days, 0)
        elif a.kind == "fluid_change":
            s = e = local_ts(a.day, a.hour)
            ageing_reset_at.append(int(np.searchsorted(idx, s.tz_convert("UTC"))))
        else:  # sensor faults are applied after the optical model
            s = local_ts(a.day, a.hour)
            e = s + pd.Timedelta(hours=a.duration_h)
        labels.append({"start": s.tz_convert("UTC"), "end": e.tz_convert("UTC"),
                       "kind": a.kind, "category": cat})

    # --- tank and drop checker dynamics -------------------------------
    c_tank = np.empty(n)
    c_chk = np.empty(n)
    c = tank.c_ambient
    d = tank.c_ambient
    wc_done_day = -1
    for i in range(n):
        inj = tank.inject_rate * inject_mult[i] if solenoid[i] else 0.0
        k = tank.degas_air if airstone[i] else tank.degas_day
        c += dt_h * (inj - k * (c - tank.c_ambient))
        if hour[i] >= sch.water_change_hour and day_idx[i] != wc_done_day:
            c = (1 - tank.water_change_frac) * c + tank.water_change_frac * tank.water_change_co2
            wc_done_day = day_idx[i]
        d += dt_h * (c - d) / tank.checker_lag_h
        c_tank[i] = c
        c_chk[i] = d

    ph = 7.0 - np.log10(np.maximum(c_chk, 0.05) / (3.0 * REFERENCE_KH))
    chart_hue = _chart_hue_from_ph(ph)

    # fluid ageing, reset by fluid changes
    days_since_fill = np.asarray((idx - idx[0]).total_seconds() / 86400.0)
    for j in sorted(ageing_reset_at):
        days_since_fill[j:] = np.asarray((idx[j:] - idx[j]).total_seconds() / 86400.0)
    sensor_hue = (chart_hue - sensor.offset) / sensor.scale
    sensor_hue = sensor_hue + sensor.ageing_deg_per_day * days_since_fill

    # --- optics ---------------------------------------------------------
    sun = sensor.sun_clear * np.exp(-0.5 * ((hour - sch.sun_hour) / 0.6) ** 2)
    sun *= 0.6 + 0.4 * rng.random(days + 2)[day_idx]  # cloudy days
    signal = np.full(n, sensor.led_clear)
    ambient = lights * sensor.tank_light_clear + sun
    sat = np.full(n, sensor.saturation)

    # case slipped / light leak change optics
    for a in anomalies:
        if a.kind in ("case_slipped", "light_leak"):
            s = local_ts(a.day, a.hour).tz_convert("UTC")
            e = s + pd.Timedelta(hours=a.duration_h)
            m = np.asarray((idx >= s) & (idx < e))
            if a.kind == "case_slipped":
                signal[m] *= 0.55
                ambient[m] += 900 * a.magnitude + lights[m] * 600
            else:
                ambient[m] += 500 * a.magnitude

    rgb = np.array([colorsys.hsv_to_rgb((h % 360) / 360.0, s_, 1.0) for h, s_ in
                    zip(sensor_hue, sat)])
    # Two frames per reading (LEDs switched through the PCF8574):
    #   off frame = ambient only (tank light, sunlight, leaks)
    #   on frame  = LED light through the checker + ambient
    # Ambient is white-ish; it can differ slightly between the two frames
    # (PWM flicker of the tank light), which the subtraction cannot remove.
    tint = np.array([0.36, 0.34, 0.30])
    share = signal[:, None] * rgb / rgb.sum(axis=1, keepdims=True)
    amb_off = ambient[:, None] * tint
    flicker = 1 + sensor.flicker_frac * rng.standard_normal(n) * (ambient > 0)
    amb_on = amb_off * flicker[:, None]

    def frame(counts):
        c = np.column_stack([counts, counts.sum(axis=1) * 1.05 + sensor.dark_clear])
        c = c + np.array([sensor.dark_clear * 0.3] * 3 + [0])  # dark counts
        c *= 1 + sensor.noise_frac * rng.standard_normal(c.shape)
        return np.round(np.clip(c, 0, 65535))

    allc = np.column_stack([frame(amb_off), frame(share + amb_on)])
    df = pd.DataFrame(allc, index=idx, columns=FRAME_COLS)
    df.index.name = "ts"
    df["light_on"] = lights
    df["co2_on"] = solenoid
    df["airstone_on"] = airstone
    df["temperature"] = 24.5 + 0.6 * lights + 0.1 * rng.standard_normal(n)
    # sensor health series as the firmware exposes them
    df["seq"] = np.arange(n, dtype=float)
    df["i2c_errors"] = 0.0
    df["atime"] = 192.0
    df["gain"] = 4.0

    # --- sensor faults on the raw output -----------------------------
    for a in anomalies:
        s = local_ts(a.day, a.hour).tz_convert("UTC")
        e = s + pd.Timedelta(hours=a.duration_h)
        m = (df.index >= s) & (df.index < e)
        if a.kind == "stuck":
            first = df.index[m][0]
            df.loc[m, FRAME_COLS] = df.loc[first, FRAME_COLS].to_numpy()
        elif a.kind == "dropout":
            df = df.loc[~m]
        elif a.kind == "jump":
            df.loc[m, FRAME_COLS] = (df.loc[m, FRAME_COLS]
                                     * np.tile([1.6, 0.7, 0.9, 1.3], 2)).round()

    truth = pd.DataFrame({
        "c_tank": c_tank, "c_checker": c_chk, "ph": ph, "chart_hue": chart_hue,
        "sensor_hue_true": sensor_hue,
    }, index=idx)
    lab = pd.DataFrame(labels, columns=["start", "end", "kind", "category"])
    return SimResult(readings=df, labels=lab, truth=truth)


def default_scenario(days: int = 42) -> list[AnomalySpec]:
    """A mixed test set used by the evaluation notebook and tests."""
    specs = [
        AnomalySpec("stuck", day=16, hour=9.0, duration_h=4),
        AnomalySpec("missed_co2", day=18),
        AnomalySpec("dropout", day=20, hour=2.0, duration_h=2),
        AnomalySpec("overdose", day=22),
        AnomalySpec("case_slipped", day=24, hour=10.0, duration_h=8),
        AnomalySpec("solenoid_stuck_open", day=26),
        AnomalySpec("fluid_change", day=29, hour=10.0),
        AnomalySpec("jump", day=31, hour=14.0, duration_h=0.5),
        AnomalySpec("light_leak", day=33, hour=8.0, duration_h=24),
        AnomalySpec("diffuser_decline", day=35, duration_days=6),
    ]
    return [a for a in specs if a.day + a.duration_days <= days]
