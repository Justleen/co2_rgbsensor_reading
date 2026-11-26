"""Small status page: ``co2watch serve``.

One process, standard library HTTP server, no extra dependencies. It serves a
single static page (``static/status.html``) and one JSON endpoint that the
page polls:

* ``GET /``             the page
* ``GET /api/status``   everything the page shows (see ``build_status``)
* ``GET /healthz``      "ok" (for a liveness probe)

With ``--score-every N`` the same process also runs the score job every N
seconds (instead of a CronJob that starts a fresh Python every 5 minutes) and
the page shows that result. Otherwise the JSON is recomputed at most every
``cache_s`` seconds, whatever the number of open browsers, so the cost on lap1 is one VictoriaMetrics read plus one
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
from .config import CHART_KH4, CO2_INDEX_DEFAULT_ENDS, TZ

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


def index_map(df: pd.DataFrame, fit_days: float = 7) -> dict:
    """Linear map from sensor hue to the D1's CO2 index %: pct = a + b * hue.

    The D1 computes its index from its own white-balanced hue, which is not the
    same number as the sensor hue here (LED on minus off frames). So when the D1
    exports its index, fit the map to it over the last ``fit_days``; the page
    then shows the same % as the D1. Otherwise use the D1's index ends (or the
    firmware defaults) directly on the sensor hue. Never clamped: below 0 % and
    above 100 % are real readings outside the D1's ends.
    """
    def ends():
        lo, hi = CO2_INDEX_DEFAULT_ENDS
        src = "firmware defaults"
        if "co2_index_low" in df and df["co2_index_low"].notna().any():
            lo, hi = float(df["co2_index_low"].dropna().iloc[-1]), \
                float(df["co2_index_high"].dropna().iloc[-1])
            src = "the D1's ends"
        b = -100.0 / (lo - hi)
        return {"a": _num(-b * lo, 4), "b": _num(b, 5), "method": "ends", "low_hue": _num(lo, 1),
                "high_hue": _num(hi, 1), "source": src}

    if "co2_index_pct" not in df or "hue" not in df:
        return ends()
    recent = df[df.index >= df.index.max() - pd.Timedelta(days=fit_days)]
    x, y = recent["hue"].to_numpy(float), recent["co2_index_pct"].to_numpy(float)
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 288 or np.ptp(x[m]) < 15:   # need a day of data and a real daily swing
        return ends()
    b, a = np.polyfit(x[m], y[m], 1)
    r2 = 1 - np.var(y[m] - (a + b * x[m])) / max(np.var(y[m]), 1e-9)
    if b >= 0 or r2 < 0.8:
        return ends()
    return {"a": _num(a, 4), "b": _num(b, 5), "method": "fit", "r2": _num(r2, 3),
            "low_hue": _num(-a / b, 1), "high_hue": _num((100 - a) / b, 1),
            "source": "fitted to the D1's index"}


# hue pairs in alert details (detect.explain) -> "<what>: X % vs usual Y %"
_NOTE_PAIRS = (("hue_now", "hue_normal", "CO2 index"), ("hue_now", "peak_normal", "CO2 index"),
               ("peak_hue", "peak_normal", "peak"), ("night_hue", "night_normal", "night"))


def _index_note(details_json, imap: dict | None) -> str | None:
    """The hue numbers of a stored alert reason as CO2 index %, for the page."""
    if not imap or not details_json:
        return None
    try:
        d = json.loads(details_json)
    except (TypeError, ValueError):
        return None
    a, b = imap["a"], imap["b"]
    for now_k, ref_k, what in _NOTE_PAIRS:
        if d.get(now_k) is not None and d.get(ref_k) is not None:
            word = "usual peak" if ref_k == "peak_normal" and now_k == "hue_now" else "usual"
            return (f"{what}: {a + b * d[now_k]:.0f} % vs {word} {a + b * d[ref_k]:.0f} %")
    if d.get("swing_now") is not None and d.get("swing_normal") is not None:
        return (f"swing: {abs(b) * d['swing_now']:.0f} vs usual "
                f"{abs(b) * d['swing_normal']:.0f} index points")
    return None


def _light(df: pd.DataFrame) -> list:
    """Dimmer level in %, or the on/off state as 0/100 when there is no dimmer series."""
    if "light_pct" in df and df["light_pct"].notna().any():
        return _col(df, "light_pct", 0)
    if "light_on" in df:
        return [_num(v * 100, 0) for v in df["light_on"].astype(float).to_numpy()]
    return [None] * len(df)


def build_status(con, raw: pd.DataFrame | None, now: pd.Timestamp,
                 window_h: float = 48, errors: list[str] | None = None,
                 prepared=None) -> dict:
    """Everything the status page shows, as plain JSON-able data.

    ``raw`` is the reading frame (vm.read_all / frames_from_csv), ideally 16
    days so the per-slot normal band has its 14 days of history. ``con`` is
    the SQLite connection (alerts, job runs, daily aggregates).
    ``prepared`` = (features.prepare frame, detect.Detection) when the caller
    already ran detection on ``raw`` (the score loop), so it is not run twice.
    """
    from .calibration import band_for_chart_hue, extract_daily_anchors, fit_calibration
    from .detect import DetectConfig, detect
    from .features import prepare

    errors = list(errors or [])
    out: dict = {"now_ms": _ms(now), "tz": str(TZ), "window_h": window_h, "errors": errors,
                 "latest": None, "series": None, "learning": None, "bands": [],
                 "index": None}
    events = [e for e in db.calibration_events(con) if e <= now]

    if raw is not None and len(raw):
        raw = raw[raw.index < now]
    if raw is not None and len(raw):
        try:
            cfg = DetectConfig()
            if prepared is not None:
                df, det = prepared
            else:
                df = prepare(raw)
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
            imap = index_map(df)
            out["index"] = imap

            def pct(h):
                return None if h is None else _num(imap["a"] + imap["b"] * h, 0)
            latest["hue_pct"], latest["normal_pct"] = pct(hue), pct(latest["normal_hue"])
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
        "index_note": _index_note(r.get("details_json"), out["index"]),
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

    def put(self, status: dict) -> None:
        body = json.dumps(status, separators=(",", ":")).encode()
        with self.lock:
            self.body, self.at = body, time.monotonic()

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


def score_once(db_path: str, load, since_hours: float = 2.0) -> tuple[bool, str, dict]:
    """One pass of the score loop: load readings, run detection, log alerts
    (exactly like ``co2watch score``), record it in job_runs and return the
    page status built from the same detection."""
    from .detect import DetectConfig, detect, log_alerts
    from .features import prepare
    con = db.connect(db_path)
    run = db.job_started(con, "score")
    try:
        raw, now, errors = load()
        if raw is not None and len(raw):
            raw = raw[raw.index < now]
        if errors or raw is None or raw.empty:
            msg = errors[0] if errors else "no readings in the selected window"
            db.job_finished(con, run, False, msg)
            return False, msg, build_status(con, raw, now, errors=errors)
        df = prepare(raw)
        events = [e for e in db.calibration_events(con) if e <= now]
        det = detect(df, DetectConfig(), events)
        logged = log_alerts(con, det, now, since_hours)
        new = sum(created for _, created, _ in logged)
        msg = (f"scored up to {df.index[-1].tz_convert(TZ):%Y-%m-%d %H:%M}; latest score "
               f"{det.scores['score'].iloc[-1]:.2f}; {new} new alert(s)")
        db.job_finished(con, run, True, msg)
        return True, msg, build_status(con, raw, now, prepared=(df, det))
    except Exception as e:
        db.job_finished(con, run, False, f"{type(e).__name__}: {e}")
        raise
    finally:
        con.close()


def _score_loop(db_path: str, load, every_s: float, cache: _Cache) -> None:
    while True:
        t0 = time.monotonic()
        try:
            ok, msg, status = score_once(db_path, load)
            cache.put(status)
            print(f"score: {msg}", flush=True)
        except Exception as e:  # keep looping; the failure is in job_runs
            print(f"score failed: {type(e).__name__}: {e}", flush=True)
        time.sleep(max(5.0, every_s - (time.monotonic() - t0)))


def serve(host: str, port: int, db_path: str, vm_url: str | None = None,
          source: str | None = None, csv: str | None = None, cache_s: float = 120,
          history_days: float = 16, score_every_s: float = 0) -> None:
    """Serve the page. With ``score_every_s`` > 0 a background thread also runs
    the score job every that many seconds (replacing the score CronJob), and the
    page shows that thread's latest result instead of computing its own."""
    page = resources.files("co2watch").joinpath("static/status.html").read_bytes()
    load = make_loader(vm_url, source, csv, history_days)

    def compute():
        raw, now, errors = load()
        con = db.connect(db_path)
        try:
            return build_status(con, raw, now, errors=errors)
        finally:
            con.close()

    # With the score loop the loop keeps the cache fresh; the page only computes
    # by itself if the loop has not delivered for two rounds.
    cache = _Cache(compute, max(cache_s, 2 * score_every_s + 60) if score_every_s else cache_s)
    if score_every_s:
        threading.Thread(target=_score_loop, args=(db_path, load, score_every_s, cache),
                         daemon=True, name="score-loop").start()

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
          f"(data: {'csv ' + csv if csv else vm_url or 'SQLite only'}"
          f"{f'; scoring every {score_every_s:g} s' if score_every_s else ''})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
