"""Tracking domain models.

The platform owns orbital propagation and Doppler math. Server routes and
frontend code consume these dataclasses as contracts; UI components should not
reimplement the mission math for production.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal


# Neutral sample TLE (ISS-like 51° inclination LEO, fictitious epoch).
# Used only as a fallback when neither the operator's gss.yml nor the
# active mission has seeded `platform.tracking.tle`. Mission-specific
# TLEs (e.g. MAVERIC's actual orbital elements) live in the mission
# package and are seeded into runtime config by the mission's `build`
# hook — see mav_gss_lib/missions/maveric/tracking_defaults.py.
SAMPLE_TLE_LINE1 = "1 25544U 98067A   26001.50000000  .00000000  00000-0  00000-0 0  9990"
SAMPLE_TLE_LINE2 = "2 25544  51.6400   0.0000 0000000   0.0000   0.0000 15.50000000000007"
SPEED_OF_LIGHT_MPS = 299_792_458.0
EARTH_RADIUS_KM = 6378.137

DopplerMode = Literal["disconnected", "connected", "sim"]


@dataclass(frozen=True, slots=True)
class TrackingStation:
    id: str
    name: str
    lat_deg: float
    lon_deg: float
    alt_m: float
    min_elevation_deg: float


@dataclass(frozen=True, slots=True)
class TrackingTle:
    source: str
    name: str
    line1: str
    line2: str
    method: str = "manual"        # "manual" | "fetched" | "seed"
    fetched_at_ms: int = 0


@dataclass(frozen=True, slots=True)
class TrackingFrequencies:
    rx_hz: float
    tx_hz: float


@dataclass(frozen=True, slots=True)
class TrackingDisplay:
    day_night_map: bool = True


@dataclass(frozen=True, slots=True)
class TrackingConfig:
    enabled: bool
    selected_station_id: str
    stations: tuple[TrackingStation, ...]
    tle: TrackingTle
    frequencies: TrackingFrequencies
    display: TrackingDisplay

    @property
    def selected_station(self) -> TrackingStation:
        for station in self.stations:
            if station.id == self.selected_station_id:
                return station
        return self.stations[0]


@dataclass(frozen=True, slots=True)
class SatellitePoint:
    time_ms: int
    lat_deg: float
    lon_deg: float
    altitude_km: float


@dataclass(frozen=True, slots=True)
class LookAngles:
    elevation_deg: float
    azimuth_deg: float
    range_km: float
    range_rate_mps: float


@dataclass(frozen=True, slots=True)
class GroundTrack:
    trailing: tuple[tuple[SatellitePoint, ...], ...]
    upcoming: tuple[tuple[SatellitePoint, ...], ...]


@dataclass(frozen=True, slots=True)
class Footprint:
    center_lat_deg: float
    center_lon_deg: float
    radius_deg: float
    visible_from_station: bool


@dataclass(frozen=True, slots=True)
class SunPoint:
    lat_deg: float
    lon_deg: float


@dataclass(frozen=True, slots=True)
class DopplerCorrection:
    ts_ms: int
    station_id: str
    satellite: str
    mode: DopplerMode
    range_rate_mps: float
    rx_hz: float
    rx_shift_hz: float
    rx_tune_hz: float
    tx_hz: float
    tx_shift_hz: float
    tx_tune_hz: float


@dataclass(frozen=True, slots=True)
class ContactPass:
    id: str
    aos_ms: int
    max_ms: int
    los_ms: int
    duration_s: float
    start_elevation_deg: float
    max_elevation_deg: float
    end_elevation_deg: float
    aos_azimuth_deg: float
    max_azimuth_deg: float
    los_azimuth_deg: float
    range_km_at_max: float
    range_rate_mps_at_max: float


@dataclass(frozen=True, slots=True)
class PassSample:
    time_ms: int
    elevation_deg: float
    azimuth_deg: float
    range_km: float
    range_rate_mps: float


@dataclass(frozen=True, slots=True)
class PassDetail:
    summary: ContactPass
    samples: tuple[PassSample, ...]


@dataclass(frozen=True, slots=True)
class TrackingState:
    ts_ms: int
    config: TrackingConfig
    tle_epoch_ms: int
    satellite: SatellitePoint
    look: LookAngles
    contact_state: str
    above_horizon: bool
    visible_from_station: bool
    doppler: DopplerCorrection
    footprint: Footprint
    sun: SunPoint
    ground_track: GroundTrack
    upcoming_passes: tuple[ContactPass, ...] = field(default_factory=tuple)


def to_plain(value):
    """Convert tracking dataclasses to JSON-serializable containers."""
    return asdict(value)
