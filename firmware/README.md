# Firmware

`co2watch/` is the Arduino sketch for the Wemos D1 mini (ESP8266) that reads the
drop checker and drives the light dimmer.

## What it does

- Reads the TCS34725 every 5 minutes as two frames, LEDs off then on. The LEDs
  are switched through PCF8574 P0, which is found by an I2C scan. Gain (4x) and
  ATIME (0xC0, 154 ms) are fixed constants.
- Serves `/metrics` (Prometheus text, scraped by VictoriaMetrics job `co2watch`)
  with the names from `docs/design.md` section 4, plus `tcs34725_hue_degrees`,
  `aquarium_light_dim_*` and `co2watch_build_info`.
- Serves a web page on `/`: drop-checker colour (net = on - off, white
  balanced, as swatch, hex and hue), raw counts, and a dimmer slider.
  White balance is display-only; `/metrics` keeps the raw counts.
- Dimmer: D6 (GPIO12), 1 kHz PWM into a PWM-to-0-10V board, then the LCM-40
  DIM input. Manual only (`/dimmer?level=NN`, the page, or serial `dNN`),
  clamped to `DIM_MIN_PCT`..`DIM_MAX_PCT` (compile-time). The last level is
  stored in flash and restored at boot. During a reset D6 is low, so the light
  drops to its minimum for about a second.

- Light schedule (off until enabled on the page): 8 points (24-hour HH:MM +
  level), each fading to the next one (smooth cosine, linear or step), wrapping
  at midnight. Time from `nl.pool.ntp.org` in Europe/Amsterdam (DST handled).
  A manual change pauses the schedule for 1 hour; without a valid time the
  light goes to 30 %. Stored in flash; API at `/api/schedule`.

Wiring: SDA D2, SCL D1, TCS34725 at 0x29, PCF8574 at 0x20 (on the bench board).

## Secrets

Create `co2watch/secrets.h` (gitignored):

```c
#define WIFI_SSID "..."
#define WIFI_PASS "..."
#define OTA_PASS  "..."   // enables over-the-air updates
```

Without it the sketch runs serial-only.

## Build and flash

Board `esp8266:esp8266:d1_mini` (ESP8266 core 3.1.x); no extra libraries.

- USB: `arduino-cli compile --upload -b esp8266:esp8266:d1_mini -p /dev/ttyUSB0 firmware/co2watch`
  (your user needs to be in the `dialout` group).
- Over WiFi: `firmware/flash-ota.sh [host]` (default `co2watch.fritz.box`).
  Set `ARDUINO_CLI` if `arduino-cli` is not on `PATH`.

Bump `FW_VERSION` with each change; it shows on the page and in `/metrics`.
