#!/usr/bin/env python3
"""Local emulator for the co2watch D1 web page.

Serves firmware/co2watch/page.html (re-read on every request, so edits show on
reload) plus mock versions of the firmware's HTTP API, with state in memory:

  /                 the page
  /api/state        reading, colour, dimmer, time (what the page polls)
  /api/schedule     GET schedule; POST en=, pts=, reset=1, resume=1
  /dimmer           GET level; POST level=NN
  /white            POST set=1 | reset=1
  /read             POST: take a reading now
  /metrics          Prometheus text, same names as the firmware
  /api/co2index     POST low=|high= (deg or "now"), reset=1
  /relay            POST name=co2|air|heat|pump&on=0|1 (PREVIEW: emulator only)

Readings are simulated: the drop-checker hue drifts blue at night to green/lime
in the day, with a little noise. Python stdlib only.

  python3 firmware/emulator/emulate.py            # http://localhost:8080/
  python3 firmware/emulator/emulate.py --no-ntp   # board without time: safe 30 %
  python3 firmware/emulator/emulate.py --errors   # every 3rd POST fails
  python3 firmware/emulator/emulate.py --speed 60 # clock runs 60x (1 h per minute)
"""
import argparse
import colorsys
import json
import math
import random
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

PAGE = Path(__file__).resolve().parent.parent / "co2watch" / "page.html"
TZ = ZoneInfo("Europe/Amsterdam")

# Keep in sync with co2watch.ino.
_m = __import__("re").search(r'#define FW_VERSION "([^"]+)"',
                            (PAGE.parent / "co2watch.ino").read_text())
FW_VERSION = (_m.group(1) if _m else "?") + "-emulator"
DIM_MIN, DIM_MAX = 0, 100
SAFE_PCT = 30.0
OVERRIDE_S = 3600
# Preview: 100 % from 11:00 to 18:00 (7 h full light, CO2 from 09:00), down to
# 30 % by 19:00, hold 30 % until 20:30, then fade to 0 % by 21:30.
# The firmware default still peaks at 10:00.
SCHED_DEFAULT = [[420, 4, 1], [540, 18, 0], [590, 40, 0], [660, 100, 0],
                 [1080, 100, 0], [1110, 40, 0], [1195, 25, 0], [1290, 0, 1]]
ATIME, GAIN = 0xC0, 4
MAX_COUNT = min((256 - ATIME) * 1024, 65535)


class Board:
    def __init__(self, args):
        self.args = args
        self.t0_real = time.time()
        self.t0_sim = time.time()
        self.boot = time.monotonic()
        self.seq = 0
        self.read_at = 0.0
        self.off = [0, 0, 0, 0]
        self.on = [0, 0, 0, 0]
        self.wb = [1.0, 1.0, 1.0]
        self.dim_pct = 0          # last manual level
        self.dim_out = 0.0        # actual output
        self.dim_changes = 0
        self.sched = [p[:] for p in SCHED_DEFAULT]
        self.sched_en = True   # always on, as on the D1
        self.maxh = 7.0   # preview: max hours at 100 % (page enforces it)
        self.override_until = None
        self.source = "manual"
        self.posts = 0
        self.idx_low, self.idx_high = 203.0, 153.5   # CO2 index ends (firmware defaults)
        self.hue_hist = []
        # Relay preview (mockup only; the firmware has no relay code yet).
        self.relays = {"co2": True, "air": False, "heat": False, "pump": True}
        self.relay_override = {}   # name -> monotonic time the manual hold ends
        self.relay_src = {k: "manual" for k in self.relays}
        self.take_reading()

    # --- time -------------------------------------------------------------
    def now(self):
        """Simulated wall-clock time (epoch seconds)."""
        return self.t0_sim + (time.time() - self.t0_real) * self.args.speed

    def time_valid(self):
        return not self.args.no_ntp

    def local(self):
        return datetime.fromtimestamp(self.now(), TZ)

    def minute(self):
        t = self.local()
        return t.hour * 60 + t.minute + t.second / 60

    # --- simulated sensor -------------------------------------------------
    def true_hue(self):
        """Drop-checker hue: ~200 (blue) at night, ~95 (green/lime) late in the day."""
        m = self.minute() if self.time_valid() else 600
        # CO2 on 05:00-19:00 with a 1.5 h lag; smooth rise and fall.
        def ramp(x, a, b):
            if x <= a:
                return 0.0
            if x >= b:
                return 1.0
            return (1 - math.cos(math.pi * (x - a) / (b - a))) / 2
        level = ramp(m, 390, 720) - ramp(m, 1230, 1500)
        return 205 - 110 * level + random.gauss(0, 1.5)

    def take_reading(self):
        h = self.true_hue() / 360
        r, g, b = colorsys.hsv_to_rgb(h, 0.55, 1.0)
        # Sensor is more sensitive to red: undo a typical white balance.
        sens = (1.25, 1.0, 0.55)
        scale = 5000
        ambient = 40 + 120 * (self.dim_out / 100)  # tank light leaks in
        self.off = [int(ambient * 0.5), int(ambient * 0.3), int(ambient * 0.2), int(ambient)]
        net = [int(v * s * scale + random.gauss(0, 15)) for v, s in zip((r, g, b), sens)]
        net_c = int(sum(net) * 1.1)
        self.on = [min(MAX_COUNT, o + n) for o, n in zip(self.off, net + [net_c])]
        self.seq += 1
        self.read_at = time.monotonic()
        if hasattr(self, "hue_hist"):
            c = self.colour()
            if c["ok"]:
                self.hue_hist = (self.hue_hist + [c["hue"]])[-3:]

    def colour(self):
        c = {"ok": False, "net": [0, 0, 0], "rgb": [0, 0, 0], "hex": "#000000", "hue": 0.0}
        sat = any(v >= MAX_COUNT for v in self.on + self.off)
        net = [self.on[i] - self.off[i] for i in range(3)]
        c["net"] = net
        v = [max(n, 0) * w for n, w in zip(net, self.wb)]
        mx, mn = max(v), min(v)
        if sat or mx <= 0:
            return c
        rgb = [int(x / mx * 255 + 0.5) for x in v]
        hue = colorsys.rgb_to_hsv(*[x / mx for x in v])[0] * 360 if mx > mn else 0.0
        c.update(ok=True, rgb=rgb, hex="#%02X%02X%02X" % tuple(rgb), hue=round(hue, 1))
        return c

    # --- dimmer / schedule (mirrors co2watch.ino) --------------------------
    def set_output(self, pct):
        self.dim_out = round(float(min(max(pct, DIM_MIN), DIM_MAX)), 1)

    def manual(self, pct):
        self.dim_pct = int(min(max(pct, DIM_MIN), DIM_MAX))
        self.set_output(self.dim_pct)
        self.dim_changes += 1
        if self.sched_en:
            self.override_until = time.monotonic() + OVERRIDE_S
            self.source = "override"

    def sched_level(self, m):
        p = self.sched
        i = 7
        for k in range(8):
            if p[k][0] <= m:
                i = k
        j = (i + 1) % 8
        span = p[j][0] - p[i][0]
        if span <= 0:
            span += 1440
        d = m - p[i][0]
        if d < 0:
            d += 1440
        f = min(d / span, 1)
        a, b = p[i][1], p[j][1]
        if p[i][2] == 2:
            return a
        if p[i][2] == 0:
            f = (1 - math.cos(math.pi * f)) / 2
        return a + (b - a) * f

    def relay_rules(self):
        """From the 100 % bar (first two consecutive points at 100 %): CO2 on 2 h
        before the bar starts, off 2 h before it ends; air off from CO2-on until
        the bar ends. Returns (co2_on, co2_off, air_on_at) or None without a bar."""
        p = self.sched
        for k in range(7):
            if p[k][1] >= 100 and p[k + 1][1] >= 100:
                st, en = p[k][0], p[k + 1][0]
                return (st - 120) % 1440, (en - 120) % 1440, en
        return None

    def tick_relays(self):
        now = time.monotonic()
        for k in ("co2", "air"):
            if not self.sched_en:
                self.relay_override.pop(k, None)
                self.relay_src[k] = "manual"
                continue
            if k in self.relay_override:
                if now < self.relay_override[k]:
                    self.relay_src[k] = "override"
                    continue
                del self.relay_override[k]
            if not self.time_valid():
                self.relay_src[k] = "safe"           # no time: CO2 off, air on
                self.relays[k] = k == "air"
                continue
            r = self.relay_rules()
            m = self.minute()
            def win(a, b):
                return a <= m < b if a <= b else (m >= a or m < b)
            if r is None:
                want = k == "air"
            elif k == "co2":
                want = win(r[0], r[1])
            else:
                want = not win(r[0], r[2])
            self.relays[k] = want
            self.relay_src[k] = "schedule"
        # Hard interlock: CO2 and air are never on together; air wins (safe for fish).
        if self.relays["air"]:
            self.relays["co2"] = False
            self.relay_src["co2"] = "interlock"

    def tick(self):
        self.tick_relays()
        if not self.sched_en:
            self.override_until = None
            self.source = "manual"
            return
        if self.override_until is not None:
            if time.monotonic() < self.override_until:
                return
            self.override_until = None
        if not self.time_valid():
            self.source = "safe"
            self.set_output(SAFE_PCT)
        else:
            self.source = "schedule"
            self.set_output(self.sched_level(self.minute()))

    def override_left(self):
        if self.override_until is None:
            return 0
        return max(0, int(self.override_until - time.monotonic()))

    # --- JSON -------------------------------------------------------------
    def state(self):
        self.tick()
        t = self.local()
        valid = self.time_valid()
        return {
            "version": FW_VERSION, "seq": self.seq,
            "age": int(time.monotonic() - self.read_at), "saturated": False,
            "i2c_errors": 0,
            "off": [self.off[0], self.off[1], self.off[2], self.off[3]],
            "on": [self.on[0], self.on[1], self.on[2], self.on[3]],
            "colour": self.colour(), "wb": [round(w, 4) for w in self.wb],
            "dim": {"level": self.dim_out, "min": DIM_MIN, "max": DIM_MAX,
                    "source": self.source, "override_left": self.override_left()},
            "time_valid": valid, "time": t.strftime("%H:%M:%S") if valid else "",
            "minute": round(self.minute(), 2) if valid else 0,
            "relays": dict(self.relays), "relay_src": dict(self.relay_src),
            "co2idx": self.co2idx(),
            "preview": True,  # enables preview-only page features (relays, drag-to-edit)
        }

    def co2idx(self):
        if not self.hue_hist:
            return {"ok": False, "value": 0, "hue": 0, "n": 0, "low": self.idx_low,
                    "high": self.idx_high}
        h = sum(self.hue_hist) / len(self.hue_hist)
        v = (self.idx_low - h) / (self.idx_low - self.idx_high) * 100
        return {"ok": True, "value": round(v, 1), "hue": round(h, 1),
                "n": len(self.hue_hist), "low": self.idx_low, "high": self.idx_high}

    def schedule(self):
        return {"en": self.sched_en, "pts": self.sched, "maxh": self.maxh}

    def metrics(self):
        self.tick()
        c = self.colour()
        lines = ["# TYPE tcs34725_raw gauge"]
        for led, fr in (("off", self.off), ("on", self.on)):
            for ch, v in zip(("red", "green", "blue", "clear"), fr):
                lines.append(f'tcs34725_raw{{channel="{ch}",led="{led}"}} {v}')
            lines.append(f'tcs34725_avalid{{led="{led}"}} 1')
        lines += [
            f"tcs34725_reading_timestamp_seconds {int(self.now())}",
            f"tcs34725_reading_age_seconds {int(time.monotonic() - self.read_at)}",
            "tcs34725_saturated 0",
        ]
        if c["ok"]:
            lines.append(f"tcs34725_hue_degrees {c['hue']:.1f}")
        lines += [
            f"tcs34725_reading_seq {self.seq}", "tcs34725_id_ok 1",
            f"tcs34725_atime {ATIME}", f"tcs34725_gain {GAIN}",
            "# TYPE tcs34725_i2c_errors_total counter", "tcs34725_i2c_errors_total 0",
            "co2watch_pcf8574_present 1",
            f"aquarium_light_dim_percent {self.dim_out:.1f}",
            f"aquarium_light_schedule_enabled {int(self.sched_en)}",
            f"aquarium_light_override_active {int(self.override_until is not None)}",
            f"co2watch_time_valid {int(self.time_valid())}",
            f"aquarium_light_dim_min_percent {DIM_MIN}",
            f"aquarium_light_dim_max_percent {DIM_MAX}",
            "# TYPE aquarium_light_dim_changes_total counter",
            f"aquarium_light_dim_changes_total {self.dim_changes}",
            f'co2watch_build_info{{version="{FW_VERSION}"}} 1',
            f"co2watch_uptime_seconds {int(time.monotonic() - self.boot)}",
            "co2watch_wifi_rssi_dbm -60",
        ]
        return "\n".join(lines) + "\n"


def parse_pts(v):
    pts = []
    for part in v.split(";"):
        bits = part.split(",")
        if len(bits) != 3 or not all(b.strip().isdigit() for b in bits):
            return None, "pts must be 8 entries min,level,ramp\n"
        pts.append([int(b) for b in bits])
    if len(pts) != 8:
        return None, "pts must be 8 entries min,level,ramp\n"
    for k, (m, lv, r) in enumerate(pts):
        if m >= 1440 or lv > 100 or r > 2 or (k and m <= pts[k - 1][0]):
            return None, "times must increase, levels 0-100, ramp 0-2\n"
    return pts, None


def make_handler(board):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            if self.command == "POST":
                print("%s %s" % (self.command, self.path))

        def send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def j(self, obj):
            self.send(200, json.dumps(obj, separators=(",", ":")))

        def do_GET(self):
            self.route()

        def do_POST(self):
            board.posts += 1
            if board.args.errors and board.posts % 3 == 0:
                self.send(500, "emulated failure (--errors)\n", "text/plain")
                return
            if board.args.latency:
                time.sleep(board.args.latency)
            self.route()

        def route(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
            if u.path == "/":
                self.send(200, PAGE.read_text(), "text/html")
            elif u.path == "/api/state":
                self.j(board.state())
            elif u.path == "/metrics":
                self.send(200, board.metrics(), "text/plain; version=0.0.4")
            elif u.path == "/read":
                board.take_reading()
                self.j(board.state())
            elif u.path == "/dimmer":
                if "level" in q:
                    v = q["level"]
                    if not v.isdigit() or int(v) > 100:
                        self.send(400, "level must be an integer 0-100\n", "text/plain")
                        return
                    board.manual(int(v))
                board.tick()
                self.j({"level": board.dim_out, "min": DIM_MIN, "max": DIM_MAX,
                        "source": board.source})
            elif u.path == "/white":
                if "reset" in q:
                    board.wb = [1.0, 1.0, 1.0]
                elif "set" in q:
                    net = board.colour()["net"]
                    if min(net) < 20:
                        self.send(409, "reading too weak or invalid for a white reference\n",
                                  "text/plain")
                        return
                    board.wb = [max(net) / n for n in net]
                self.j(board.state())
            elif u.path == "/api/co2index":
                lo, hi = board.idx_low, board.idx_high
                cur = board.co2idx()
                for name in ("low", "high"):
                    if name in q:
                        v = q[name]
                        if v == "now":
                            if not cur["ok"]:
                                self.send(409, "no valid reading yet\n", "text/plain")
                                return
                            x = cur["hue"]
                        else:
                            try:
                                x = float(v)
                            except ValueError:
                                x = -1
                            if not 0 <= x <= 360:
                                self.send(400, "hue must be 0-360\n", "text/plain")
                                return
                        if name == "low":
                            lo = x
                        else:
                            hi = x
                if "reset" in q:
                    lo, hi = 203.0, 153.5
                if abs(lo - hi) < 1:
                    self.send(400, "low and high hue must differ\n", "text/plain")
                    return
                board.idx_low, board.idx_high = lo, hi
                self.j(board.state())
            elif u.path == "/relay":
                name = q.get("name", "")
                if name not in board.relays:
                    self.send(400, "name must be one of co2, air, heat, pump\n", "text/plain")
                    return
                if "on" in q:
                    board.tick()
                    if name == "co2" and q["on"] == "1" and board.relays["air"]:
                        self.send(409, "Air is on: CO2 stays off (air takes precedence)\n",
                                  "text/plain")
                        return
                    board.relays[name] = q["on"] == "1"
                    if board.sched_en and name in ("co2", "air"):
                        board.relay_override[name] = time.monotonic() + OVERRIDE_S
                board.tick()
                self.j(board.relays)
            elif u.path == "/api/schedule":
                if "reset" in q:
                    board.sched = [p[:] for p in SCHED_DEFAULT]
                if "pts" in q:
                    pts, err = parse_pts(q["pts"])
                    if err:
                        self.send(400, err, "text/plain")
                        return
                    board.sched = pts
                if "en" in q:
                    board.sched_en = True   # the schedule always runs (as on the D1)
                    board.override_until = None
                    board.relay_override.clear()
                if "resume" in q:
                    board.override_until = None
                    board.relay_override.clear()
                if "maxh" in q:
                    try:
                        board.maxh = min(16.0, max(0.5, float(q["maxh"])))
                    except ValueError:
                        self.send(400, "maxh must be a number of hours\n", "text/plain")
                        return
                board.tick()
                self.j(board.schedule())
            else:
                self.send(404, "Not found: %s\n" % u.path, "text/plain")
    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to reach it from your phone")
    ap.add_argument("--no-ntp", action="store_true", help="pretend the board has no time")
    ap.add_argument("--errors", action="store_true", help="every 3rd POST returns HTTP 500")
    ap.add_argument("--latency", type=float, default=0.4,
                    help="seconds of delay on POST, like the real board (default 0.4)")
    ap.add_argument("--speed", type=float, default=1.0, help="clock speed-up factor")
    ap.add_argument("--start", help="simulated start time HH:MM (today, Amsterdam)")
    ap.add_argument("--reading-every", type=float, default=300,
                    help="seconds between simulated readings (default 300)")
    ap.add_argument("--relay-flip", type=float, default=20,
                    help="seconds between random relay flips, 0 = off (default 20)")
    args = ap.parse_args()

    board = Board(args)
    if args.start:
        h, m = map(int, args.start.split(":"))
        t = datetime.now(TZ).replace(hour=h, minute=m, second=0, microsecond=0)
        board.t0_sim = t.timestamp()
        board.take_reading()

    srv = ThreadingHTTPServer((args.host, args.port), make_handler(board))

    import threading

    def reader():
        while True:
            time.sleep(args.reading_every)
            board.take_reading()
    threading.Thread(target=reader, daemon=True).start()

    def flipper():
        # Flip a random relay now and then so the state display can be seen moving.
        while args.relay_flip > 0:
            time.sleep(args.relay_flip)
            k = random.choice(["heat", "pump"])  # CO2/air follow the schedule rules
            board.relays[k] = not board.relays[k]
            print(f"(emulator) relay {k} -> {'on' if board.relays[k] else 'off'}")
    threading.Thread(target=flipper, daemon=True).start()

    print(f"co2watch emulator on http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}/"
          f"  (page: {PAGE}, edits show on reload)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
