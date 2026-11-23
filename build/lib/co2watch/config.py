"""Central constants and defaults.

Everything here is an *assumption* or a *prior*, not a measured fact about the
tank. Values the system can learn (anchors, baselines) live in the database,
not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

TZ_NAME = "Europe/Amsterdam"
TZ = ZoneInfo(TZ_NAME)

SAMPLE_PERIOD_MIN = 5

# ---------------------------------------------------------------------------
# Sensor and /metrics contract (the D1 serves these; VictoriaMetrics scrapes)
# ---------------------------------------------------------------------------
CHANNELS = ("red", "green", "blue", "clear")
LED_STATES = ("off", "on")  # two frames per reading, LEDs via PCF8574

# Metric names are an ASSUMPTION until the M1 firmware exists. Change them here.
METRIC_RAW = "tcs34725_raw"  # labels: channel=red|green|blue|clear, led=off|on
METRIC_READ_TS = "tcs34725_reading_timestamp_seconds"  # when the frame pair was taken
METRIC_READ_SEQ = "tcs34725_reading_seq"  # increments per reading (stuck firmware check)
METRIC_ATIME = "tcs34725_atime"  # register value, must stay fixed
METRIC_GAIN = "tcs34725_gain"  # 1, 4, 16 or 60, must stay fixed
METRIC_I2C_ERRORS = "tcs34725_i2c_errors_total"
# Controller states, if the controller reports them (open item).
CONTEXT_METRICS = {
    "light_on": "aquarium_light_on",
    "co2_on": "aquarium_co2_on",
    "airstone_on": "aquarium_airstone_on",
    "temperature": "aquarium_temperature_celsius",
    "ph": "aquarium_ph",
}
# Simulated or backfilled data is written with this label so it never mixes
# with the scraped series. Real scraped series have no "source" label.
SOURCE_LABEL = "source"


def saturation_limit(atime: int) -> int:
    """Max count per channel for an ATIME register value (datasheet)."""
    return min((256 - atime) * 1024, 65535)

# Drop checker reference fluid carbonate hardness (dKH). Leaflet column "KH4".
REFERENCE_KH = 4.0


@dataclass(frozen=True)
class ChartBand:
    """One row of the drop checker leaflet colour chart (KH4 column)."""

    co2_mg_l: float
    ph: float
    chart_hue_deg: float  # hue of the printed colour, sampled from the leaflet image
    name: str
    verdict: str  # leaflet's verdict: "low", "borderline", "target", "high"


# Sampled from the leaflet image (screen sRGB, HSV hue). These are only used for
# the ORDER and relative SPACING of the bands. The sensor does not see these
# exact hues: it looks through fluid, plastic and glass with its own filters.
CHART_KH4: tuple[ChartBand, ...] = (
    ChartBand(3.0, 7.6, 203.0, "blue", "low"),
    ChartBand(8.0, 7.2, 186.0, "blue-green", "low"),
    ChartBand(12.0, 7.0, 167.0, "teal-green", "borderline"),
    ChartBand(20.0, 6.8, 115.0, "green", "target"),
    ChartBand(50.0, 6.4, 64.0, "lime", "high"),
)


def co2_from_ph(ph: float, kh: float = REFERENCE_KH) -> float:
    """Standard aquarium approximation: CO2 [mg/l] = 3 * KH * 10^(7 - pH)."""
    return 3.0 * kh * 10 ** (7.0 - ph)


def ph_from_co2(co2_mg_l: float, kh: float = REFERENCE_KH) -> float:
    import math

    return 7.0 - math.log10(co2_mg_l / (3.0 * kh))


@dataclass
class CalibrationConfig:
    # Window (local time) where the overnight low is searched if no CO2/air
    # stone states are available. Overridden by context states when present.
    night_window_local: tuple[int, int] = (3, 8)
    # Rolling window used to find a stable plateau.
    plateau_window_min: int = 60
    # Max hue slope (deg/hour) for a window to count as stable.
    max_stable_slope_deg_h: float = 3.0
    # Assumed CO2 level in the drop checker at the overnight low.
    # The air stone degasses the tank, and the checker lags 1-2 h, so we assume
    # it sits near the leaflet's lowest band. This is a PRIOR, not a measurement.
    night_anchor_co2_mg_l: float = 3.0
    # Days of anchors used for the rolling fit.
    anchor_history_days: int = 14
    # Days after a calibration event before the new fit is trusted.
    settle_days: int = 3
    # Prior on the scale between sensor hue and chart hue (chart = a + b*sensor).
    scale_prior_mean: float = 1.0
    scale_prior_sd: float = 0.35


@dataclass
class TankSchedule:
    """Default schedule used by the simulator (local time). Real schedules are
    read from context entities; this is only for synthetic data."""

    lights_on: float = 13.0  # hour, local
    lights_off: float = 20.0
    co2_lead_h: float = 2.0  # CO2 on this long before lights on
    co2_stop_before_off_h: float = 2.0  # CO2 off this long before lights off
    water_change_hour: float = 11.0
    sun_hour: float = 15.5  # stray sunlight bump centre
    extra: dict = field(default_factory=dict)

    @property
    def co2_on(self) -> float:
        return self.lights_on - self.co2_lead_h

    @property
    def co2_off(self) -> float:
        return self.lights_off - self.co2_stop_before_off_h


BAND_NAMES = tuple(b.name for b in CHART_KH4)
