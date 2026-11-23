# co2_rgbsensor_reading (co2watch)

Reading a CO2 drop checker with an RGB sensor and analysing the data with ML.

Self-learning anomaly detection for an aquarium CO2 drop checker, read by a
TCS34725 colour sensor on a Wemos D1 mini (ESPHome). Runs in Python on a small
Linux box or k8s node. Notifications only; it never controls equipment.

**Status: step 1 of 4** (design, data model, simulator, self-calibration).
Nothing has been tested on real tank data yet. See [docs/design.md](docs/design.md).

## What's here

| Path | What |
|---|---|
| `docs/design.md` | architecture diagram, options considered, assumptions |
| `src/co2watch/db.py` | SQLite schema, CSV import (backfill) and export |
| `src/co2watch/features.py` | hue, ratios, regular UTC grid, rolling features, local time |
| `src/co2watch/simulator.py` | synthetic tank + drop checker + sensor, with injectable anomalies |
| `src/co2watch/calibration.py` | self-calibration against the leaflet chart, drift report |
| `src/co2watch/config.py` | leaflet chart (KH4), priors and defaults |
| `tests/` | unit tests (`pytest`) |

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest

# 6 weeks of synthetic data with the standard anomaly set, loaded into a DB
co2watch simulate --days 42 --out data/sim --db data/co2watch.sqlite
co2watch calibrate --db data/co2watch.sqlite

# record a fluid change (starts a fresh calibration)
co2watch mark-calibration --db data/co2watch.sqlite --kind fluid_change --when "2026-11-24 10:00"

# backfill / export
co2watch import-csv my_history.csv --db data/co2watch.sqlite
co2watch export-csv out.csv --db data/co2watch.sqlite
```

CSV import expects a `ts` column (ISO time with offset, or UTC) and `red, green,
blue, clear`. Optional context columns: `light_on, co2_on, airstone_on,
temperature, ph`.

## Simulated anomalies

`stuck`, `dropout`, `jump`, `case_slipped`, `light_leak` (sensor/setup),
`missed_co2`, `overdose`, `solenoid_stuck_open`, `diffuser_decline` (real
problems), and `fluid_change` (a calibration event, not an anomaly). Each comes
with a labelled time window in `sim_labels.csv`.

## Coming next

Step 2: MQTT ingester and Kubernetes manifests. Step 3: detection and an
evaluation notebook. Step 4: Home Assistant buttons, retraining, LLM explanations.
Labelling, retraining and troubleshooting sections will be added with them.
