#!/usr/bin/env bash
# Build the co2watch sketch and push it to the D1 over WiFi (ArduinoOTA).
# Usage: firmware/flash-ota.sh [host]   (default aqua.fritz.box)
# Needs arduino-cli with the esp8266 core, and OTA_PASS in co2watch/secrets.h.
set -euo pipefail
HOST=${1:-aqua.fritz.box}
DIR=$(cd "$(dirname "$0")" && pwd)
CLI=${ARDUINO_CLI:-arduino-cli}
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
PASS=$(sed -n 's/^#define OTA_PASS "\(.*\)"/\1/p' "$DIR/co2watch/secrets.h")
[ -n "$PASS" ] || { echo "OTA_PASS missing in co2watch/secrets.h" >&2; exit 1; }
ESPOTA=$(find ~/.arduino15/packages/esp8266/hardware -name espota.py | sort | tail -1)
python3 "$DIR/gen_page.py"
"$CLI" compile -b esp8266:esp8266:d1_mini --output-dir "$OUT" "$DIR/co2watch"
IP=$(getent hosts "$HOST" | awk '{print $1; exit}')
python3 "$ESPOTA" -i "${IP:-$HOST}" -p 8266 -a "$PASS" -f "$OUT/co2watch.ino.bin"
