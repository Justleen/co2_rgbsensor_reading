import pandas as pd
import pytest

from co2watch import db
from co2watch.detect import DetectConfig, detect
from co2watch.evaluate import evaluate, match
from co2watch.features import prepare
from co2watch.simulator import AnomalySpec, simulate


def run(anomalies, days=21, seed=7):
    res = simulate(days=days, seed=seed, anomalies=anomalies)
    events = list(res.labels.loc[res.labels["category"] == "calibration_event", "start"])
    det = detect(prepare(res.readings), DetectConfig(), events)
    return res, det, match(det.alerts, res.labels)


@pytest.mark.parametrize("spec,expected", [
    (AnomalySpec("stuck", day=15, hour=9, duration_h=3), "stuck"),
    (AnomalySpec("dropout", day=15, hour=2, duration_h=2), "dropout"),
    (AnomalySpec("case_slipped", day=15, hour=10, duration_h=6), "clear_jump"),
    (AnomalySpec("light_leak", day=15, hour=0, duration_h=24), "baseline_ambient"),
    (AnomalySpec("missed_co2", day=15), "daily_peak"),
])
def test_each_anomaly_is_caught_by_the_expected_detector(spec, expected):
    res, det, m = run([spec])
    hits = m[m["matched"] == spec.kind]
    assert len(hits), det.alerts
    assert expected in set(hits["kind"])
    assert m["matched"].notna().all(), m[m["matched"].isna()]  # no false alarms


def test_cold_start_flags_only_hard_faults():
    # an overdose on day 2: no baseline yet, so nothing may fire; a stuck sensor still does
    res, det, m = run([AnomalySpec("overdose", day=2),
                       AnomalySpec("stuck", day=3, hour=9, duration_h=2)], days=5)
    assert set(det.alerts["kind"]) == {"stuck"}


def test_fluid_change_does_not_alert_and_resets_hue_baseline():
    res, det, m = run([AnomalySpec("fluid_change", day=12, hour=10)], days=20)
    assert det.alerts.empty, det.alerts
    s = det.scores
    after = s.loc[res.labels.iloc[0]["start"]:].iloc[:12 * 24 * 3]
    assert after["baseline_hue"].isna().all()       # silent while the new baseline builds


def test_reasons_are_templated_with_numbers():
    res, det, m = run([AnomalySpec("missed_co2", day=15)])
    r = det.alerts.loc[det.alerts["kind"] == "daily_peak", "reason"].iloc[0]
    assert "usual peak" in r and "deg" in r and "Check the bottle" in r


def test_evaluate_smoke():
    r = evaluate(seeds=(1,), days=28, clean=True)
    assert set(r.per_kind["kind"]) >= {"stuck", "missed_co2"}
    assert r.per_kind["recall"].min() >= 0.0
    assert r.false_alarms["per_week"].mean() < 1.0


def test_upsert_alert_merges(tmp_path):
    con = db.connect(tmp_path / "a.sqlite")
    t = pd.Timestamp("2026-11-20 10:00", tz="UTC")
    i1, c1 = db.upsert_alert(con, t, t + pd.Timedelta("20min"), 1.5, "stuck", "x")
    i2, c2 = db.upsert_alert(con, t, t + pd.Timedelta("40min"), 2.0, "stuck", "y")
    i3, c3 = db.upsert_alert(con, t, t + pd.Timedelta("40min"), 2.0, "dropout", "z")
    assert c1 and not c2 and c3 and i1 == i2 != i3
    al = db.read_alerts(con)
    assert len(al) == 2 and al.loc[al["id"] == i1, "score"].iloc[0] == 2.0
