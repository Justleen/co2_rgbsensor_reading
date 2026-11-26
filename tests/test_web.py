import json
import socket
import threading
import time
import urllib.request

import pandas as pd
import pytest

from co2watch import cli, db, vm
from co2watch.simulator import AnomalySpec, simulate
from co2watch.web import build_status, serve


@pytest.fixture(scope="module")
def sim():
    return simulate(days=10, seed=4, anomalies=[AnomalySpec("stuck", day=9, hour=8,
                                                            duration_h=3)])


def test_build_status_shapes_and_valid_json(tmp_path, sim):
    con = db.connect(tmp_path / "t.sqlite")
    raw = sim.readings
    now = raw.index[-1] + pd.Timedelta(minutes=5)
    db.log_alert(con, now - pd.Timedelta(hours=3), now - pd.Timedelta(minutes=10), 2.0,
                 "stuck", "Raw sensor values have not changed")
    run = db.job_started(con, "archive")
    db.job_finished(con, run, True, "3 days aggregated")
    st = build_status(con, raw, now)
    json.dumps(st, allow_nan=False)   # NaN must have become null
    assert st["errors"] == []
    assert len(st["series"]["t"]) == 48 * 12
    assert len(st["series"]["hue"]) == len(st["series"]["lo"]) == len(st["series"]["t"])
    assert st["latest"]["age_min"] == 5
    assert st["latest"]["co2_mg_l"] is not None and st["latest"]["band"]
    assert st["learning"]["ready"]
    assert st["alerts"][0]["open"] and st["alerts"][0]["kind"] == "stuck"
    jobs = {j["job"]: j for j in st["jobs"]}
    assert jobs["archive"]["state"] == "ok"   # just ran (real clock vs sim time: no lateness)
    assert jobs["backup"]["state"] == "never"
    assert {b["name"] for b in st["bands"]} >= {"blue", "green"}


def test_build_status_without_readings(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    st = build_status(con, None, pd.Timestamp.now(tz="UTC"), errors=["VM down"])
    assert st["latest"] is None and st["series"] is None
    assert st["errors"] == ["VM down"] and st["alerts"] == [] and st["daily"] == []


def test_job_runs_are_recorded_by_the_cli(tmp_path):
    path = tmp_path / "t.sqlite"
    cli.main(["backup", "--db", str(path), "--dir", str(tmp_path / "bk")])
    with pytest.raises(Exception):
        cli.main(["calibrate", "--csv", str(tmp_path / "missing.csv"), "--db", str(path)])
    runs = {r["job"]: r for r in db.last_job_runs(db.connect(path))}
    assert runs["backup"]["ok"] == 1 and runs["backup"]["summary"].startswith("wrote ")
    assert runs["calibrate"]["ok"] == 0 and runs["calibrate"]["fails_since_ok"] == 1


def test_late_and_failed_job_states(tmp_path):
    con = db.connect(tmp_path / "t.sqlite")
    day = 24 * 3600 * 1000
    t = db.ms(pd.Timestamp.now(tz="UTC"))
    con.execute("INSERT INTO job_runs (job, started_ms, finished_ms, ok) VALUES "
                "('calibrate', ?, ?, 1)", (t - 3 * day, t - 3 * day))
    con.execute("INSERT INTO job_runs (job, started_ms, finished_ms, ok, summary) VALUES "
                "('archive', ?, ?, 0, 'boom')", (t - 1000, t))
    con.commit()
    jobs = {j["job"]: j for j in build_status(con, None, pd.Timestamp.now(tz="UTC"))["jobs"]}
    assert jobs["calibrate"]["state"] == "late"
    assert jobs["archive"]["state"] == "failed" and jobs["archive"]["summary"] == "boom"


def test_http_server_smoke(tmp_path, sim):
    csv = tmp_path / "r.csv"
    vm.frames_to_csv(sim.readings, csv)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=serve, args=("127.0.0.1", port, str(tmp_path / "t.sqlite")),
                     kwargs={"csv": str(csv)}, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    page = urllib.request.urlopen(base + "/").read().decode()
    assert "api/status" in page
    st = json.loads(urllib.request.urlopen(base + "/api/status", timeout=60).read())
    assert st["latest"]["age_min"] == 5 and st["series"]["t"]


def test_score_once_logs_alerts_records_run_and_builds_status(tmp_path, sim):
    from co2watch.web import score_once
    path = str(tmp_path / "t.sqlite")
    raw = sim.readings
    stuck_end = pd.Timestamp(sim.labels.query("kind == 'stuck'")["end"].iloc[0])
    now = stuck_end + pd.Timedelta(minutes=30)
    ok, msg, st = score_once(path, lambda: (raw, now, []))
    assert ok and "new alert" in msg
    json.dumps(st, allow_nan=False)
    con = db.connect(path)
    assert "stuck" in set(db.read_alerts(con)["kind"])
    assert {j["job"]: j for j in st["jobs"]}["score"]["state"] == "ok"
    ok, msg, _ = score_once(path, lambda: (None, now, ["VM down"]))
    assert not ok and msg == "VM down"
    assert db.last_job_runs(con)[-1]["fails_since_ok"] == 1
