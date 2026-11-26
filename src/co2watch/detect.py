"""M2 detection: hard-fault rules, a per-time-of-day robust baseline and
daily summaries, combined into one score with templated reasons.

Every component returns a score per 5-minute slot where **1.0 means "at the
threshold"**, plus the numbers needed to explain it. The combined score is the
maximum of the weighted component scores; an alert is a run of slots at or
above ``threshold``. No component needs labels.

Cold start: the rules work from the first reading. The baseline and the daily
summaries stay silent until ``min_history_days`` of history exist since the
last calibration event (fluid change / sensor moved).

Hue goes DOWN as CO2 goes UP: a reading "bluer than usual" means less CO2.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .calibration import extract_daily_anchors
from .config import SAMPLE_PERIOD_MIN, TZ, CalibrationConfig
from .features import FRAME_COLS, add_window_features

MAD_K = 1.4826  # MAD -> standard deviation for normal data


@dataclass
class DetectConfig:
    threshold: float = 1.0
    # rules
    gap_min: int = 15                 # missing data longer than this = dropout
    flat_window_min: int = 30         # raw frames unchanged this long = stuck
    clear_jump_frac: float = 0.25     # net (LED) clear moved this much vs last 24 h
    led_fail_frac: float = 0.10       # net clear below this fraction of normal = LEDs not lighting
    # baseline (per time-of-day slot)
    slot_min: int = 30
    history_days: int = 14
    min_history_days: int = 5
    hue_k: float = 4.0                # robust sigmas for score 1.0
    hue_floor_deg: float = 4.0        # never treat a spread below this as normal noise
    timing_tolerance_h: float = 0.5   # on CO2 ramps, allow this much schedule shift
    persist_min: int = 30             # baseline deviations must last this long
    ambient_k: float = 6.0
    ambient_floor: float = 60.0       # counts
    # daily summaries
    daily_k: float = 3.0
    guard_k: float = 1.5              # live overdose guard: spreads past the usual peak
    daily_ready_hour: float = 21.0    # judge a day's peak only once data reaches this local hour
    peak_floor_deg: float = 8.0
    night_floor_deg: float = 8.0
    swing_drop_frac: float = 0.30     # last 3 days' swing this much smaller than normal
    # suppress setup-type alerts this long around a calibration event
    calibration_quiet_h: float = 3.0
    weights: dict = field(default_factory=lambda: {
        "rule": 1.0, "baseline_hue": 1.0, "baseline_ambient": 1.0, "daily": 1.0})


# ---------------------------------------------------------------- helpers

def _runs(mask: pd.Series) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Start/end timestamps of consecutive True runs (end = last True slot)."""
    m = mask.fillna(False).astype(bool).to_numpy()
    if not m.any():
        return []
    idx = mask.index
    edges = np.diff(np.concatenate([[0], m.astype(int), [0]]))
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0] - 1
    return [(idx[s], idx[e]) for s, e in zip(starts, ends)]


def _epoch_start(index: pd.DatetimeIndex, events: list[pd.Timestamp]) -> pd.Series:
    """For each slot, the latest calibration event at or before it (or NaT)."""
    out = pd.Series(pd.NaT, index=index, dtype="datetime64[ns, UTC]")
    for ev in sorted(events or []):
        out[index >= ev] = ev
    return out


# ---------------------------------------------------------------- rules

def rule_scores(df: pd.DataFrame, cfg: DetectConfig) -> pd.DataFrame:
    """Hard faults. Score is 2.0 while a rule fires (forces an alert), else 0.
    Returns one column per rule plus ``info_<rule>`` text columns."""
    out = pd.DataFrame(index=df.index)
    slots = cfg.gap_min // SAMPLE_PERIOD_MIN

    # dropout: a run of missing slots longer than gap_min
    miss = df["missing"].astype(bool)
    run_id = (miss != miss.shift()).cumsum()
    run_len = miss.groupby(run_id).transform("sum")
    out["dropout"] = np.where(miss & (run_len > slots), 2.0, 0.0)
    if "i2c_errors" in df:
        inc = df["i2c_errors"].diff().fillna(0) > 0
        out["dropout"] = np.maximum(out["dropout"], np.where(inc, 2.0, 0.0))

    # flat-line: raw frames identical for flat_window_min
    w = add_window_features(df, cfg.flat_window_min) if "n_changes" not in df else df
    full = df[FRAME_COLS].notna().all(axis=1)
    window_full = full.rolling(f"{cfg.flat_window_min}min").sum() >= cfg.flat_window_min // 5
    out["stuck"] = np.where((w["n_changes"] == 0) & window_full, 2.0, 0.0)

    # saturation (computed mask or firmware flag)
    sat = df.get("saturated", pd.Series(False, index=df.index)).fillna(False).astype(bool)
    if "saturated_flag" in df:
        sat |= df["saturated_flag"].fillna(0) > 0
    out["saturated"] = np.where(sat, 2.0, 0.0)

    # sensor identity / configuration
    bad = pd.Series(False, index=df.index)
    if "id_ok" in df:
        bad |= df["id_ok"].fillna(1) < 0.5
    for c in ("atime", "gain"):
        if c in df:
            v = df[c].ffill()
            bad |= v.ne(v.shift()) & v.shift().notna()
    out["config"] = np.where(bad, 2.0, 0.0)

    # LED / net-signal checks against the previous 24 h (excluding the last hour)
    net = df["clear"].rolling("30min", min_periods=2).median()
    ref = df["clear"].shift(12).rolling("24h", min_periods=48).median()
    rel = net / ref
    out["led_fail"] = np.where(rel < cfg.led_fail_frac, 2.0, 0.0)
    jump = (rel - 1).abs() > cfg.clear_jump_frac
    out["clear_jump"] = np.where(jump & (rel >= cfg.led_fail_frac), 2.0, 0.0)
    out["clear_rel"] = rel
    if "seq" in df:
        out["seq_moving"] = df["seq"].diff(cfg.flat_window_min // 5).fillna(1) > 0
    return out


# ---------------------------------------------------------------- baseline

def _slot_baseline(series: pd.Series, local_slot: pd.Series, local_day: pd.Series,
                   epoch: pd.Series, cfg: DetectConfig) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Median, MAD and number of history days of the same local time slot over
    the previous ``history_days`` days, same calibration epoch, today excluded."""
    frame = pd.DataFrame({"v": series, "slot": local_slot, "day": local_day, "ep": epoch})
    daily = frame.groupby(["ep", "slot", "day"], dropna=False)["v"].median().rename("v")
    daily = daily.reset_index()
    med = pd.Series(np.nan, index=series.index)
    mad = pd.Series(np.nan, index=series.index)
    nday = pd.Series(0.0, index=series.index)
    days_sorted = sorted(frame["day"].unique())
    day_pos = {d: i for i, d in enumerate(days_sorted)}
    daily["pos"] = daily["day"].map(day_pos)
    for (ep, slot), g in daily.groupby(["ep", "slot"], dropna=False):
        g = g.sort_values("pos")
        vals, pos = g["v"].to_numpy(), g["pos"].to_numpy()
        for p in pos:
            hist = vals[(pos < p) & (pos >= p - cfg.history_days) & np.isfinite(vals)]
            sel = (frame["slot"] == slot) & (frame["day"].map(day_pos) == p)
            sel &= frame["ep"].isna() if pd.isna(ep) else (frame["ep"] == ep)
            if len(hist):
                m = np.median(hist)
                med[sel] = m
                mad[sel] = np.median(np.abs(hist - m)) * MAD_K
                nday[sel] = len(hist)
    return med, mad, nday


def baseline_scores(df: pd.DataFrame, cfg: DetectConfig,
                    events: list[pd.Timestamp] | None = None) -> pd.DataFrame:
    local = df.index.tz_convert(TZ)
    slot = pd.Series(((local.hour * 60 + local.minute) // cfg.slot_min).to_numpy(), index=df.index)
    day = pd.Series(local.date, index=df.index)
    epoch = _epoch_start(df.index, events or [])
    out = pd.DataFrame(index=df.index)

    hue = df["hue"].rolling("30min", min_periods=3).median()
    med, mad, nday = _slot_baseline(hue, slot, day, epoch, cfg)
    # On the steep CO2 ramps a small shift in timing is a big hue difference;
    # widen the allowed spread by the current slope x the timing tolerance.
    slope = (hue - hue.shift(6)).abs() / 0.5
    spread = np.maximum(mad.fillna(0), cfg.hue_floor_deg)
    allowed = cfg.hue_k * spread + slope.fillna(0) * cfg.timing_tolerance_h
    dev = hue - med
    ok = nday >= cfg.min_history_days
    raw = pd.Series(np.where(ok, dev.abs() / allowed, np.nan), index=df.index)
    # must persist: the lowest score over the last persist_min minutes
    out["baseline_hue"] = raw.rolling(f"{cfg.persist_min}min", min_periods=1).min() \
        .where(raw.notna())
    out["hue_now"], out["hue_normal"], out["hue_dev"] = hue, med, dev
    out["hue_allowed"] = allowed.where(ok)    # |dev| at which baseline_hue reaches 1.0

    if "ambient_clear" in df:
        amb = df["ambient_clear"].rolling("30min", min_periods=3).median()
        # Ambient light does not change when the fluid is refreshed, so this
        # baseline ignores calibration events (a moved sensor shows up anyway).
        no_epoch = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
        amed, amad, anday = _slot_baseline(amb, slot, day, no_epoch, cfg)
        aspread = np.maximum(amad.fillna(0), cfg.ambient_floor)
        adev = amb - amed
        aok = anday >= cfg.min_history_days
        # only increases matter (leak, moved sensor); a cloudy day is not a fault
        araw = pd.Series(np.where(aok, adev.clip(lower=0) / (cfg.ambient_k * aspread), np.nan),
                         index=df.index)
        out["baseline_ambient"] = araw.rolling(f"{cfg.persist_min}min", min_periods=1).min() \
            .where(araw.notna())
        out["ambient_now"], out["ambient_normal"] = amb, amed
    return out


# ---------------------------------------------------------------- daily summaries

def daily_scores(df: pd.DataFrame, cfg: DetectConfig, events: list[pd.Timestamp] | None = None,
                 cal: CalibrationConfig | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-day peak (greenest stable hue), night low (bluest) and swing, each
    against the median of earlier days in the same calibration epoch. Scores are
    spread over the window that the summary describes."""
    cal = cal or CalibrationConfig()
    anchors = extract_daily_anchors(df, cal)
    anchors["swing"] = anchors["night_hue"] - anchors["plateau_hue"]
    ev = sorted(events or [])
    first_ts = df.groupby("local_date").apply(lambda d: d.index[0], include_groups=False)
    anchors["epoch"] = [max([e for e in ev if e <= first_ts[d]], default=pd.NaT)
                        for d in anchors["local_date"]]
    rows = []
    for i, r in anchors.iterrows():
        same = anchors.iloc[:i]
        same = same[(same["epoch"].isna() if pd.isna(r["epoch"]) else same["epoch"] == r["epoch"])
                    & same["valid"]].tail(cfg.history_days)
        rec = {"local_date": r["local_date"], "n_hist": len(same)}
        if len(same) >= cfg.min_history_days:
            # the usual daily peak, known before today starts (for the live guard)
            h = same["plateau_hue"].dropna()
            rec["guard_normal"] = h.median()
            rec["guard_spread"] = max(np.median(np.abs(h - h.median())) * MAD_K,
                                      cfg.peak_floor_deg)
        day_rows = df[df["local_date"] == r["local_date"]]
        ready = len(day_rows) and day_rows["local_hour"].iloc[-1] >= cfg.daily_ready_hour
        if len(same) >= cfg.min_history_days and r["valid"] and ready:
            def z(col, floor):
                h = same[col].dropna()
                m = h.median()
                s = max(np.median(np.abs(h - m)) * MAD_K, floor)
                return r[col] - m, m, s
            dp, mp, sp = z("plateau_hue", cfg.peak_floor_deg)
            dn, mn, sn = z("night_hue", cfg.night_floor_deg)
            rec.update(peak_dev=dp, peak_normal=mp, peak_score=abs(dp) / (cfg.daily_k * sp),
                       night_dev=dn, night_normal=mn,
                       night_score=max(-dn, 0) / (cfg.daily_k * sn),  # only "not blue enough" matters
                       peak_hue=r["plateau_hue"], night_hue=r["night_hue"])
            recent = anchors.iloc[max(0, i - 2):i + 1]["swing"].median()
            normal_swing = same["swing"].median()
            drop = 1 - recent / normal_swing if normal_swing > 0 else np.nan
            rec.update(swing_now=recent, swing_normal=normal_swing,
                       swing_score=max(drop, 0) / cfg.swing_drop_frac if np.isfinite(drop) else
                       np.nan)
        rows.append(rec)
    summ = pd.DataFrame(rows)

    cols = ["daily_peak", "daily_night", "daily_swing", "peak_hue", "peak_normal", "peak_dev",
            "night_hue", "night_normal", "swing_now", "swing_normal", "beyond_peak"]
    out = pd.DataFrame(np.nan, index=df.index, columns=cols)
    local_hour = df["local_hour"]
    # Live overdose guard: hue greener/yellower than the usual daily peak, at
    # any time of day, sustained for persist_min. Does not wait for the day.
    hue = df["hue"].rolling("30min", min_periods=3).median()
    for _, s in summ.iterrows():
        if pd.isna(s.get("guard_normal", np.nan)):
            continue
        day = df["local_date"] == s["local_date"]
        excess = (s["guard_normal"] - hue[day]).clip(lower=0) / (cfg.guard_k * s["guard_spread"])
        out.loc[day, "beyond_peak"] = excess.rolling(f"{cfg.persist_min}min", min_periods=1).min()
        out.loc[day, "peak_normal"] = s["guard_normal"]
    for _, s in summ.iterrows():
        if "peak_score" not in s or pd.isna(s.get("peak_score")):
            continue
        day = df["local_date"] == s["local_date"]
        dayt, night = day & (local_hour >= 10), day & (local_hour < 10)
        out.loc[dayt, "daily_peak"] = s["peak_score"]
        out.loc[dayt, "daily_swing"] = s["swing_score"]
        out.loc[night, "daily_night"] = s["night_score"]
        for c in ("peak_hue", "peak_normal", "peak_dev", "swing_now", "swing_normal"):
            out.loc[dayt, c] = s[c]
        for c in ("night_hue", "night_normal"):
            out.loc[night, c] = s[c]
    return out, summ


# ---------------------------------------------------------------- combine

RULE_COLS = ("dropout", "stuck", "saturated", "config", "led_fail", "clear_jump")
DAILY_COLS = ("daily_peak", "daily_night", "daily_swing")
LIVE_COLS = ("beyond_peak",)


@dataclass
class Detection:
    scores: pd.DataFrame        # per slot: every component + combined score
    alerts: pd.DataFrame        # start, end, kind, score, reason, details
    daily: pd.DataFrame         # daily summaries


def detect(df: pd.DataFrame, cfg: DetectConfig | None = None,
           events: list[pd.Timestamp] | None = None) -> Detection:
    """Run every detector on a prepared frame (features.prepare output plus
    optional health/context columns)."""
    cfg = cfg or DetectConfig()
    w = add_window_features(df, cfg.flat_window_min)
    rules = rule_scores(w, cfg)
    base = baseline_scores(df, cfg, events)
    daily, summ = daily_scores(df, cfg, events)
    s = pd.concat([rules, base, daily], axis=1)

    # quiet period after calibration events for setup-type detectors
    for ev in events or []:
        q = (s.index >= ev - pd.Timedelta(minutes=30)) & \
            (s.index < ev + pd.Timedelta(hours=cfg.calibration_quiet_h))
        s.loc[q, ["clear_jump", "baseline_ambient"]] = 0.0

    wts = cfg.weights
    comp = {c: s[c] * wts["rule"] for c in RULE_COLS}
    comp["baseline_hue"] = s["baseline_hue"] * wts["baseline_hue"]
    if "baseline_ambient" in s:
        comp["baseline_ambient"] = s["baseline_ambient"] * wts["baseline_ambient"]
    for c in DAILY_COLS + LIVE_COLS:
        comp[c] = s[c] * wts["daily"]
    cdf = pd.DataFrame(comp)
    s["score"] = cdf.max(axis=1, skipna=True).fillna(0.0)
    kind = cdf.fillna(-1).idxmax(axis=1)
    # a firing hard-fault rule names the alert: it is the most specific cause
    rules_df = cdf[list(RULE_COLS)].fillna(0)
    rule_kind = rules_df.idxmax(axis=1).where(rules_df.max(axis=1) >= cfg.threshold)
    s["kind"] = rule_kind.fillna(kind).where(s["score"] > 0)
    alerts = _make_alerts(s, df, cfg)
    return Detection(scores=s, alerts=alerts, daily=summ)


def _make_alerts(s: pd.DataFrame, df: pd.DataFrame, cfg: DetectConfig) -> pd.DataFrame:
    rows = []
    above = s["score"] >= cfg.threshold
    # merge runs separated by short gaps (<= 30 min)
    filled = above.copy()
    gaps = _runs(~above)
    for a, b in gaps:
        if (b - a) <= pd.Timedelta(minutes=30) and a > s.index[0] and b < s.index[-1]:
            filled[a:b] = True
    for start, end in _runs(filled):
        win = s.loc[start:end]
        hit = win[win["score"] >= cfg.threshold]
        kind = hit["kind"].mode().iloc[0] if len(hit) else win["kind"].mode().iloc[0]
        peak_t = win["score"].idxmax()
        reason, details = explain(kind, s.loc[peak_t], df.loc[start:end], win)
        end_t = end + pd.Timedelta(minutes=SAMPLE_PERIOD_MIN)
        # When the alert could first be raised live: the first slot where a
        # per-slot detector crossed the threshold; daily summaries only count
        # once the day's window is over.
        live = hit[~hit["kind"].astype(str).str.startswith("daily_")]
        known = (live.index[0] + pd.Timedelta(minutes=SAMPLE_PERIOD_MIN)) if len(live) else end_t
        rows.append({"start": start, "end": end_t, "known_at": known, "kind": kind,
                     "score": float(win["score"].max()), "reason": reason, "details": details})
    return pd.DataFrame(rows, columns=["start", "end", "known_at", "kind", "score", "reason",
                                       "details"])


# ---------------------------------------------------------------- reasons

def _local(t) -> str:
    return pd.Timestamp(t).tz_convert(TZ).strftime("%a %d %b %H:%M")


def _dur(win: pd.DataFrame) -> str:
    mins = int((win.index[-1] - win.index[0]).total_seconds() // 60) + SAMPLE_PERIOD_MIN
    return f"{mins // 60} h {mins % 60:02d} min" if mins >= 60 else f"{mins} min"


def explain(kind: str, row: pd.Series, raw: pd.DataFrame, win: pd.DataFrame) -> tuple[str, dict]:
    """Templated plain-language reason. Only numbers computed above are used;
    likely causes are worded as possibilities, never as certainties."""
    d: dict = {"kind": kind, "from": _local(win.index[0]), "duration": _dur(win)}
    f = lambda x: None if x is None or pd.isna(x) else round(float(x), 1)  # noqa: E731
    if kind == "dropout":
        txt = (f"No sensor data for {_dur(win)} from {d['from']}. Likely causes: D1 offline, "
               f"Wi-Fi, scrape failure, or I2C errors on the sensor bus.")
    elif kind == "stuck":
        moving = bool(win.get("seq_moving", pd.Series([True])).any())
        cause = ("the reading counter keeps moving, so the sensor or I2C bus is returning "
                 "frozen values" if moving else
                 "the reading counter also stopped, so the firmware may have hung")
        txt = f"Raw sensor values have not changed for {_dur(win)} from {d['from']}: {cause}."
    elif kind == "saturated":
        txt = (f"Sensor channels at their maximum count ({_dur(win)} from {d['from']}); colour "
               f"readings are not usable. Possible causes: strong stray light, LEDs too bright, "
               f"or gain/ATIME changed.")
    elif kind == "config":
        txt = (f"Sensor identity or settings changed at {d['from']} (ID check failed, or gain/"
               f"ATIME changed). The baseline is no longer comparable; mark a calibration event "
               f"if this was intended.")
    elif kind == "led_fail":
        d["net_clear_rel"] = f(row.get("clear_rel"))
        txt = (f"The LED-lit signal dropped to {d['net_clear_rel']}x its usual level from "
               f"{d['from']}. Possible causes: illumination LEDs not switching (PCF8574, wiring) "
               f"or the sensor pointing away from the checker.")
    elif kind == "clear_jump":
        rel = row.get("clear_rel")
        d["net_clear_rel"] = f(rel)
        direction = "dropped" if rel is not None and rel < 1 else "rose"
        txt = (f"The LED-lit signal {direction} to {d['net_clear_rel']}x its usual level at "
               f"{d['from']} and stayed there {_dur(win)}. Possible causes: sensor case slipped or "
               f"moved, or something between sensor and checker. If you refilled the checker, "
               f"mark a calibration event.")
    elif kind == "baseline_ambient":
        d.update(ambient_now=f(row.get("ambient_now")), ambient_normal=f(row.get("ambient_normal")))
        txt = (f"Stray light (LEDs-off frame) at {d['ambient_now']} counts vs a usual "
               f"{d['ambient_normal']} for this time of day ({_dur(win)} from {d['from']}). "
               f"Possible causes: light leak, sensor moved, new light source nearby. The colour "
               f"reading itself is ambient-corrected.")
    elif kind == "baseline_hue":
        dev = row.get("hue_dev")
        d.update(hue_now=f(row.get("hue_now")), hue_normal=f(row.get("hue_normal")),
                 hue_dev=f(dev))
        more = dev is not None and dev < 0
        txt = (f"Drop checker hue {d['hue_now']} deg vs a usual {d['hue_normal']} deg for this "
               f"time of day ({abs(d['hue_dev'] or 0):.0f} deg "
               f"{'greener/yellower = more CO2' if more else 'bluer = less CO2'}), "
               f"{_dur(win)} from {d['from']}. "
               + ("Check for overdose risk: bubble rate, regulator, solenoid."
                  if more else "Possible causes: CO2 bottle low, regulator, diffuser, or "
                  "solenoid not opening."))
    elif kind == "daily_peak":
        dev = row.get("peak_dev")
        d.update(peak_hue=f(row.get("peak_hue")), peak_normal=f(row.get("peak_normal")))
        if dev is not None and dev > 0:
            txt = (f"The CO2 peak on {d['from'][:10]} only reached hue {d['peak_hue']} deg vs a "
                   f"usual peak of {d['peak_normal']} deg: CO2 stayed lower (bluer) than normal. "
                   f"Check the bottle, regulator, diffuser and solenoid.")
        else:
            txt = (f"The CO2 peak on {d['from'][:10]} went to hue {d['peak_hue']} deg vs a usual "
                   f"peak of {d['peak_normal']} deg: more CO2 than normal (greener/yellower). "
                   f"Check the bubble rate; risk to fish if it rises further.")
    elif kind == "beyond_peak":
        d.update(hue_now=f(row.get("hue_now")), peak_normal=f(row.get("peak_normal")))
        txt = (f"Drop checker hue {d['hue_now']} deg is past the usual daily CO2 peak of "
               f"{d['peak_normal']} deg (greener/yellower = more CO2), {_dur(win)} from "
               f"{d['from']}. Possible overdose: check the bubble rate and regulator; "
               f"watch the fish.")
    elif kind == "daily_night":
        d.update(night_hue=f(row.get("night_hue")), night_normal=f(row.get("night_normal")))
        txt = (f"Overnight to {d['from'][:10]} the checker only got to hue {d['night_hue']} deg "
               f"vs a usual {d['night_normal']} deg: CO2 did not drop as usual. Possible causes: "
               f"CO2 solenoid stuck open, air stone not running, or the CO2 schedule changed.")
    elif kind == "daily_swing":
        d.update(swing_now=f(row.get("swing_now")), swing_normal=f(row.get("swing_normal")))
        txt = (f"The daily colour swing between night and peak is {d['swing_now']} deg over the "
               f"last 3 days vs a usual {d['swing_normal']} deg. A gradual cause is likely: "
               f"diffuser clogging, bubble counter, regulator drift. Fluid ageing alone would "
               f"shift night and day together rather than shrink the swing.")
    else:
        txt = f"Unusual readings from {d['from']} ({kind})."
    return txt, d
