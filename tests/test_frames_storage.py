import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd
import pytest

from co2watch import db, vm
from co2watch.config import saturation_limit
from co2watch.features import combine_frames, prepare
from co2watch.simulator import AnomalySpec, simulate


# ---------------------------------------------------------------- two frames

def test_ambient_subtraction_removes_tank_light_and_sun():
    res = simulate(days=4, seed=1)
    df = prepare(res.readings)
    lit = df["light_on"].astype(bool)
    # raw on-frame clear jumps with the tank light; the net signal does not
    assert df.loc[lit, "clear_on"].median() > 1.15 * df.loc[~lit, "clear_on"].median()
    assert df.loc[lit, "clear"].median() == pytest.approx(df.loc[~lit, "clear"].median(),
                                                          rel=0.02)
    assert df.loc[lit, "ambient_clear"].median() > 10 * df.loc[~lit, "ambient_clear"].median()


def test_light_leak_shows_in_ambient_not_in_hue():
    res = simulate(days=4, seed=2, anomalies=[AnomalySpec("light_leak", day=2, hour=0,
                                                          duration_h=24)])
    df = prepare(res.readings)
    day1 = df[df["local_date"].astype(str) == "2026-10-13"]
    day2 = df[df["local_date"].astype(str) == "2026-10-14"]
    night = lambda d: d[d["local_hour"] < 6]
    assert night(day2)["ambient_clear"].median() > 10 * night(day1)["ambient_clear"].median()
    assert abs(night(day2)["hue"].median() - night(day1)["hue"].median()) < 3


def test_saturated_readings_are_masked():
    raw = pd.DataFrame({
        "red_off": [10, 10], "green_off": [10, 10], "blue_off": [10, 10], "clear_off": [30, 30],
        "red_on": [500, 4096], "green_on": [700, 900], "blue_on": [900, 950],
        "clear_on": [2100, 2500],
    }, index=pd.date_range("2026-11-01", periods=2, freq="5min", tz="UTC"))
    out = combine_frames(raw, saturation=saturation_limit(252))  # limit 4096
    assert list(out["saturated"]) == [False, True]
    assert out["red"].iloc[0] == 490 and np.isnan(out["red"].iloc[1])
    assert saturation_limit(0) == 65535 and saturation_limit(246) == 10240


# ---------------------------------------------------------------- VictoriaMetrics

class FakeVM(BaseHTTPRequestHandler):
    """Just enough of VM: /api/v1/import/prometheus and /api/v1/export."""
    store: dict = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        for line in body.splitlines():
            series, val, ts = line.rsplit(" ", 2)
            name, labels = series.split("{", 1)
            labels = dict(kv.split("=", 1) for kv in labels.rstrip("}").split(","))
            labels = {k: v.strip('"') for k, v in labels.items()}
            key = json.dumps({"__name__": name, **labels}, sort_keys=True)
            self.store.setdefault(key, []).append((int(ts), float(val)))
        self.send_response(204); self.end_headers()

    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        match = q["match[]"][0]
        name, sel = match.split("{", 1)
        want = dict(kv.split("=", 1) for kv in sel.rstrip("}").split(",") if kv)
        want = {k: v.strip('"') for k, v in want.items()}
        t0, t1 = int(q["start"][0]) * 1000, int(q["end"][0]) * 1000
        out = []
        for key, pts in self.store.items():
            m = json.loads(key)
            if m["__name__"] != name:
                continue
            if any(m.get(k, "") != v for k, v in want.items()):
                continue
            pts = sorted(p for p in pts if t0 <= p[0] <= t1)
            out.append(json.dumps({"metric": m, "values": [p[1] for p in pts],
                                   "timestamps": [p[0] for p in pts]}))
        body = ("\n".join(out) + "\n").encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)


@pytest.fixture()
def fake_vm():
    FakeVM.store = {}
    srv = HTTPServer(("127.0.0.1", 0), FakeVM)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_vm_import_and_read_roundtrip(fake_vm):
    res = simulate(days=2, seed=3)
    n = vm.import_frames(fake_vm, res.readings, source="sim")
    assert n > 0
    start, end = res.readings.index[0], res.readings.index[-1]
    back = vm.read_all(fake_vm, start, end, source="sim")
    assert len(back) == len(res.readings)
    cols = ["red_off", "clear_on"]
    assert np.allclose(back[cols].to_numpy(), res.readings[cols].to_numpy())
    assert (back["co2_on"] == res.readings["co2_on"]).all()
    # scraped data (no source label) is kept apart from simulated data
    assert vm.read_frames(fake_vm, start, end).empty or \
        vm.read_frames(fake_vm, start, end)[cols].isna().all().all()


def test_import_requires_source_label():
    res = simulate(days=1, seed=3)
    with pytest.raises(ValueError):
        vm.to_prometheus_lines(res.readings, source="")


def test_csv_roundtrip(tmp_path):
    res = simulate(days=2, seed=4)
    vm.frames_to_csv(res.readings, tmp_path / "x.csv")
    back = vm.frames_from_csv(tmp_path / "x.csv")
    assert len(back) == len(res.readings)
    assert back["co2_on"].dtype == bool
    assert np.allclose(back["clear_on"], res.readings["clear_on"])


# ---------------------------------------------------------------- SQLite (labels etc.)

def test_labels_events_alerts(tmp_path):
    con = db.connect(tmp_path / "l.sqlite")
    t = pd.Timestamp("2026-11-20 14:00", tz="Europe/Amsterdam")
    db.add_label(con, t, t + pd.Timedelta("1h"), "normal")
    db.add_label(con, t, t + pd.Timedelta("10min"), "eye_check", band="green")
    with pytest.raises(ValueError):
        db.add_label(con, t, t, "eye_check")
    with pytest.raises(ValueError):
        db.add_label(con, t, t, "maybe")
    db.add_calibration_event(con, t, "fluid_change")
    assert db.calibration_events(con) == [t.tz_convert("UTC")]
    db.log_alert(con, t, None, 0.9, "flatline", "Sensor values have not changed for 40 min.")
    assert db.read_alerts(con)["reason"].iloc[0].startswith("Sensor")
    eye = db.eye_checks(con, lambda s, e: 110.0)
    assert list(eye["band"]) == ["green"] and eye["sensor_hue"].iloc[0] == 110.0
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "readings" not in tables  # raw data lives in VictoriaMetrics


def test_backup_and_save_anchors(tmp_path):
    from co2watch.calibration import extract_daily_anchors
    con = db.connect(tmp_path / "l.sqlite")
    anchors = extract_daily_anchors(prepare(simulate(days=3, seed=1).readings))
    assert db.save_anchors(con, anchors) == len(anchors)
    assert db.save_anchors(con, anchors) == len(anchors)  # upsert, no duplicates
    assert con.execute("SELECT COUNT(*) FROM anchors").fetchone()[0] == len(anchors)
    for _ in range(3):
        db.backup(con, tmp_path / "bk", keep=2)
        import time; time.sleep(1.1)
    files = sorted((tmp_path / "bk").glob("*.sqlite"))
    assert len(files) == 2
    import sqlite3
    assert sqlite3.connect(files[-1]).execute("SELECT COUNT(*) FROM anchors").fetchone()[0] \
        == len(anchors)
