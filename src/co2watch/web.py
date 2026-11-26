"""Small status page: ``co2watch serve``.

One process, standard library HTTP server, no extra dependencies. It serves a
single static page (``static/status.html``) and one JSON endpoint that the
page polls:

* ``GET /``             the page
* ``GET /api/status``   everything the page shows (see ``build_status``)
* ``GET /healthz``      "ok" (for a liveness probe)

The JSON is recomputed at most every ``cache_s`` seconds, whatever the number
of open browsers, so the cost on lap1 is one VictoriaMetrics read plus one
detection pass every couple of minutes while someone is looking, and nothing
when nobody is. The page is read-only: it never writes labels or alerts.
"""

from __future__ import annotations

import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

import numpy as np
import pandas as pd

from . import db, vm
from .config import CHART_KH4, TZ

# How often each scheduled job is expected to succeed (hours); a job whose last
# success is older than 1.5x this is shown as late.
JOB_EVERY_H = {"calibrate": 24, "archive": 24, "backup": 24, "score": 5 / 60, "train": 24 * 7}
ALWAYS_SHOWN = ("calibrate", "archive", "backup")
STALE_MIN = 15          # readings older than this: the page shows a warning
OPEN_ALERT_MIN = 30     # an alert whose end is this recent counts as still open


def _num(x, nd=2):
    """float -> rounded float, NaN/None/inf -> None (valid JSON)."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if math.isfinite(f) else None


def _ms(t) -> int | None:
    return None if t is None or pd.isna(t) else int(pd.Timestamp(t).timestamp() * 1000)


def _col(df: pd.DataFrame, name: str, nd=2) -> list:
    if name not in df:
        return [None] * len(df)
    return [_num(v, nd) for v in df[name].to_numpy()]


def _light(df: pd.DataFrame) -> list:
    """Dimmer level in %, or the on/off state as 0/100 when there is no dimmer series."""
    if "light_pct" in df and df["light_pct"].notna().any():
        return _col(df, "light_pct", 0)
    if "light_on" in df:
        return [_num(v * 100, 0) for v in df["light_on"].astype(float).to_numpy()]
    return [None] * len(df)


def build_status(con, raw: pd.DataFrame | None, now: pd.Timestamp,
                 window_h: float = 48, errors: list[str] | None = None) -> dict:
    """Everything the status page shows, as plain JSON-able data.

    ``raw`` is the reading frame (vm.read_all / frames_from_csv), ideally 16
    days so the per-slot normal band has its 14 days of history. ``con`` is
    the SQLite connection (alerts, job runs, daily aggregates).
    """
    from .calibration import band_for_chart_hue, extract_daily_anchors, fit_calibration
    from .detect import DetectConfig, detect
    from .features import prepare

    errors = list(errors or [])
    out: dict = {"now_ms": _ms(now), "tz": str(TZ), "window_h": window_h, "errors": errors,
                 "latest": None, "series": None, "learning": None, "bands": []}
    events = [e for e in db.calibration_events(con) if e <= now]

    if raw is not None and len(raw):
        raw = raw[raw.index < now]
    if raw is not None and len(raw):
        try:
            df = prepare(raw)
            cfg = DetectConfig()
            det = detect(df, cfg, events)
            s = det.scores
            fit = None
            try:
                anchors = extract_daily_anchors(df)

                def hue_at(a, b):
                    return float(df.loc[a:b, "hue"].median())

                fit = fit_calibration(anchors, db.eye_checks(con, hue_at),
                                      calibration_events=events, now=now)
            except Exception as e:  # calibration is a nice-to-have on this page
                errors.append(f"calibration: {e}")

            win = s[s.index >= now - pd.Timedelta(hours=window_h)]
            dwin = df.loc[win.index]
            normal = win.get("hue_normal")
            allowed = win.get("hue_allowed")
            out["series"] = {
                "t": [_ms(t) for t in win.index],
                "hue": _col(win, "hue_now", 1),
                "normal": _col(win, "hue_normal", 1),
                "lo": [_num(v, 1) for v in (normal - allowed)] if normal is not None
                and allowed is not None else [],
                "hi": [_num(v, 1) for v in (normal + allowed)] if normal is not None
                and allowed is not None else [],
                "score": _col(win, "score", 2),
                "light_pct": _light(dwin),
                "threshold": cfg.threshold,
            }

            valid = df["hue"].dropna()
            last_t = valid.index[-1] if len(valid) else df.index[-1]
            row = df.loc[last_t]
            srow = s.loc[last_t]
            hue = _num(srow.get("hue_now"), 1) or _num(row.get("hue"), 1)
            latest = {
                "ts_ms": _ms(last_t),
                "age_min": _num((now - last_t).total_seconds() / 60, 0),
                "hue": hue,
                "co2_index_pct": _num(row.get("co2_index_pct"), 0),
                "light_pct": _num(row.get("light_pct"), 0),
                "i2c_errors": _num(row.get("i2c_errors"), 0),
                "saturated": bool(row.get("saturated", False)) if pd.notna(
                    row.get("saturated", np.nan)) else None,
                "score": _num(srow.get("score"), 2),
                "kind": srow.get("kind") if isinstance(srow.get("kind"), str) else None,
                "normal_hue": _num(srow.get("hue_normal"), 1),
            }
            if fit is not None and hue is not None:
                est = fit.co2_estimate(hue)
                latest.update({
                    "co2_mg_l": _num(est["co2_mg_l"], 0), "co2_low": _num(est["low"], 0),
                    "co2_high": _num(est["high"], 0),
                    "band": band_for_chart_hue(float(fit.chart_hue(hue))),
                    "calibration": fit.status})
                # leaflet bands as sensor hue, for the reference lines on the chart
                if abs(fit.b) > 1e-6:
                    out["bands"] = [{"name": b.name, "co2": _num(b.co2_mg_l, 0),
                                     "verdict": b.verdict,
                                     "hue": _num((b.chart_hue_deg - fit.a) / fit.b, 1)}
                                    for b in CHART_KH4]
            out["latest"] = latest

            # how far the per-slot normal is in learning it (same epoch only)
            epoch0 = max(events, default=None)
            hist = df if epoch0 is None else df[df.index >= epoch0]
            days = int(pd.Index(hist.index.tz_convert(TZ).date).nunique()) - 1
            out["learning"] = {"days": max(days, 0), "needed": cfg.min_history_days,
                               "ready": bool(s["hue_normal"].iloc[-1:].notna().any()),
                               "since_ms": _ms(epoch0)}
        except Exception as e:
            errors.append(f"readings: {type(e).__name__}: {e}")

    # --- SQLite ---------------------------------------------------------------
    al = db.read_alerts(con).tail(10).iloc[::-1]
    open_after = now - pd.Timedelta(minutes=OPEN_ALERT_MIN)
    out["alerts"] = [{
        "id": int(r["id"]), "start_ms": _ms(r["start"]), "end_ms": _ms(r["end"]),
        "kind": r["kind"], "score": _num(r["score"], 2), "reason": r["reason"],
        "open": bool(pd.notna(r["end"]) and r["end"] >= open_after),
    } for _, r in al.iterrows()]

    runs = {r["job"]: r for r in db.last_job_runs(con)}
    jobs = []
    for job in list(ALWAYS_SHOWN) + sorted(set(runs) - set(ALWAYS_SHOWN)):
        r = runs.get(job)
        every = JOB_EVERY_H.get(job, 24)
        if r is None:
            jobs.append({"job": job, "state": "never", "every_h": every})
            continue
        if r["ok"] is None:
            state = "running"
        elif r["ok"] == 0:
            state = "failed"
        else:
            state = "ok"
        late = r["last_ok_ms"] is None or (
            _ms(now) - r["last_ok_ms"] > (every * 1.5 * 3600 + 600) * 1000)
        if state == "ok" and late:
            state = "late"
        jobs.append({**r, "state": state, "every_h": every})
    out["jobs"] = jobs

    daily = pd.read_sql_query(
        "SELECT local_date, night_hue, peak_hue, swing, rise_t90_local, peak_minutes, "
        "coverage, alerts_n, score_max FROM agg_daily ORDER BY local_date DESC LIMIT 7", con)
    out["daily"] = [{k: (_num(v, 2) if isinstance(v, (int, float, np.floating, np.integer))
                         and not isinstance(v, bool) else v) for k, v in row.items()}
                    for row in daily.to_dict("records")]
    return out


# ---------------------------------------------------------------- HTTP server

class _Cache:
    def __init__(self, compute, ttl: float):
        self.compute, self.ttl = compute, ttl
        self.lock = threading.Lock()
        self.at, self.body = 0.0, b""

    def get(self) -> bytes:
        with self.lock:  # one computation at a time; others wait and reuse it
            if time.monotonic() - self.at > self.ttl or not self.body:
                self.body = json.dumps(self.compute(), separators=(",", ":")).encode()
                self.at = time.monotonic()
            return self.body


def make_loader(vm_url: str | None, source: str | None, csv: str | None, history_days: float):
    """Returns load(now) -> (raw frame or None, now, errors)."""
    csv_frame = vm.frames_from_csv(csv) if csv else None

    def load():
        now = pd.Timestamp.now(tz="UTC")
        if csv_frame is not None:
            # local mock: pretend "now" is just after the end of the file
            now = min(now, csv_frame.index.max() + pd.Timedelta(minutes=5))
            return csv_frame[csv_frame.index >= now - pd.Timedelta(days=history_days)], now, []
        if not vm_url:
            return None, now, []
        try:
            raw = vm.read_all(vm_url, now - pd.Timedelta(days=history_days), now, source=source)
            return raw, now, []
        except Exception as e:
            return None, now, [f"VictoriaMetrics not reachable: {e}"]
    return load


def serve(host: str, port: int, db_path: str, vm_url: str | None = None,
          source: str | None = None, csv: str | None = None, cache_s: float = 120,
          history_days: float = 16) -> None:
    page = resources.files("co2watch").joinpath("static/status.html").read_bytes()
    load = make_loader(vm_url, source, csv, history_days)

    def compute():
        raw, now, errors = load()
        con = db.connect(db_path)
        try:
            return build_status(con, raw, now, errors=errors)
        finally:
            con.close()

    cache = _Cache(compute, cache_s)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/api/status":
                try:
                    self._send(200, cache.get(), "application/json")
                except Exception as e:
                    self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            elif path == "/healthz":
                self._send(200, b"ok", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

        do_HEAD = do_GET

        def log_message(self, fmt, *args):  # keep the pod log quiet
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"co2watch status page on http://{host}:{port}/ "
          f"(data: {'csv ' + csv if csv else vm_url or 'SQLite only'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
