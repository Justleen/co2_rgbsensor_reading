# Detection (M2)

`src/co2watch/detect.py`, run with `co2watch score`. Classical and explainable:
hard-fault rules, a per-time-of-day robust baseline and daily summaries. No
labels are needed. Every alert carries a templated reason built only from
numbers the detectors computed; likely causes are worded as possibilities.

## Components

Every component gives a score per 5-minute slot where **1.0 = at the
threshold**. The combined score is the maximum; an alert is a run of slots at
or above `threshold` (default 1.0, `co2watch score --threshold`), with gaps of
up to 30 min merged. When a hard-fault rule fires it names the alert.

| Component | Fires when | Typical cause | Active from |
|---|---|---|---|
| `dropout` | no readings for more than 15 min, or the I2C error counter rises | D1 offline, Wi-Fi, scrape, I2C | first reading |
| `stuck` | all eight raw counts unchanged for 30 min (reason says whether `reading_seq` kept moving) | sensor/I2C frozen, or firmware hung | first reading |
| `saturated` | a channel at its max count (computed mask or `tcs34725_saturated`) | stray light, gain/ATIME | first reading |
| `config` | `tcs34725_id_ok` = 0, or gain/ATIME changed | wrong chip, dead bus, settings changed | first reading |
| `led_fail` | LED-lit (net) signal below 10 % of the last 24 h | LEDs not switching (PCF8574), sensor turned away | after 1 day |
| `clear_jump` | net signal more than 25 % away from the last 24 h | case slipped, sensor moved | after 1 day |
| `baseline_hue` | hue (30-min median) more than 4 robust sigmas (min 4 deg) from the same half-hour slot over the last 14 days, sustained 30 min; on CO2 ramps the allowance widens by slope x 30 min | CO2 high/low at an unusual time: overdose, solenoid stuck, bottle | 5 days of history |
| `baseline_ambient` | LEDs-off (stray) light more than 6 robust sigmas (min 60 counts) above the usual level for the slot, sustained 30 min | light leak, sensor moved, new lamp | 5 days of history |
| `beyond_peak` | hue more than 1.5 spreads past the usual daily peak (greener/yellower), any time of day, sustained 30 min | overdose, live | 5 days of history |
| `daily_peak` | the day's greenest stable hue more than 3 spreads (min 8 deg) from the usual peak; judged from 21:00 local | CO2 never reached normal, or too much | 5 days of history |
| `daily_night` | overnight bluest hue more than 3 spreads less blue than usual | CO2 did not drop overnight | 5 days of history |
| `daily_swing` | night-to-peak swing over the last 3 days more than 30 % smaller than usual | gradual: diffuser, bubble counter, regulator | 5 days of history |

"Robust sigma" = 1.4826 x MAD. Histories are per calibration epoch: after
`co2watch mark-calibration` (fluid change, sensor moved) the hue baselines and
daily summaries start again and stay silent for 5 days, and the setup checks
are quiet for 3 h around the event. The stray-light baseline ignores fluid
changes.

**Cold start:** for the first 5 days (and 5 days after a calibration event) only
the hard-fault rules can fire, as the project brief asked.

## Running it

```bash
co2watch score --vm http://victoriametrics.monitoring.svc.cluster.local:8428 --db data/labels.sqlite
co2watch alerts --db data/labels.sqlite
# replay a CSV as if it were a given moment
co2watch score --csv data/sim/sim_readings.csv --db /tmp/t.sqlite --now "2026-11-03 21:00" --days 60
```

`score` reads the last 16 days, runs everything, and writes alerts that were
active in the last 2 h to the `alerts` table (extending an open alert of the
same kind instead of duplicating it). Alerts are only logged; there are no
notifications.

## Evaluation on simulated data

`co2watch evaluate --seeds 5 --days 42`: five 6-week runs with the standard
anomaly set (one of each kind, plus a fluid change), and five clean runs. The
simulator includes 8 % day-to-day variation of CO2 injection; without it every
normal day is identical and detection looks better than it is.

*Time to alert* = from the anomaly's start until an alert could first be raised
live (daily summaries count only from the end of the day's window). An anomaly
counts as detected if an alert overlaps its window (1 h before to 8 h after).

| anomaly | category | detected / injected | recall | median time to alert (h) | detector |
|---|---|---|---|---|---|
| case_slipped | sensor_issue | 5/5 | 100 % | 0.3 | clear_jump |
| diffuser_decline | real_problem | 5/5 | 100 % | 57.0 | daily_peak |
| dropout | sensor_issue | 5/5 | 100 % | 0.1 | dropout |
| jump | sensor_issue | 5/5 | 100 % | 0.3 | clear_jump |
| light_leak | sensor_issue | 5/5 | 100 % | 0.8 | baseline_ambient |
| missed_co2 | real_problem | 5/5 | 100 % | 8.0 | daily_peak |
| overdose | real_problem | 5/5 | 100 % | 4.3 | daily_peak |
| solenoid_stuck_open | real_problem | 5/5 | 100 % | 0.0 | baseline_hue |
| stuck | sensor_issue | 5/5 | 100 % | 0.6 | stuck |

| run | weeks | false alarms | per week |
|---|---|---|---|
| anomalies-1 | 6 | 0 | 0.00 |
| clean-1 | 6 | 1 | 0.17 |
| anomalies-2 | 6 | 0 | 0.00 |
| clean-2 | 6 | 0 | 0.00 |
| anomalies-3 | 6 | 2 | 0.33 |
| clean-3 | 6 | 3 | 0.50 |
| anomalies-4 | 6 | 0 | 0.00 |
| clean-4 | 6 | 0 | 0.00 |
| anomalies-5 | 6 | 0 | 0.00 |
| clean-5 | 6 | 0 | 0.00 |

Overall: 0.10 false alarms per week.

**What this does and does not show.** It shows the detectors behave as
designed on the simulator, with every anomaly kind caught and few false alarms.
It does **not** show performance on the tank: the simulator is cleaner and more
regular than reality (no real PWM flicker, no glass optics, no plant or
schedule changes, a smooth sensor). The thresholds in degrees and counts (4 deg,
8 deg, 60 counts) are starting points and will need tuning on 2+ weeks of real,
labelled data (M6). Treat the real false-alarm rate as unknown until then.

Known weak spots, from the table: a gradual diffuser decline is caught only
after about two days (by design it is gradual); a missed CO2 day is reported by
the evening (about 8 h after the CO2 should have come on); an overdose takes
about 4 h to cross the live guard, partly because the drop checker itself lags
the tank by 1-2 h.
