// co2watch firmware for a Wemos D1 mini (ESP8266).
//
// Sensor: a TCS34725 looks at a CO2 drop checker. Each reading (every 5 min) is
// two frames back to back, LEDs off (ambient) then on; the LEDs are switched by
// PCF8574 P0 (found by an I2C scan). Gain and ATIME are fixed. Raw counts go to
// /metrics (Prometheus text, scraped by VictoriaMetrics; see docs/design.md 4).
// Wiring: SDA = D2, SCL = D1, TCS34725 at 0x29, PCF8574 at 0x20-0x27/0x38-0x3F.
//
// Colour (display only): net = on - off, white-balanced, as hex/hue. The CO2
// index maps the hue (average of the last 3 readings) linearly between a Low
// (0 %) and High (100 %) hue; not clamped.
//
// Light: D6 (GPIO12) 1 kHz PWM -> PWM-to-0-10V board -> Mean Well LCM-40 DIM.
// The light always follows the schedule: 8 points (time + level) with a smooth /
// linear / step fade to the next point, wrapping at midnight, local time from
// NTP (Europe/Amsterdam, DST via the TZ rule). "Set to max" (/dimmer?level=100)
// holds a level for 1 hour, then the schedule resumes; without a valid time the
// light sits at SAFE_PCT. During a reset D6 is low (LCM minimum) for ~1 s.
//
// Settings (schedule, hours at 100 %, white balance, index ends) live in one
// struct in EEPROM. Web page at / (page.html -> page.h via gen_page.py).
// Serial (115200): I2C scan at boot, every reading; 'r' = read now, 's' = scan.

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
#define FW_VERSION "0.7.0"

// ---- light ------------------------------------------------------------------
static const uint8_t DIM_PIN = D6;          // GPIO12
static const uint32_t DIM_PWM_HZ = 1000;    // converter accepts 22 Hz - 20 kHz

// ---- CO2 index (defaults: leaflet 3 mg/l hue; High as set on the D1) -------
static const float IDX_DEFAULT_LOW = 203, IDX_DEFAULT_HIGH = 69;
static const uint8_t IDX_SMOOTH = 3;  // readings averaged (3 x 5 min)

// ---- schedule ---------------------------------------------------------------
static const char *TZ_AMSTERDAM = "CET-1CEST,M3.5.0,M10.5.0/3";
static const float SAFE_PCT = 30;                       // no valid time
static const uint32_t OVERRIDE_MS = 60UL * 60UL * 1000UL;  // "Set to max" hold
enum Ramp : uint8_t { RAMP_SMOOTH = 0, RAMP_LINEAR = 1, RAMP_STEP = 2 };
struct SchedPoint { uint16_t min; uint8_t level; uint8_t ramp; };
// Default = the schedule saved on the D1 on 2026-11-26 23:08 (7 h at 100 %
// 11:00-18:00; 18 % at 09:00, 40 % at 09:50; 40 % at 18:30, 25 % at 19:55, off 21:00).
static const SchedPoint SCHED_DEFAULT[8] = {
    {420, 4, 1},    {540, 18, 0},  {590, 40, 0},  {660, 100, 0},
    {1080, 100, 0}, {1110, 40, 0}, {1195, 25, 0}, {1260, 0, 1}};

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
float dimOut = 0;       // what D6 outputs, in %
bool overrideActive = false;
uint32_t overrideStart = 0;
const char *dimSource = "safe";  // schedule / override / safe
float hueHist[IDX_SMOOTH];
uint8_t hueN = 0, hueHead = 0;
uint32_t lastReadMs = 0;
bool haveReading = false;
ESP8266WebServer server(80);

// All persistent settings, stored as one block at EEPROM address 0.
static const uint8_t SETTINGS_VERSION = 0x60;  // bump when the layout changes
struct Settings {
  uint8_t version;
  SchedPoint sched[8];
  uint8_t maxhHalf;  // hours at 100 %, in half hours
  float wb[3];       // white-balance gains r, g, b
  float idxLow, idxHigh;
} cfg;

void settingsDefaults() {
  cfg.version = SETTINGS_VERSION;
  memcpy(cfg.sched, SCHED_DEFAULT, sizeof cfg.sched);
  cfg.maxhHalf = 14;
  cfg.wb[0] = cfg.wb[1] = cfg.wb[2] = 1;
  cfg.idxLow = IDX_DEFAULT_LOW;
  cfg.idxHigh = IDX_DEFAULT_HIGH;
}

void settingsSave() {
  EEPROM.put(0, cfg);
  EEPROM.commit();
}

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

// 0x44 = TCS34721/5, 0x4D = TCS34723/7 (the board in hand).
bool tcsIdOk(uint8_t &id) { return tcsRead(REG_ID, &id, 1) && (id == 0x44 || id == 0x4D); }

bool tcsInit() {
  uint8_t id = 0;
  last.idOk = tcsIdOk(id);
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
  r.idOk = tcsIdOk(id);
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
  r.ts = timeValid() ? time(nullptr) : 0;
  r.millisAt = millis();
  last = r;
  haveReading = true;
  Colour col = colourOf(r);
  if (col.ok) {
    hueHist[hueHead] = col.hue;
    hueHead = (hueHead + 1) % IDX_SMOOTH;
    if (hueN < IDX_SMOOTH) hueN++;
  } else {
    hueN = 0;  // a failed reading must not leave a stale index in /metrics
    hueHead = 0;
  }

  Serial.printf(
      "#%lu off c=%u r=%u g=%u b=%u%s | on c=%u r=%u g=%u b=%u%s | net c=%d r=%d g=%d b=%d%s errs=%lu\n",
      (unsigned long)r.seq, r.off.c, r.off.r, r.off.g, r.off.b,
      r.off.ok ? "" : " (FAIL)", r.on.c, r.on.r, r.on.g, r.on.b,
      r.on.ok ? "" : " (FAIL)", (int)r.on.c - r.off.c, (int)r.on.r - r.off.r,
      (int)r.on.g - r.off.g, (int)r.on.b - r.off.b,
      r.saturated ? " SATURATED" : "", (unsigned long)i2cErrors);
}

// ---- light --------------------------------------------------------------------
void setOutput(float pct) {
  dimOut = constrain(pct, 0.0f, 100.0f);
  analogWrite(DIM_PIN, (int)(dimOut * 10 + 0.5f));  // range 0..1000, 0.1 % steps
}

// "Set to max" (or any level via /dimmer): hold it for 1 hour, then the
// schedule takes over again.
void holdLevel(float pct) {
  setOutput(pct);
  overrideActive = true;
  overrideStart = millis();
  dimSource = "override";
  Serial.printf("light: %.1f %% for 1 h\n", dimOut);
}

// ---- time / schedule ------------------------------------------------------------
bool timeValid() { return time(nullptr) > 1700000000; }

// Local time; false until NTP has set the clock.
bool localNow(struct tm &t) {
  time_t now = time(nullptr);
  localtime_r(&now, &t);
  return now > 1700000000;
}

float minuteOfDay(const struct tm &t) { return t.tm_hour * 60 + t.tm_min + t.tm_sec / 60.0f; }

float schedLevelAt(float m) {
  int i = 7;  // before the first point: still in the segment from the last one
  for (int k = 0; k < 8; k++)
    if (cfg.sched[k].min <= m) i = k;
  int j = (i + 1) % 8;
  float span = (float)cfg.sched[j].min - cfg.sched[i].min;
  if (span <= 0) span += 1440;
  float d = m - cfg.sched[i].min;
  if (d < 0) d += 1440;
  float f = d / span;
  if (f > 1) f = 1;
  float a = cfg.sched[i].level, b = cfg.sched[j].level;
  if (cfg.sched[i].ramp == RAMP_STEP) return a;
  if (cfg.sched[i].ramp == RAMP_SMOOTH) f = (1 - cosf(PI * f)) / 2;
  return a + (b - a) * f;
}

bool schedValid(const SchedPoint *p) {
  for (int k = 0; k < 8; k++) {
    if (p[k].min >= 1440 || p[k].level > 100 || p[k].ramp > RAMP_STEP) return false;
    if (k && p[k].min <= p[k - 1].min) return false;  // strictly increasing
  }
  return true;
}

// Called every second.
void schedTick() {
  if (overrideActive) {
    if (millis() - overrideStart < OVERRIDE_MS) return;
    overrideActive = false;
    Serial.println(F("light: override over, back to the schedule"));
  }
  struct tm t;
  if (!localNow(t)) {
    dimSource = "safe";
    setOutput(SAFE_PCT);
  } else {
    dimSource = "schedule";
    setOutput(schedLevelAt(minuteOfDay(t)));
  }
}

void sendSchedule() {
  String b = "{\"pts\":[";
  for (int k = 0; k < 8; k++) {
    if (k) b += ',';
    b += '[';
    b += cfg.sched[k].min; b += ','; b += cfg.sched[k].level; b += ','; b += cfg.sched[k].ramp;
    b += ']';
  }
  b += "],\"maxh\":";
  b += String(cfg.maxhHalf / 2.0f, 1);
  b += "}";
  server.send(200, "application/json", b);
}

// GET: current schedule. POST args: pts="min,level,ramp;..." (8), maxh=hours,
// reset=1 (default points), resume=1 (end a "Set to max" hold now).
void handleSchedule() {
  if (server.hasArg("reset")) memcpy(cfg.sched, SCHED_DEFAULT, sizeof cfg.sched);
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
    memcpy(cfg.sched, p, sizeof cfg.sched);
  }
  if (server.hasArg("resume")) overrideActive = false;
  if (server.hasArg("maxh")) {
    float h = server.arg("maxh").toFloat();
    if (!(h >= 0.5f && h <= 16.0f)) {
      server.send(400, "text/plain", "maxh must be 0.5-16 hours\n");
      return;
    }
    cfg.maxhHalf = (uint8_t)(h * 2 + 0.5f);
  }
  if (server.hasArg("reset") || server.hasArg("pts") || server.hasArg("maxh")) settingsSave();
  schedTick();
  sendSchedule();
}

void lightBegin() {
  analogWriteFreq(DIM_PWM_HZ);
  analogWriteRange(1000);
  setOutput(SAFE_PCT);  // until NTP gives the time
}

void settingsLoad() {
  EEPROM.begin(sizeof(Settings));
  EEPROM.get(0, cfg);
  bool ok = cfg.version == SETTINGS_VERSION && schedValid(cfg.sched) &&
            cfg.maxhHalf >= 1 && cfg.maxhHalf <= 32 &&
            cfg.idxLow >= 0 && cfg.idxLow <= 360 && cfg.idxHigh >= 0 &&
            cfg.idxHigh <= 360 && fabsf(cfg.idxLow - cfg.idxHigh) >= 1;
  for (int i = 0; i < 3; i++) ok = ok && cfg.wb[i] > 0.01f && cfg.wb[i] < 100.0f;
  if (!ok) settingsDefaults();  // blank, corrupt or an older layout
}

// POST /dimmer?level=0-100: hold that level for 1 hour ("Set to max" = 100).
void handleDimmer() {
  if (server.hasArg("level")) {
    String v = server.arg("level");
    char *end;
    long pct = strtol(v.c_str(), &end, 10);
    if (v.length() == 0 || *end != '\0' || pct < 0 || pct > 100) {
      server.send(400, "text/plain", "level must be an integer 0-100\n");
      return;
    }
    holdLevel(pct);
  }
  char body[80];
  snprintf(body, sizeof body, "{\"level\":%.1f,\"source\":\"%s\"}\n", dimOut, dimSource);
  server.send(200, "application/json", body);
}

// ---- colour (display only) -------------------------------------------------

Colour colourOf(const Reading &r) {
  Colour c;
  if (!haveReading || !r.off.ok || !r.on.ok || r.saturated) return c;
  c.net[0] = (int)r.on.r - r.off.r;
  c.net[1] = (int)r.on.g - r.off.g;
  c.net[2] = (int)r.on.b - r.off.b;
  float v[3], mx = 0, mn = 1e9;
  for (int i = 0; i < 3; i++) {
    v[i] = (c.net[i] > 0 ? c.net[i] : 0) * cfg.wb[i];
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
  if (!smoothHue(h) || fabsf(cfg.idxLow - cfg.idxHigh) < 1) return false;
  idx = (cfg.idxLow - h) / (cfg.idxLow - cfg.idxHigh) * 100.0f;
  return true;
}

// POST /api/co2index: low=deg, high=deg, low=now / high=now (use the smoothed
// hue), reset=1 (defaults). Always answers with the current index state.
void handleIndex() {
  float lo = cfg.idxLow, hi = cfg.idxHigh, h;
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
  if (lo != cfg.idxLow || hi != cfg.idxHigh) {
    cfg.idxLow = lo;
    cfg.idxHigh = hi;
    settingsSave();
  }
  handleState();
}

// What the page needs (raw counts are in /metrics).
void handleState() {
  Colour c = colourOf(last);
  struct tm t;
  bool tv = localNow(t);
  char tbuf[12] = "";
  if (tv) strftime(tbuf, sizeof tbuf, "%H:%M:%S", &t);
  unsigned long ovLeft =
      overrideActive ? (OVERRIDE_MS - (millis() - overrideStart)) / 1000UL : 0;
  float idxV = 0, idxH = 0;
  bool idxOk = co2Index(idxV);
  smoothHue(idxH);
  char b[480];
  snprintf(b, sizeof b,
           "{\"version\":\"" FW_VERSION "\",\"drag\":true,\"seq\":%lu,\"age\":%lu,"
           "\"saturated\":%s,\"i2c_errors\":%lu,"
           "\"colour\":{\"ok\":%s,\"hex\":\"%s\",\"hue\":%.1f},"
           "\"dim\":{\"level\":%.1f,\"source\":\"%s\",\"override_left\":%lu},"
           "\"time_valid\":%s,\"time\":\"%s\",\"minute\":%.2f,"
           "\"co2idx\":{\"ok\":%s,\"value\":%.1f,\"hue\":%.1f,\"low\":%.1f,\"high\":%.1f}}",
           (unsigned long)last.seq,
           haveReading ? (unsigned long)((millis() - last.millisAt) / 1000) : 0UL,
           last.saturated ? "true" : "false", (unsigned long)i2cErrors,
           c.ok ? "true" : "false", c.hex, c.hue, dimOut, dimSource, ovLeft,
           tv ? "true" : "false", tbuf, tv ? minuteOfDay(t) : 0.0f,
           idxOk ? "true" : "false", idxV, idxH, cfg.idxLow, cfg.idxHigh);
  server.send(200, "application/json", b);
}

void handleWhite() {
  if (server.hasArg("reset")) {
    cfg.wb[0] = cfg.wb[1] = cfg.wb[2] = 1;
    settingsSave();
  } else if (server.hasArg("set")) {
    Colour c = colourOf(last);
    int mx = max(c.net[0], max(c.net[1], c.net[2]));
    if (!c.ok || c.net[0] < 20 || c.net[1] < 20 || c.net[2] < 20) {
      server.send(409, "text/plain", "reading too weak or invalid for a white reference\n");
      return;
    }
    for (int i = 0; i < 3; i++) cfg.wb[i] = (float)mx / c.net[i];
    settingsSave();
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
  auto line = [&](const char *name, const char *labels, double v, int decimals = 0) {
    s += name;
    if (labels && *labels) { s += '{'; s += labels; s += '}'; }
    char num[24];
    snprintf(num, sizeof num, " %.*f\n", decimals, v);
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
    if (col.ok) line("tcs34725_hue_degrees", "", col.hue, 1);  // white-balanced
    float idx;
    if (co2Index(idx)) line("co2watch_co2_index_percent", "", idx, 1);
    line("co2watch_co2_index_low_hue_degrees", "", cfg.idxLow, 1);
    line("co2watch_co2_index_high_hue_degrees", "", cfg.idxHigh, 1);
  }
  line("tcs34725_reading_seq", "", last.seq);
  line("tcs34725_id_ok", "", last.idOk);
  line("tcs34725_atime", "", ATIME);
  line("tcs34725_gain", "", GAIN_X[GAIN_REG]);
  s += F("# TYPE tcs34725_i2c_errors_total counter\n");
  line("tcs34725_i2c_errors_total", "", i2cErrors);
  line("co2watch_pcf8574_present", "", pcfAddr != 0);
  line("aquarium_light_dim_percent", "", dimOut, 1);
  line("aquarium_light_override_active", "", overrideActive);
  line("co2watch_time_valid", "", timeValid());
  s += F("co2watch_build_info{version=\"" FW_VERSION "\"} 1\n");
  line("co2watch_uptime_seconds", "", millis() / 1000.0);
  line("co2watch_wifi_rssi_dbm", "", WiFi.RSSI());
  server.send(200, "text/plain; version=0.0.4", s);
}

// ---- setup / loop ----------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(200);
  lightBegin();  // first, so D6 leaves its reset state as early as possible
  settingsLoad();
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
  while (Serial.available()) {
    char c = Serial.read();
    if (c == 'r') takeReading();
    if (c == 's') scanBus();
  }
}
