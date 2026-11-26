// co2watch firmware for a Wemos D1 mini (ESP8266).
//
// Reads a TCS34725 colour sensor looking at a CO2 drop checker and serves the
// last reading on http://<ip>/metrics in Prometheus text format, for
// VictoriaMetrics to scrape. See docs/design.md section 4 for the contract.
//
// Wiring: I2C SDA = D2 (GPIO4), SCL = D1 (GPIO5). TCS34725 at 0x29. The
// breakout's LED pin is driven by PCF8574 P0 (LOW = LEDs off). The PCF8574
// address is found by scanning 0x20-0x27 (or 0x38-0x3F for the "A" part).
//
// Each reading is two frames taken back to back: LEDs off (ambient), then
// LEDs on. Gain and ATIME are fixed constants and must not change once the
// baseline is collected.
//
// Light dimmer (manual only, nothing automatic): D6 (GPIO12) outputs 1 kHz PWM
// to a PWM-to-0-10V converter whose OUT drives the Mean Well LCM-40 DIM+/DIM-.
// Duty % = light level %. Set it with http://<host>/dimmer?level=NN (0-100),
// read it with http://<host>/dimmer. Requests are clamped to DIM_MIN_PCT..
// DIM_MAX_PCT. The last level set is stored in flash and restored at boot.
// While the D1 boots or resets, D6 is low, so the LCM sits at its minimum
// (a dim glow, not off) for about a second.
//
// Light schedule: 8 points (time + level), each with a ramp to the next point
// (smooth = cosine, linear, or step), wrapping around midnight. Local time
// comes from NTP in Europe/Amsterdam (DST handled by the TZ rule). When the
// schedule is on: a manual change holds for 1 hour, then the schedule resumes;
// without a valid time the light goes to SAFE_PCT. When it is off, only manual
// control applies. Edit it on the web page; it is stored in flash.
//
// Web page at http://<host>/ : set the dimmer and see the drop-checker colour
// (net = LEDs on - LEDs off, white-balanced, as a swatch, hex and hue). The
// white balance is display-only; /metrics keeps the raw counts for the model.
// "Use as white reference" stores per-channel gains from the current reading
// (take it with the sensor on the white backing, no drop checker).
//
// Serial (115200): prints the I2C scan at boot and every reading.
// Send 'r' to take a reading now, 's' to rescan the bus, 'dNN' + Enter to set
// the dimmer to NN %.

#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ESP8266mDNS.h>
#include <Wire.h>
#include <EEPROM.h>
#include <ArduinoOTA.h>
#include <time.h>

#if __has_include("secrets.h")
#include "secrets.h"  // defines WIFI_SSID and WIFI_PASS; not committed
#endif
#ifndef WIFI_SSID
#define WIFI_SSID ""
#define WIFI_PASS ""
#endif

// ---- fixed sensor settings (changing these starts a new calibration epoch) --
static const uint8_t ATIME = 0xC0;      // 64 cycles: 153.6 ms, max count 65535
static const uint8_t GAIN_REG = 0x01;   // 0=1x 1=4x 2=16x 3=60x
static const uint8_t GAIN_X[] = {1, 4, 16, 60};

static const uint32_t READ_INTERVAL_MS = 5UL * 60UL * 1000UL;
static const char *HOSTNAME = "co2watch";
#define FW_VERSION "0.6.1"

// ---- dimmer limits (edit and reflash to change) -----------------------------
static const uint8_t DIM_PIN = D6;          // GPIO12
static const uint32_t DIM_PWM_HZ = 1000;    // converter accepts 22 Hz - 20 kHz
static const uint8_t DIM_MIN_PCT = 0;       // lowest level a request can set
static const uint8_t DIM_MAX_PCT = 100;     // highest level a request can set
static const uint8_t DIM_FIRST_BOOT_PCT = 0;  // used until a level is ever set
static const uint8_t EEPROM_MAGIC = 0xD1;
static const uint8_t EEPROM_WB_MAGIC = 0xB4;  // byte 2; gains at bytes 4..15
static const uint8_t EEPROM_SCHED_MAGIC = 0x5C;  // byte 16; enable 17; points 18..49
static const int EEPROM_MAXH = 50;  // hours at 100 %, in half hours (1..32)
static const int EEPROM_IDX = 52;   // magic 0xC1; float low hue @54, high hue @58

// ---- CO2 index ----------------------------------------------------------------
// Zooms into the drop checker's colour band: the (white-balanced, smoothed) hue
// is mapped linearly so that IDX_LOW_HUE = 0 % (blue, low CO2) and IDX_HIGH_HUE =
// 100 % (high CO2). Low default is the leaflet's 3 mg/l hue; high default is the
// end leen set on the D1 on 2026-11-26. Set both ends from real readings on the
// page. Not clamped, so values below 0 or above 100 mean "outside the range".
static const float IDX_DEFAULT_LOW = 203, IDX_DEFAULT_HIGH = 153.5;  // ends set on the D1 2026-11-26
static const uint8_t IDX_SMOOTH = 3;  // readings averaged (3 x 5 min)

// ---- schedule ---------------------------------------------------------------
static const char *TZ_AMSTERDAM = "CET-1CEST,M3.5.0,M10.5.0/3";
static const float SAFE_PCT = 30;                       // no valid time
static const uint32_t OVERRIDE_MS = 60UL * 60UL * 1000UL;  // manual hold
enum Ramp : uint8_t { RAMP_SMOOTH = 0, RAMP_LINEAR = 1, RAMP_STEP = 2 };
struct SchedPoint { uint16_t min; uint8_t level; uint8_t ramp; };
// Default = the schedule saved on the D1 on 2026-11-26 (7 h at 100 % 11:00-18:00,
// 40 % at 18:30, 25 % at 19:55, off by 21:30).
static const SchedPoint SCHED_DEFAULT[8] = {
    {420, 4, 1},    {480, 15, 0},  {510, 40, 0},  {660, 100, 0},
    {1080, 100, 0}, {1110, 40, 0}, {1195, 25, 0}, {1290, 0, 1}};

// ---- TCS34725 registers ----------------------------------------------------
static const uint8_t TCS_ADDR = 0x29;
static const uint8_t CMD = 0x80, CMD_AUTOINC = 0xA0;
static const uint8_t REG_ENABLE = 0x00, REG_ATIME = 0x01, REG_CONTROL = 0x0F,
                     REG_ID = 0x12, REG_STATUS = 0x13, REG_CDATA = 0x14;
static const uint8_t EN_PON = 0x01, EN_AEN = 0x02;

uint8_t pcfAddr = 0;  // 0 = not found

struct Frame {
  uint16_t c = 0, r = 0, g = 0, b = 0;
  bool ok = false;
  bool avalid = false;
};

struct Reading {
  Frame off, on;
  uint32_t seq = 0;
  time_t ts = 0;          // unix time, 0 if NTP not synced yet
  uint32_t millisAt = 0;
  bool idOk = false;
  bool saturated = false;
} last;

struct Colour {
  bool ok = false;
  int net[3] = {0, 0, 0};
  uint8_t rgb[3] = {0, 0, 0};
  float hue = 0;
  char hex[8] = "#000000";
};

uint32_t i2cErrors = 0;
uint8_t dimPct = 0;     // last manual level
float dimOut = 0;       // what D6 actually outputs, in %
SchedPoint sched[8];
bool schedEnabled = false;
float idxLow = IDX_DEFAULT_LOW, idxHigh = IDX_DEFAULT_HIGH;
float hueHist[IDX_SMOOTH];
uint8_t hueN = 0, hueHead = 0;
float schedMaxH = 7;   // hours at 100 % (the page enforces it while editing)
bool overrideActive = false;
uint32_t overrideStart = 0;
const char *dimSource = "manual";  // manual / schedule / override / safe
uint32_t dimChanges = 0;
uint32_t lastReadMs = 0;
bool haveReading = false;
ESP8266WebServer server(80);

// ---- I2C helpers -----------------------------------------------------------
bool tcsWrite(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(TCS_ADDR);
  Wire.write(CMD | reg);
  Wire.write(val);
  if (Wire.endTransmission() != 0) { i2cErrors++; return false; }
  return true;
}

bool tcsRead(uint8_t reg, uint8_t *buf, uint8_t n) {
  Wire.beginTransmission(TCS_ADDR);
  Wire.write(CMD_AUTOINC | reg);
  if (Wire.endTransmission(false) != 0) { i2cErrors++; return false; }
  if (Wire.requestFrom(TCS_ADDR, n) != n) { i2cErrors++; return false; }
  for (uint8_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

bool setLeds(bool on) {
  if (!pcfAddr) { i2cErrors++; return false; }
  Wire.beginTransmission(pcfAddr);
  // P0 drives the LED pin; other pins stay high (inputs / weak pull-up).
  Wire.write(on ? 0xFF : 0xFE);
  if (Wire.endTransmission() != 0) { i2cErrors++; return false; }
  return true;
}

void scanBus() {
  Serial.println(F("I2C scan:"));
  int found = 0;
  pcfAddr = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      found++;
      Serial.printf("  0x%02X", a);
      if (a == TCS_ADDR) Serial.print(F("  TCS34725"));
      if (!pcfAddr && ((a >= 0x20 && a <= 0x27) || (a >= 0x38 && a <= 0x3F))) {
        pcfAddr = a;
        Serial.print(F("  PCF8574 (LED switch)"));
      }
      Serial.println();
    }
  }
  if (!found) Serial.println(F("  nothing found (check wiring / pull-ups)"));
}

uint16_t maxCount() {
  uint32_t m = (uint32_t)(256 - ATIME) * 1024;
  return m > 65535 ? 65535 : (uint16_t)m;
}

uint32_t integrationMs() { return (uint32_t)((256 - ATIME) * 24 + 9) / 10; }

bool tcsInit() {
  uint8_t id = 0;
  last.idOk = tcsRead(REG_ID, &id, 1) && (id == 0x44 || id == 0x4D);
  bool ok = tcsWrite(REG_ATIME, ATIME) && tcsWrite(REG_CONTROL, GAIN_REG) &&
            tcsWrite(REG_ENABLE, EN_PON);
  delay(3);  // 2.4 ms warm-up after PON
  Serial.printf("TCS34725 id=0x%02X (%s), ATIME=0x%02X (%lu ms), gain=%ux\n", id,
                last.idOk ? "ok" : "UNEXPECTED", ATIME,
                (unsigned long)integrationMs(), GAIN_X[GAIN_REG]);
  return ok;
}

// Start a fresh integration, wait for it, read status + 4 channels.
Frame takeFrame() {
  Frame f;
  if (!tcsWrite(REG_ENABLE, EN_PON)) return f;          // stop ADC
  if (!tcsWrite(REG_ENABLE, EN_PON | EN_AEN)) return f; // restart: new cycle
  delay(integrationMs() + integrationMs() / 5 + 3);
  uint8_t buf[9];
  if (!tcsRead(REG_STATUS, buf, 9)) return f;           // 0x13..0x1B
  f.avalid = buf[0] & 0x01;
  f.c = buf[1] | (buf[2] << 8);
  f.r = buf[3] | (buf[4] << 8);
  f.g = buf[5] | (buf[6] << 8);
  f.b = buf[7] | (buf[8] << 8);
  f.ok = true;
  return f;
}

void takeReading() {
  Reading r;
  uint8_t id = 0;
  r.idOk = tcsRead(REG_ID, &id, 1) && (id == 0x44 || id == 0x4D);
  // Re-apply the fixed settings every time, in case the chip browned out.
  tcsWrite(REG_ATIME, ATIME);
  tcsWrite(REG_CONTROL, GAIN_REG);

  setLeds(false);
  delay(integrationMs());  // let any previous integration with LEDs on finish
  r.off = takeFrame();
  bool ledOk = setLeds(true);
  r.on = takeFrame();
  setLeds(false);
  if (!ledOk) r.on.ok = false;  // without the expander the on frame is not lit

  tcsWrite(REG_ENABLE, EN_PON);  // ADC idle between readings
  uint16_t mx = maxCount();
  r.saturated = r.off.c >= mx || r.on.c >= mx || r.on.r >= mx || r.on.g >= mx ||
                r.on.b >= mx;
  r.seq = last.seq + 1;
  time_t now = time(nullptr);
  r.ts = now > 1700000000 ? now : 0;
  r.millisAt = millis();
  last = r;
  haveReading = true;
  Colour col = colourOf(r);
  if (col.ok) {
    hueHist[hueHead] = col.hue;
    hueHead = (hueHead + 1) % IDX_SMOOTH;
    if (hueN < IDX_SMOOTH) hueN++;
  }

  Serial.printf(
      "#%lu off c=%u r=%u g=%u b=%u%s | on c=%u r=%u g=%u b=%u%s | net c=%d r=%d g=%d b=%d%s errs=%lu\n",
      (unsigned long)r.seq, r.off.c, r.off.r, r.off.g, r.off.b,
      r.off.ok ? "" : " (FAIL)", r.on.c, r.on.r, r.on.g, r.on.b,
      r.on.ok ? "" : " (FAIL)", (int)r.on.c - r.off.c, (int)r.on.r - r.off.r,
      (int)r.on.g - r.off.g, (int)r.on.b - r.off.b,
      r.saturated ? " SATURATED" : "", (unsigned long)i2cErrors);
}

// ---- dimmer -----------------------------------------------------------------
uint8_t clampDim(int pct) {
  if (pct < DIM_MIN_PCT) return DIM_MIN_PCT;
  if (pct > DIM_MAX_PCT) return DIM_MAX_PCT;
  return (uint8_t)pct;
}

void setOutput(float pct) {
  if (pct < DIM_MIN_PCT) pct = DIM_MIN_PCT;
  if (pct > DIM_MAX_PCT) pct = DIM_MAX_PCT;
  dimOut = pct;
  analogWrite(DIM_PIN, (int)(pct * 10 + 0.5f));  // range 0..1000, 0.1 % steps
}

// A manual change: from the page, /dimmer or serial.
void applyDim(uint8_t pct, bool save) {
  dimPct = clampDim(pct);
  setOutput(dimPct);
  if (save) {
    dimChanges++;
    EEPROM.write(0, EEPROM_MAGIC);
    EEPROM.write(1, dimPct);
    EEPROM.commit();
    if (schedEnabled) {
      overrideActive = true;
      overrideStart = millis();
      dimSource = "override";
    }
  }
  Serial.printf("dimmer: %u %% (limits %u-%u)%s\n", dimPct, DIM_MIN_PCT, DIM_MAX_PCT,
                overrideActive ? ", schedule paused 1 h" : "");
}

// ---- schedule ---------------------------------------------------------------
bool timeValid() { return time(nullptr) > 1700000000; }

float minuteOfDay() {
  time_t now = time(nullptr);
  struct tm t;
  localtime_r(&now, &t);
  return t.tm_hour * 60 + t.tm_min + t.tm_sec / 60.0f;
}

float schedLevelAt(float m) {
  int i = 7;  // before the first point: still in the segment from the last one
  for (int k = 0; k < 8; k++)
    if (sched[k].min <= m) i = k;
  int j = (i + 1) % 8;
  float span = (float)sched[j].min - sched[i].min;
  if (span <= 0) span += 1440;
  float d = m - sched[i].min;
  if (d < 0) d += 1440;
  float f = d / span;
  if (f > 1) f = 1;
  float a = sched[i].level, b = sched[j].level;
  if (sched[i].ramp == RAMP_STEP) return a;
  if (sched[i].ramp == RAMP_SMOOTH) f = (1 - cosf(PI * f)) / 2;
  return a + (b - a) * f;
}

bool schedValid(const SchedPoint *p) {
  for (int k = 0; k < 8; k++) {
    if (p[k].min >= 1440 || p[k].level > 100 || p[k].ramp > RAMP_STEP) return false;
    if (k && p[k].min <= p[k - 1].min) return false;  // strictly increasing
  }
  return true;
}

void schedSave() {
  EEPROM.write(16, EEPROM_SCHED_MAGIC);
  EEPROM.write(17, schedEnabled);
  EEPROM.put(18, sched);
  EEPROM.write(EEPROM_MAXH, (uint8_t)(schedMaxH * 2 + 0.5f));
  EEPROM.commit();
}

// The schedule always runs after a boot (power cut, reset, OTA), so the light
// never stays stuck at a manual level. Switching it off on the page lasts until
// the next reboot.
void schedLoad() {
  memcpy(sched, SCHED_DEFAULT, sizeof sched);
  schedEnabled = true;
  if (EEPROM.read(16) != EEPROM_SCHED_MAGIC) return;
  SchedPoint p[8];
  EEPROM.get(18, p);
  if (!schedValid(p)) return;
  memcpy(sched, p, sizeof sched);
  uint8_t hh = EEPROM.read(EEPROM_MAXH);
  if (hh >= 1 && hh <= 32) schedMaxH = hh / 2.0f;
}

// Called every second.
void schedTick() {
  if (!schedEnabled) {
    overrideActive = false;
    dimSource = "manual";
    return;
  }
  if (overrideActive) {
    if (millis() - overrideStart < OVERRIDE_MS) return;
    overrideActive = false;
    Serial.println(F("schedule: manual override expired"));
  }
  if (!timeValid()) {
    dimSource = "safe";
    setOutput(SAFE_PCT);
  } else {
    dimSource = "schedule";
    setOutput(schedLevelAt(minuteOfDay()));
  }
}

void sendSchedule() {
  String b = "{\"en\":";
  b += schedEnabled ? "true" : "false";
  b += ",\"pts\":[";
  for (int k = 0; k < 8; k++) {
    if (k) b += ',';
    b += '[';
    b += sched[k].min; b += ','; b += sched[k].level; b += ','; b += sched[k].ramp;
    b += ']';
  }
  b += "],\"maxh\":";
  b += String(schedMaxH, 1);
  b += "}";
  server.send(200, "application/json", b);
}

// GET: current schedule. POST args: en=0|1, pts="min,level,ramp;..." (8),
// reset=1 (defaults, keeps en), resume=1 (end the manual override now).
void handleSchedule() {
  if (server.hasArg("reset")) memcpy(sched, SCHED_DEFAULT, sizeof sched);
  if (server.hasArg("pts")) {
    SchedPoint p[8];
    String v = server.arg("pts");
    int pos = 0;
    for (int k = 0; k < 8; k++) {
      unsigned m, l, r;
      int used = 0;
      if (sscanf(v.c_str() + pos, "%u,%u,%u%n", &m, &l, &r, &used) != 3 ||
          m > 65535 || l > 255 || r > 255) {
        server.send(400, "text/plain", "pts must be 8 entries min,level,ramp\n");
        return;
      }
      p[k] = {(uint16_t)m, (uint8_t)l, (uint8_t)r};
      pos += used;
      if (v[pos] == ';') pos++;
    }
    if (!schedValid(p)) {
      server.send(400, "text/plain", "times must increase, levels 0-100, ramp 0-2\n");
      return;
    }
    memcpy(sched, p, sizeof sched);
  }
  if (server.hasArg("en")) {
    bool en = server.arg("en") == "1";
    if (!en && schedEnabled) {
      dimPct = clampDim((int)(dimOut + 0.5f));  // keep the light where it is
      EEPROM.write(0, EEPROM_MAGIC);
      EEPROM.write(1, dimPct);
    }
    schedEnabled = en;
    overrideActive = false;
  }
  if (server.hasArg("resume")) overrideActive = false;
  if (server.hasArg("maxh")) {
    float h = server.arg("maxh").toFloat();
    if (!(h >= 0.5f && h <= 16.0f)) {
      server.send(400, "text/plain", "maxh must be 0.5-16 hours\n");
      return;
    }
    schedMaxH = (int)(h * 2 + 0.5f) / 2.0f;
  }
  if (server.hasArg("reset") || server.hasArg("pts") || server.hasArg("en") ||
      server.hasArg("maxh"))
    schedSave();
  schedTick();
  sendSchedule();
}

void dimmerBegin() {
  analogWriteFreq(DIM_PWM_HZ);
  analogWriteRange(1000);
  EEPROM.begin(64);
  uint8_t pct = DIM_FIRST_BOOT_PCT;
  if (EEPROM.read(0) == EEPROM_MAGIC) pct = EEPROM.read(1);
  schedLoad();
  if (schedEnabled) setOutput(SAFE_PCT);  // until NTP gives the time
  else applyDim(pct, false);
  dimPct = clampDim(pct);
  wbLoad();
  idxLoad();
}

void handleDimmer() {
  if (server.hasArg("level")) {
    String v = server.arg("level");
    char *end;
    long pct = strtol(v.c_str(), &end, 10);
    if (v.length() == 0 || *end != '\0' || pct < 0 || pct > 100) {
      server.send(400, "text/plain", "level must be an integer 0-100\n");
      return;
    }
    applyDim((uint8_t)pct, true);
  }
  char body[128];
  snprintf(body, sizeof body, "{\"level\":%.1f,\"min\":%u,\"max\":%u,\"source\":\"%s\"}\n",
           dimOut, DIM_MIN_PCT, DIM_MAX_PCT, dimSource);
  server.send(200, "application/json", body);
}

// ---- colour (display only) -------------------------------------------------
float wbGain[3] = {1, 1, 1};  // red, green, blue

void wbLoad() {
  if (EEPROM.read(2) != EEPROM_WB_MAGIC) return;
  float g[3];
  EEPROM.get(4, g);
  for (int i = 0; i < 3; i++)
    if (!(g[i] > 0.01f && g[i] < 100.0f)) return;  // corrupt: keep defaults
  memcpy(wbGain, g, sizeof g);
}

void wbSave() {
  EEPROM.write(2, EEPROM_WB_MAGIC);
  EEPROM.put(4, wbGain);
  EEPROM.commit();
}

Colour colourOf(const Reading &r) {
  Colour c;
  if (!haveReading || !r.off.ok || !r.on.ok || r.saturated) return c;
  c.net[0] = (int)r.on.r - r.off.r;
  c.net[1] = (int)r.on.g - r.off.g;
  c.net[2] = (int)r.on.b - r.off.b;
  float v[3], mx = 0, mn = 1e9;
  for (int i = 0; i < 3; i++) {
    v[i] = (c.net[i] > 0 ? c.net[i] : 0) * wbGain[i];
    if (v[i] > mx) mx = v[i];
    if (v[i] < mn) mn = v[i];
  }
  if (mx <= 0) return c;
  for (int i = 0; i < 3; i++) c.rgb[i] = (uint8_t)(v[i] / mx * 255.0f + 0.5f);
  snprintf(c.hex, sizeof c.hex, "#%02X%02X%02X", c.rgb[0], c.rgb[1], c.rgb[2]);
  float d = mx - mn, h = 0;
  if (d > 0) {
    if (mx == v[0]) h = fmodf((v[1] - v[2]) / d, 6.0f);
    else if (mx == v[1]) h = (v[2] - v[0]) / d + 2.0f;
    else h = (v[0] - v[1]) / d + 4.0f;
    h *= 60.0f;
    if (h < 0) h += 360.0f;
  }
  c.hue = h;
  c.ok = true;
  return c;
}

// ---- web page ---------------------------------------------------------------
// The page lives in page.html; page.h is generated from it by
// firmware/gen_page.py (flash-ota.sh runs it). Edit page.html, not page.h.
#include "page.h"

void handleRoot() { server.send_P(200, "text/html", PAGE); }

// ---- CO2 index ------------------------------------------------------------------
bool smoothHue(float &h) {
  if (!hueN) return false;
  float sum = 0;
  for (uint8_t i = 0; i < hueN; i++) sum += hueHist[i];
  h = sum / hueN;
  return true;
}

bool co2Index(float &idx) {
  float h;
  if (!smoothHue(h) || fabsf(idxLow - idxHigh) < 1) return false;
  idx = (idxLow - h) / (idxLow - idxHigh) * 100.0f;
  return true;
}

void idxLoad() {
  if (EEPROM.read(EEPROM_IDX) != 0xC1) return;
  float lo, hi;
  EEPROM.get(EEPROM_IDX + 2, lo);
  EEPROM.get(EEPROM_IDX + 6, hi);
  if (lo >= 0 && lo <= 360 && hi >= 0 && hi <= 360 && fabsf(lo - hi) >= 1) {
    idxLow = lo;
    idxHigh = hi;
  }
}

void idxSave() {
  EEPROM.write(EEPROM_IDX, 0xC1);
  EEPROM.put(EEPROM_IDX + 2, idxLow);
  EEPROM.put(EEPROM_IDX + 6, idxHigh);
  EEPROM.commit();
}

// POST /api/co2index: low=deg, high=deg, low=now / high=now (use the smoothed
// hue), reset=1 (defaults). Always answers with the current index state.
void handleIndex() {
  float lo = idxLow, hi = idxHigh, h;
  bool haveH = smoothHue(h);
  for (int k = 0; k < 2; k++) {
    const char *name = k ? "high" : "low";
    if (!server.hasArg(name)) continue;
    String v = server.arg(name);
    float x;
    if (v == "now") {
      if (!haveH) {
        server.send(409, "text/plain", "no valid reading yet\n");
        return;
      }
      x = h;
    } else {
      x = v.toFloat();
      if (!(x >= 0 && x <= 360)) {
        server.send(400, "text/plain", "hue must be 0-360\n");
        return;
      }
    }
    (k ? hi : lo) = x;
  }
  if (server.hasArg("reset")) {
    lo = IDX_DEFAULT_LOW;
    hi = IDX_DEFAULT_HIGH;
  }
  if (fabsf(lo - hi) < 1) {
    server.send(400, "text/plain", "low and high hue must differ\n");
    return;
  }
  if (lo != idxLow || hi != idxHigh) {
    idxLow = lo;
    idxHigh = hi;
    idxSave();
  }
  handleState();
}

void handleState() {
  Colour c = colourOf(last);
  char tbuf[12] = "";
  float mod = 0;
  if (timeValid()) {
    time_t now = time(nullptr);
    struct tm t;
    localtime_r(&now, &t);
    strftime(tbuf, sizeof tbuf, "%H:%M:%S", &t);
    mod = t.tm_hour * 60 + t.tm_min + t.tm_sec / 60.0f;
  }
  unsigned long ovLeft =
      overrideActive ? (OVERRIDE_MS - (millis() - overrideStart)) / 1000UL : 0;
  float idxV = 0, idxH = 0;
  bool idxOk = co2Index(idxV);
  smoothHue(idxH);
  char b[900];
  snprintf(b, sizeof b,
           "{\"version\":\"" FW_VERSION "\",\"drag\":true,\"seq\":%lu,\"age\":%lu,\"saturated\":%s,\"i2c_errors\":%lu,"
           "\"off\":[%u,%u,%u,%u],\"on\":[%u,%u,%u,%u],"
           "\"colour\":{\"ok\":%s,\"net\":[%d,%d,%d],\"rgb\":[%u,%u,%u],\"hex\":\"%s\",\"hue\":%.1f},"
           "\"wb\":[%.4f,%.4f,%.4f],\"dim\":{\"level\":%.1f,\"min\":%u,\"max\":%u,"
           "\"source\":\"%s\",\"override_left\":%lu},"
           "\"time_valid\":%s,\"time\":\"%s\",\"minute\":%.2f,"
           "\"co2idx\":{\"ok\":%s,\"value\":%.1f,\"hue\":%.1f,\"n\":%u,\"low\":%.1f,\"high\":%.1f}}",
           (unsigned long)last.seq,
           haveReading ? (unsigned long)((millis() - last.millisAt) / 1000) : 0UL,
           last.saturated ? "true" : "false", (unsigned long)i2cErrors, last.off.r,
           last.off.g, last.off.b, last.off.c, last.on.r, last.on.g, last.on.b, last.on.c,
           c.ok ? "true" : "false", c.net[0], c.net[1], c.net[2], c.rgb[0], c.rgb[1],
           c.rgb[2], c.hex, c.hue, wbGain[0], wbGain[1], wbGain[2], dimOut, DIM_MIN_PCT,
           DIM_MAX_PCT, dimSource, ovLeft, timeValid() ? "true" : "false", tbuf, mod,
           idxOk ? "true" : "false", idxV, idxH, hueN, idxLow, idxHigh);
  server.send(200, "application/json", b);
}

void handleWhite() {
  if (server.hasArg("reset")) {
    wbGain[0] = wbGain[1] = wbGain[2] = 1;
    wbSave();
  } else if (server.hasArg("set")) {
    Colour c = colourOf(last);
    int mx = max(c.net[0], max(c.net[1], c.net[2]));
    if (!c.ok || c.net[0] < 20 || c.net[1] < 20 || c.net[2] < 20) {
      server.send(409, "text/plain", "reading too weak or invalid for a white reference\n");
      return;
    }
    for (int i = 0; i < 3; i++) wbGain[i] = (float)mx / c.net[i];
    wbSave();
  }
  hueN = 0;  // hues before the change are not comparable
  Colour c2 = colourOf(last);
  if (c2.ok) { hueHist[0] = c2.hue; hueHead = 1; hueN = 1; }
  handleState();
}

// ---- /metrics --------------------------------------------------------------
void handleMetrics() {
  String s;
  s.reserve(1800);
  auto line = [&](const char *name, const char *labels, double v) {
    s += name;
    if (labels && *labels) { s += '{'; s += labels; s += '}'; }
    char num[24];
    snprintf(num, sizeof num, " %.0f\n", v);
    s += num;
  };
  s += F("# TYPE tcs34725_raw gauge\n");
  if (haveReading) {
    const Frame *fr[2] = {&last.off, &last.on};
    const char *led[2] = {"off", "on"};
    for (int i = 0; i < 2; i++) {
      if (!fr[i]->ok) continue;  // a failed frame is simply absent
      char l[48];
      uint16_t v[4] = {fr[i]->r, fr[i]->g, fr[i]->b, fr[i]->c};
      const char *ch[4] = {"red", "green", "blue", "clear"};
      for (int k = 0; k < 4; k++) {
        snprintf(l, sizeof l, "channel=\"%s\",led=\"%s\"", ch[k], led[i]);
        line("tcs34725_raw", l, v[k]);
      }
      snprintf(l, sizeof l, "led=\"%s\"", led[i]);
      line("tcs34725_avalid", l, fr[i]->avalid);
    }
    if (last.ts) line("tcs34725_reading_timestamp_seconds", "", (double)last.ts);
    line("tcs34725_reading_age_seconds", "", (millis() - last.millisAt) / 1000.0);
    line("tcs34725_saturated", "", last.saturated);
    Colour col = colourOf(last);
    if (col.ok) {
      char num[24];
      snprintf(num, sizeof num, " %.1f\n", col.hue);
      s += F("tcs34725_hue_degrees");  // white-balanced, display grade
      s += num;
    }
    float idx;
    if (co2Index(idx)) {
      char num[24];
      snprintf(num, sizeof num, " %.1f\n", idx);
      s += F("co2watch_co2_index_percent");  // smoothed hue mapped low..high
      s += num;
    }
    line("co2watch_co2_index_low_hue_degrees", "", idxLow);
    line("co2watch_co2_index_high_hue_degrees", "", idxHigh);
  }
  line("tcs34725_reading_seq", "", last.seq);
  line("tcs34725_id_ok", "", last.idOk);
  line("tcs34725_atime", "", ATIME);
  line("tcs34725_gain", "", GAIN_X[GAIN_REG]);
  s += F("# TYPE tcs34725_i2c_errors_total counter\n");
  line("tcs34725_i2c_errors_total", "", i2cErrors);
  line("co2watch_pcf8574_present", "", pcfAddr != 0);
  {
    char num[24];
    snprintf(num, sizeof num, " %.1f\n", dimOut);
    s += F("aquarium_light_dim_percent");
    s += num;
  }
  line("aquarium_light_schedule_enabled", "", schedEnabled);
  line("aquarium_light_override_active", "", overrideActive);
  line("co2watch_time_valid", "", timeValid());
  line("aquarium_light_dim_min_percent", "", DIM_MIN_PCT);
  line("aquarium_light_dim_max_percent", "", DIM_MAX_PCT);
  s += F("# TYPE aquarium_light_dim_changes_total counter\n");
  line("aquarium_light_dim_changes_total", "", dimChanges);
  s += F("co2watch_build_info{version=\"" FW_VERSION "\"} 1\n");
  line("co2watch_uptime_seconds", "", millis() / 1000.0);
  line("co2watch_wifi_rssi_dbm", "", WiFi.RSSI());
  server.send(200, "text/plain; version=0.0.4", s);
}

// ---- setup / loop ----------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(200);
  dimmerBegin();  // first, so the light level is restored as early as possible
  Serial.println(F("\n\nco2watch firmware " FW_VERSION));
  Wire.begin(D2, D1);  // SDA, SCL
  Wire.setClock(100000);  // long UTP run: stay at 100 kHz
  scanBus();
  setLeds(false);
  tcsInit();

  if (strlen(WIFI_SSID)) {
    WiFi.mode(WIFI_STA);
    WiFi.hostname(HOSTNAME);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    Serial.printf("WiFi: connecting to %s", WIFI_SSID);
    for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
      delay(250);
      Serial.print('.');
    }
    Serial.println();
    configTime(TZ_AMSTERDAM, "nl.pool.ntp.org", "pool.ntp.org");
    MDNS.begin(HOSTNAME);
    server.on("/metrics", handleMetrics);
    server.on("/dimmer", handleDimmer);
    server.on("/api/schedule", handleSchedule);
    server.on("/", handleRoot);
    server.on("/api/state", handleState);
    server.on("/white", handleWhite);
    server.on("/api/co2index", handleIndex);
    server.on("/read", []() { takeReading(); handleState(); });
    server.begin();
#ifdef OTA_PASS
    // Over-the-air updates, so the board can be reflashed without USB.
    ArduinoOTA.setHostname(HOSTNAME);
    ArduinoOTA.setPassword(OTA_PASS);
    ArduinoOTA.begin();
#endif
  } else {
    WiFi.mode(WIFI_OFF);
    Serial.println(F("WiFi: no secrets.h, serial only"));
  }
  takeReading();
  lastReadMs = millis();
}

void loop() {
  static wl_status_t prev = WL_IDLE_STATUS;
  if (strlen(WIFI_SSID)) {
    server.handleClient();
#ifdef OTA_PASS
    ArduinoOTA.handle();
#endif
    MDNS.update();
    wl_status_t st = WiFi.status();
    if (st != prev) {
      prev = st;
      if (st == WL_CONNECTED)
        Serial.printf("WiFi: connected, http://%s/metrics (http://%s.local/metrics)\n",
                      WiFi.localIP().toString().c_str(), HOSTNAME);
      else
        Serial.printf("WiFi: status %d\n", st);
    }
  }
  static uint32_t lastTick = 0;
  if (millis() - lastTick >= 1000) {
    lastTick = millis();
    schedTick();
  }
  if (millis() - lastReadMs >= READ_INTERVAL_MS) {
    lastReadMs = millis();
    takeReading();
  }
  static String cmd;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmd.length() > 1 && cmd[0] == 'd') applyDim(clampDim(cmd.substring(1).toInt()), true);
      cmd = "";
    } else if (cmd.length() == 0 && c == 'r') {
      takeReading();
    } else if (cmd.length() == 0 && c == 's') {
      scanBus();
    } else if (cmd.length() < 8) {
      cmd += c;
    }
  }
}
