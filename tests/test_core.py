import numpy as np
import pandas as pd
import pytest

from co2watch import db
from co2watch.calibration import (
    chart_hue_to_co2, drift_report, extract_daily_anchors, fit_calibration)
from co2watch.config import CHART_KH4, co2_from_ph
from co2watch.features import (
    add_basic_features, add_window_features, hue_from_rgb, minutes_since_state_on,
    to_regular_grid)
from co2watch.simulator import AnomalySpec, SensorModel, default_scenario, simulate


@pytest.fixture(scope="module")
def clean_sim():
    return simulate(days=21, seed=3)


def feats(res):
    return add_basic_features(to_regular_grid(res.readings))


# ---------------------------------------------------------------- chart

def test_chart_matches_kh4_formula():
    for band in CHART_KH4:
        assert co2_from_ph(band.ph) == pytest.approx(band.co2_mg_l, rel=0.06)


def test_chart_hue_to_co2_roundtrip_and_monotonic():
    for band in CHART_KH4:
        assert chart_hue_to_co2(band.chart_hue_deg) == pytest.approx(band.co2_mg_l, rel=1e-6)
    hues = np.linspace(40, 230, 50)
    assert np.all(np.diff(chart_hue_to_co2(hues)) <= 0)


# ---------------------------------------------------------------- features

def test_hue_known_colours():
    assert hue_from_rgb(255, 0, 0) == pytest.approx(0)
    assert hue_from_rgb(0, 255, 0) == pytest.approx(120)
    assert hue_from_rgb(0, 0, 255) == pytest.approx(240)
    assert np.isnan(hue_from_rgb(10, 10, 10))
    assert np.isnan(hue_from_rgb(np.nan, 1, 2))


def test_dst_day_has_25_hours_of_slots(clean_sim):
    df = feats(clean_sim)
    per_day = df.groupby("local_date").size()
    dst_end = pd.Timestamp("2026-10-25").date()  # clocks go back in Amsterdam
    assert per_day[dst_end] == 25 * 12
    assert per_day.drop(dst_end).iloc[1:-1].eq(24 * 12).all()
    # UTC index stays strictly increasing and unique
    assert df.index.is_monotonic_increasing and df.index.is_unique


def test_gaps_are_marked_not_filled():
    res = simulate(days=3, seed=1, anomalies=[AnomalySpec("dropout", day=1, hour=2,
                                                          duration_h=2)])
    grid = to_regular_grid(res.readings)
    assert grid["missing"].sum() == 24
    assert grid.loc[grid["missing"], "red"].isna().all()


def test_window_features_detect_flatline():
    res = simulate(days=3, seed=1, anomalies=[AnomalySpec("stuck", day=1, hour=9,
                                                          duration_h=3)])
    df = add_window_features(feats(res))
    lab = res.labels.iloc[0]
    inside = df.loc[lab["start"] + pd.Timedelta("40min"): lab["end"] - pd.Timedelta("5min")]
    assert (inside["n_changes"] == 0).all()
    assert (df["n_changes"].drop(inside.index).iloc[10:] > 0).mean() > 0.99


def test_minutes_since_co2_on(clean_sim):
    df = clean_sim.readings
    states = df["co2_on"][df["co2_on"].ne(df["co2_on"].shift())]
    m = minutes_since_state_on(df.index, states)
    assert m[~df["co2_on"]].isna().all()
    assert m[df["co2_on"]].min() == 0
    assert m[df["co2_on"]].max() == pytest.approx(7 * 60 - 5)  # CO2 11:00-18:00


# ---------------------------------------------------------------- simulator

def test_daily_cycle_shape(clean_sim):
    t = clean_sim.truth
    daily_max = t["c_checker"].groupby(t.index.date).max().iloc[1:]
    daily_min = t["c_checker"].groupby(t.index.date).min().iloc[1:]
    assert daily_max.between(15, 30).all()
    assert daily_min.lt(4).all()


def test_scenario_labels_and_effects():
    res = simulate(days=42, seed=5, anomalies=default_scenario())
    assert set(res.labels["kind"]) >= {"stuck", "missed_co2", "overdose", "diffuser_decline"}
    t = res.truth["c_checker"]
    day = lambda d: t[(t.index >= pd.Timestamp("2026-10-12", tz="Europe/Amsterdam")
                       + pd.Timedelta(days=d))
                      & (t.index < pd.Timestamp("2026-10-12", tz="Europe/Amsterdam")
                         + pd.Timedelta(days=d + 1))]
    normal_peak = day(10).max()
    assert day(18).max() < 0.5 * normal_peak       # missed CO2
    assert day(22).max() > 1.5 * normal_peak       # overdose
    assert day(27).min() > 2 * day(10).min()       # no overnight drop


# ---------------------------------------------------------------- calibration

def test_anchors_and_fit_recover_hidden_offset(clean_sim):
    anchors = extract_daily_anchors(feats(clean_sim))
    assert anchors["valid"].iloc[1:].all()
    fit = fit_calibration(anchors)
    assert fit.status == "ok"
    # night anchor maps to ~3 mg/l
    night = anchors["night_hue"].median()
    assert fit.co2_estimate(night)["co2_mg_l"] == pytest.approx(3.0, rel=0.35)
    # prior-only scale: the daily peak interval must contain the truth (~21 mg/l)
    peak = anchors["plateau_hue"].median()
    est = fit.co2_estimate(peak)
    truth = clean_sim.truth["c_checker"].groupby(clean_sim.truth.index.date).max().median()
    assert est["low"] <= truth <= est["high"]


def test_eye_checks_tighten_the_fit(clean_sim):
    anchors = extract_daily_anchors(feats(clean_sim))
    peak = anchors["plateau_hue"].median()
    eye = pd.DataFrame({"sensor_hue": [peak, peak + 1], "band": ["green", "green"]})
    loose = fit_calibration(anchors).co2_estimate(peak)
    tight = fit_calibration(anchors, eye).co2_estimate(peak)
    assert (tight["high"] - tight["low"]) < 0.5 * (loose["high"] - loose["low"])
    assert tight["co2_mg_l"] == pytest.approx(20, rel=0.25)


def test_calibration_event_resets_fit():
    sensor = SensorModel(ageing_deg_per_day=-0.5)
    res = simulate(days=20, seed=4, sensor=sensor,
                   anomalies=[AnomalySpec("fluid_change", day=14, hour=10)])
    anchors = extract_daily_anchors(feats(res))
    ev = [res.labels.iloc[0]["start"]]
    fit = fit_calibration(anchors, calibration_events=ev)
    assert fit.n_night <= 6
    assert fit.status in ("settling", "ok")
    before = fit_calibration(anchors.iloc[:14])
    # fresh fluid is bluer again than the aged fluid just before the change
    assert anchors["night_hue"].iloc[16:].median() > anchors["night_hue"].iloc[10:14].median()
    assert fit.a != pytest.approx(before.a, abs=1.0)


def test_drift_report_ageing_vs_diffuser():
    ageing = simulate(days=21, seed=6, sensor=SensorModel(ageing_deg_per_day=-0.6))
    rep = drift_report(extract_daily_anchors(feats(ageing)))
    assert rep["pattern"] == "whole_scale_shift"
    diffuser = simulate(days=21, seed=6, anomalies=[AnomalySpec("diffuser_decline", day=8,
                                                                duration_days=10)])
    rep = drift_report(extract_daily_anchors(feats(diffuser)))
    assert rep["pattern"] == "daily_swing_shrinking"


# ---------------------------------------------------------------- db

def test_db_roundtrip(tmp_path, clean_sim):
    con = db.connect(tmp_path / "t.sqlite")
    r = clean_sim.readings.iloc[:500]
    assert db.insert_readings(con, r) == 500
    assert db.insert_readings(con, r) == 0  # append-only, no duplicates
    db.insert_context_changes(con, r["co2_on"], "co2_on")
    back = db.read_readings(con)
    assert len(back) == 500 and back.index.tz is not None
    co2 = db.context_on_grid(con, "co2_on", back.index)
    assert (co2.to_numpy() == r["co2_on"].to_numpy()).all()
    n = db.export_csv(con, tmp_path / "out.csv")
    assert n == 500
    out = pd.read_csv(tmp_path / "out.csv")
    assert {"ts", "local_time", "red", "co2_on"} <= set(out.columns)
    con2 = db.connect(tmp_path / "t2.sqlite")
    assert db.import_csv(con2, tmp_path / "out.csv") == 500
