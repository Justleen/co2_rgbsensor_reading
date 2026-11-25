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
#define FW_VERSION "0.3.0"

// ---- dimmer limits (edit and reflash to change) -----------------------------
static const uint8_t DIM_PIN = D6;          // GPIO12
static const uint32_t DIM_PWM_HZ = 1000;    // converter accepts 22 Hz - 20 kHz
static const uint8_t DIM_MIN_PCT = 0;       // lowest level a request can set
static const uint8_t DIM_MAX_PCT = 100;     // highest level a request can set
static const uint8_t DIM_FIRST_BOOT_PCT = 0;  // used until a level is ever set
static const uint8_t EEPROM_MAGIC = 0xD1;
static const uint8_t EEPROM_WB_MAGIC = 0xB4;  // byte 2; gains at bytes 4..15

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
uint8_t dimPct = 0;
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

void applyDim(uint8_t pct, bool save) {
  dimPct = clampDim(pct);
  analogWrite(DIM_PIN, dimPct * 10);  // range 0..1000
  if (save) {
    dimChanges++;
    EEPROM.write(0, EEPROM_MAGIC);
    EEPROM.write(1, dimPct);
    EEPROM.commit();
  }
  Serial.printf("dimmer: %u %% (limits %u-%u)\n", dimPct, DIM_MIN_PCT, DIM_MAX_PCT);
}

void dimmerBegin() {
  analogWriteFreq(DIM_PWM_HZ);
  analogWriteRange(1000);
  EEPROM.begin(32);
  uint8_t pct = DIM_FIRST_BOOT_PCT;
  if (EEPROM.read(0) == EEPROM_MAGIC) pct = EEPROM.read(1);
  applyDim(pct, false);
  wbLoad();
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
  char body[96];
  snprintf(body, sizeof body, "{\"level\":%u,\"min\":%u,\"max\":%u}\n", dimPct,
           DIM_MIN_PCT, DIM_MAX_PCT);
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
static const char PAGE[] PROGMEM = R"HTML(<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>co2watch</title><style>
:root{--bg:#f6f6f4;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--bd:#ddd}
@media(prefers-color-scheme:dark){:root{--bg:#161615;--fg:#eee;--mut:#9a9a94;--card:#222220;--bd:#3a3a37}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
main{max-width:480px;margin:0 auto;padding:16px}
section{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px;margin-bottom:12px}
h1{font-size:20px;margin:4px 0 12px}h2{font-size:15px;margin:0 0 10px}
#sw{height:110px;border-radius:8px;border:1px solid var(--bd);background:#000}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.big{font-size:22px;font-variant-numeric:tabular-nums}
small,.mut{color:var(--mut)}input[type=range]{flex:1;min-width:160px}
button{font:inherit;padding:6px 12px;border-radius:6px;border:1px solid var(--bd);background:var(--bg);color:var(--fg)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}td,th{text-align:right;padding:2px 6px}th:first-child,td:first-child{text-align:left}
</style></head><body><main>
<h1>co2watch <small id="ver"></small></h1>
<section><h2>Drop checker colour</h2>
<div id="sw"></div>
<div class="row" style="margin-top:10px"><span class="big" id="hex">-</span><span class="big" id="hue">-</span></div>
<small id="info">loading...</small>
<table style="margin-top:8px"><tr><th></th><th>red</th><th>green</th><th>blue</th><th>clear</th></tr>
<tr><td>LEDs off</td><td id="o0"></td><td id="o1"></td><td id="o2"></td><td id="o3"></td></tr>
<tr><td>LEDs on</td><td id="n0"></td><td id="n1"></td><td id="n2"></td><td id="n3"></td></tr>
<tr><td>net</td><td id="d0"></td><td id="d1"></td><td id="d2"></td><td></td></tr>
<tr><td>white gain</td><td id="g0"></td><td id="g1"></td><td id="g2"></td><td></td></tr></table>
<div class="row" style="margin-top:10px"><button onclick="act('/read')">Take reading now</button>
<button onclick="if(confirm('Use the current reading as white? Do this with the sensor on the white backing.'))act('/white?set=1')">Use as white reference</button>
<button onclick="act('/white?reset=1')">Reset white</button></div>
</section>
<section><h2>Light dimmer</h2>
<div class="row"><input type="range" id="lv" min="0" max="100" oninput="lvo.textContent=this.value+' %'">
<span class="big" id="lvo">-</span><button onclick="setDim()">Set</button></div>
<small id="dinfo"></small></section>
</main><script>
const $=id=>document.getElementById(id);let busy=0;
function show(s){
 $('ver').textContent='v'+s.version;
 $('sw').style.background=s.colour.ok?s.colour.hex:'repeating-linear-gradient(45deg,#888 0 8px,#aaa 8px 16px)';
 $('hex').textContent=s.colour.ok?s.colour.hex:'no colour';
 $('hue').textContent=s.colour.ok?'hue '+s.colour.hue.toFixed(1)+'°':'';
 $('info').textContent=s.seq?('reading #'+s.seq+', '+s.age+' s ago'+(s.saturated?', SATURATED':'')+', i2c errors '+s.i2c_errors):'no reading yet';
 for(let i=0;i<4;i++){$('o'+i).textContent=s.off[i];$('n'+i).textContent=s.on[i];}
 for(let i=0;i<3;i++){$('d'+i).textContent=s.colour.net[i];$('g'+i).textContent=s.wb[i].toFixed(3);}
 const lv=$('lv');lv.min=s.dim.min;lv.max=s.dim.max;
 if(document.activeElement!==lv){lv.value=s.dim.level;$('lvo').textContent=s.dim.level+' %';}
 $('dinfo').textContent='now '+s.dim.level+' %, limits '+s.dim.min+'-'+s.dim.max+' %';
}
async function load(){if(busy)return;try{show(await (await fetch('/api/state')).json())}catch(e){$('info').textContent='offline?'}}
async function act(u){busy=1;try{await fetch(u,{method:'POST'})}finally{busy=0;load()}}
function setDim(){act('/dimmer?level='+$('lv').value)}
load();setInterval(load,5000);
</script></body></html>)HTML";

void handleRoot() { server.send_P(200, "text/html", PAGE); }

void handleState() {
  Colour c = colourOf(last);
  char b[600];
  snprintf(b, sizeof b,
           "{\"version\":\"" FW_VERSION "\",\"seq\":%lu,\"age\":%lu,\"saturated\":%s,\"i2c_errors\":%lu,"
           "\"off\":[%u,%u,%u,%u],\"on\":[%u,%u,%u,%u],"
           "\"colour\":{\"ok\":%s,\"net\":[%d,%d,%d],\"rgb\":[%u,%u,%u],\"hex\":\"%s\",\"hue\":%.1f},"
           "\"wb\":[%.4f,%.4f,%.4f],\"dim\":{\"level\":%u,\"min\":%u,\"max\":%u}}",
           (unsigned long)last.seq,
           haveReading ? (unsigned long)((millis() - last.millisAt) / 1000) : 0UL,
           last.saturated ? "true" : "false", (unsigned long)i2cErrors, last.off.r,
           last.off.g, last.off.b, last.off.c, last.on.r, last.on.g, last.on.b, last.on.c,
           c.ok ? "true" : "false", c.net[0], c.net[1], c.net[2], c.rgb[0], c.rgb[1],
           c.rgb[2], c.hex, c.hue, wbGain[0], wbGain[1], wbGain[2], dimPct, DIM_MIN_PCT,
           DIM_MAX_PCT);
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
  }
  line("tcs34725_reading_seq", "", last.seq);
  line("tcs34725_id_ok", "", last.idOk);
  line("tcs34725_atime", "", ATIME);
  line("tcs34725_gain", "", GAIN_X[GAIN_REG]);
  s += F("# TYPE tcs34725_i2c_errors_total counter\n");
  line("tcs34725_i2c_errors_total", "", i2cErrors);
  line("co2watch_pcf8574_present", "", pcfAddr != 0);
  line("aquarium_light_dim_percent", "", dimPct);
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
    configTime(0, 0, "pool.ntp.org", "time.google.com");
    MDNS.begin(HOSTNAME);
    server.on("/metrics", handleMetrics);
    server.on("/dimmer", handleDimmer);
    server.on("/", handleRoot);
    server.on("/api/state", handleState);
    server.on("/white", handleWhite);
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
