"""Skyfield-backed satellite tracking calculations."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Iterable

import numpy as np
from skyfield.api import EarthSatellite, load, wgs84

from .models import (
    EARTH_RADIUS_KM,
    SPEED_OF_LIGHT_MPS,
    ContactPass,
    DopplerCorrection,
    DopplerMode,
    Footprint,
    GroundTrack,
    LookAngles,
    PassDetail,
    PassSample,
    SatellitePoint,
    SunPoint,
    TrackingConfig,
    TrackingState,
    TrackingStation,
)

_TIMESCALE = load.timescale()


class TrackingError(ValueError):
    """Raised when tracking inputs cannot produce a valid state."""


def _time_from_ms(time_ms: int):
    return _TIMESCALE.from_datetime(datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc))


def _normalize_lon(value: float) -> float:
    lon = value
    while lon > 180.0:
        lon -= 360.0
    while lon < -180.0:
        lon += 360.0
    return lon


def _normalize_360(value: float) -> float:
    angle = value % 360.0
    if angle < 0:
        angle += 360.0
    return angle


def _deg_to_rad(value: float) -> float:
    return value * math.pi / 180.0


def _rad_to_deg(value: float) -> float:
    return value * 180.0 / math.pi


def satellite_from_lines(name: str, line1: str, line2: str) -> EarthSatellite:
    try:
        satellite = EarthSatellite(line1, line2, name, _TIMESCALE)
    except Exception as exc:
        raise TrackingError(f"invalid TLE: {exc}") from exc
    error = getattr(getattr(satellite, "model", None), "error", 0)
    if error:
        raise TrackingError(f"invalid TLE: SGP4 error {error}")
    return satellite


def build_satellite(config: TrackingConfig) -> EarthSatellite:
    return satellite_from_lines(config.tle.name, config.tle.line1, config.tle.line2)


def station_topos(station: TrackingStation):
    return wgs84.latlon(station.lat_deg, station.lon_deg, elevation_m=station.alt_m)


def tle_epoch_ms(satellite: EarthSatellite) -> int:
    return int(satellite.epoch.utc_datetime().timestamp() * 1000)


def satellite_point_at(satellite: EarthSatellite, time_ms: int) -> SatellitePoint:
    geocentric = satellite.at(_time_from_ms(time_ms))
    subpoint = geocentric.subpoint()
    return SatellitePoint(
        time_ms=int(time_ms),
        lat_deg=float(subpoint.latitude.degrees),
        lon_deg=_normalize_lon(float(subpoint.longitude.degrees)),
        altitude_km=float(subpoint.elevation.km),
    )


def look_angles_at(satellite: EarthSatellite, station: TrackingStation, time_ms: int) -> LookAngles:
    topocentric = (satellite - station_topos(station)).at(_time_from_ms(time_ms))
    alt, az, distance = topocentric.altaz()
    position_km = np.asarray(topocentric.position.km, dtype=float)
    velocity_km_s = np.asarray(topocentric.velocity.km_per_s, dtype=float)
    norm = float(np.linalg.norm(position_km))
    range_rate_mps = 0.0
    if norm > 0:
        range_rate_mps = float(np.dot(position_km, velocity_km_s) / norm * 1000.0)
    return LookAngles(
        elevation_deg=float(alt.degrees),
        azimuth_deg=_normalize_360(float(az.degrees)),
        range_km=float(distance.km),
        range_rate_mps=range_rate_mps,
    )


def doppler_correction(
    *,
    time_ms: int,
    station: TrackingStation,
    satellite_name: str,
    mode: DopplerMode,
    look: LookAngles,
    rx_hz: float,
    tx_hz: float,
) -> DopplerCorrection:
    rx_shift_hz = -look.range_rate_mps / SPEED_OF_LIGHT_MPS * rx_hz
    tx_shift_hz = look.range_rate_mps / SPEED_OF_LIGHT_MPS * tx_hz
    return DopplerCorrection(
        ts_ms=int(time_ms),
        station_id=station.id,
        satellite=satellite_name,
        mode=mode,
        range_rate_mps=look.range_rate_mps,
        rx_hz=rx_hz,
        rx_shift_hz=rx_shift_hz,
        rx_tune_hz=rx_hz + rx_shift_hz,
        tx_hz=tx_hz,
        tx_shift_hz=tx_shift_hz,
        tx_tune_hz=tx_hz + tx_shift_hz,
    )


def footprint_radius_deg(altitude_km: float, min_elevation_deg: float) -> float:
    orbital_radius_km = EARTH_RADIUS_KM + max(1.0, altitude_km)
    horizon = math.acos(EARTH_RADIUS_KM / orbital_radius_km)
    target = _deg_to_rad(max(0.0, min(89.0, min_elevation_deg)))
    if target <= 0:
        return _rad_to_deg(horizon)

    low = 0.0
    high = horizon
    for _ in range(32):
        mid = (low + high) / 2.0
        elevation = math.atan2(
            orbital_radius_km * math.cos(mid) - EARTH_RADIUS_KM,
            orbital_radius_km * math.sin(mid),
        )
        if elevation > target:
            low = mid
        else:
            high = mid
    return _rad_to_deg(low)


def sun_point(time_ms: int) -> SunPoint:
    jd = time_ms / 86_400_000 + 2440587.5
    n = jd - 2451545.0
    mean_longitude = _normalize_360(280.460 + 0.9856474 * n)
    mean_anomaly = _deg_to_rad(_normalize_360(357.528 + 0.9856003 * n))
    ecliptic_longitude = _deg_to_rad(_normalize_360(
        mean_longitude + 1.915 * math.sin(mean_anomaly) + 0.020 * math.sin(2 * mean_anomaly),
    ))
    obliquity = _deg_to_rad(23.439 - 0.0000004 * n)
    right_ascension = math.atan2(
        math.cos(obliquity) * math.sin(ecliptic_longitude),
        math.cos(ecliptic_longitude),
    )
    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic_longitude))
    gmst_deg = _normalize_360(280.46061837 + 360.98564736629 * (jd - 2451545.0))
    return SunPoint(
        lat_deg=_rad_to_deg(declination),
        lon_deg=_normalize_lon(_rad_to_deg(right_ascension) - gmst_deg),
    )


def split_dateline(points: Iterable[SatellitePoint]) -> tuple[tuple[SatellitePoint, ...], ...]:
    items = tuple(points)
    if not items:
        return tuple()
    segments: list[list[SatellitePoint]] = [[items[0]]]
    for previous, current in zip(items, items[1:]):
        if abs(current.lon_deg - previous.lon_deg) > 180.0:
            segments.append([current])
        else:
            segments[-1].append(current)
    return tuple(tuple(segment) for segment in segments if len(segment) > 1)


def orbital_period_minutes(satellite: EarthSatellite) -> float:
    return (2.0 * math.pi) / float(satellite.model.no_kozai)


def ground_track(satellite: EarthSatellite, center_ms: int, *, step_ms: int = 15_000) -> GroundTrack:
    period_ms = orbital_period_minutes(satellite) * 60_000.0
    start_ms = int(center_ms - period_ms / 2.0)
    end_ms = int(center_ms + period_ms / 2.0)
    trailing: list[SatellitePoint] = []
    upcoming: list[SatellitePoint] = []
    for ms in range(start_ms, end_ms + 1, step_ms):
        point = satellite_point_at(satellite, ms)
        if ms <= center_ms:
            trailing.append(point)
        else:
            upcoming.append(point)
    return GroundTrack(
        trailing=split_dateline(trailing),
        upcoming=split_dateline(upcoming),
    )


def _elevation_at(satellite: EarthSatellite, station: TrackingStation, time_ms: int) -> float:
    return look_angles_at(satellite, station, time_ms).elevation_deg


def _refine_crossing(
    satellite: EarthSatellite,
    station: TrackingStation,
    before_ms: int,
    after_ms: int,
    min_elevation_deg: float,
    entering: bool,
) -> int:
    low = before_ms
    high = after_ms
    for _ in range(14):
        mid = round((low + high) / 2)
        above = _elevation_at(satellite, station, mid) >= min_elevation_deg
        if entering:
            if above:
                high = mid
            else:
                low = mid
        elif above:
            low = mid
        else:
            high = mid
    return high if entering else low


def _contact_pass(
    satellite: EarthSatellite,
    station: TrackingStation,
    aos_ms: int,
    max_ms: int,
    los_ms: int,
) -> ContactPass:
    aos = look_angles_at(satellite, station, aos_ms)
    maximum = look_angles_at(satellite, station, max_ms)
    los = look_angles_at(satellite, station, los_ms)
    return ContactPass(
        id=str(aos_ms),
        aos_ms=aos_ms,
        max_ms=max_ms,
        los_ms=los_ms,
        duration_s=max(0.0, (los_ms - aos_ms) / 1000.0),
        start_elevation_deg=aos.elevation_deg,
        max_elevation_deg=maximum.elevation_deg,
        end_elevation_deg=los.elevation_deg,
        aos_azimuth_deg=aos.azimuth_deg,
        max_azimuth_deg=maximum.azimuth_deg,
        los_azimuth_deg=los.azimuth_deg,
        range_km_at_max=maximum.range_km,
        range_rate_mps_at_max=maximum.range_rate_mps,
    )


def upcoming_passes(
    satellite: EarthSatellite,
    station: TrackingStation,
    from_ms: int,
    *,
    count: int = 10,
    horizon_hours: float = 48.0,
    step_ms: int = 30_000,
) -> tuple[ContactPass, ...]:
    passes: list[ContactPass] = []
    stop_ms = int(from_ms + horizon_hours * 60 * 60 * 1000)
    previous_ms = from_ms
    previous_elevation = _elevation_at(satellite, station, previous_ms)
    previous_above = previous_elevation >= station.min_elevation_deg
    active: dict[str, float | int] | None = None
    if previous_above:
        active = {
            "aos_ms": from_ms,
            "max_ms": from_ms,
            "max_elevation_deg": previous_elevation,
        }

    for ms in range(from_ms + step_ms, stop_ms + 1, step_ms):
        current = look_angles_at(satellite, station, ms)
        current_above = current.elevation_deg >= station.min_elevation_deg

        if active is None and not previous_above and current_above:
            aos_ms = _refine_crossing(satellite, station, previous_ms, ms, station.min_elevation_deg, True)
            active = {
                "aos_ms": aos_ms,
                "max_ms": ms,
                "max_elevation_deg": current.elevation_deg,
            }

        if active is not None and current.elevation_deg > float(active["max_elevation_deg"]):
            active["max_ms"] = ms
            active["max_elevation_deg"] = current.elevation_deg

        if active is not None and previous_above and not current_above:
            los_ms = _refine_crossing(satellite, station, previous_ms, ms, station.min_elevation_deg, False)
            passes.append(_contact_pass(satellite, station, int(active["aos_ms"]), int(active["max_ms"]), los_ms))
            active = None
            if len(passes) >= count:
                break

        previous_ms = ms
        previous_above = current_above

    return tuple(passes)


def pass_detail(
    satellite: EarthSatellite,
    station: TrackingStation,
    contact: ContactPass,
    *,
    samples: int = 96,
) -> PassDetail:
    duration_ms = max(1, contact.los_ms - contact.aos_ms)
    step_ms = max(1, round(duration_ms / max(2, samples - 1)))
    rows: list[PassSample] = []
    for ms in range(contact.aos_ms, contact.los_ms + 1, step_ms):
        look = look_angles_at(satellite, station, ms)
        rows.append(PassSample(
            time_ms=ms,
            elevation_deg=look.elevation_deg,
            azimuth_deg=look.azimuth_deg,
            range_km=look.range_km,
            range_rate_mps=look.range_rate_mps,
        ))
    if rows[-1].time_ms != contact.los_ms:
        look = look_angles_at(satellite, station, contact.los_ms)
        rows.append(PassSample(
            time_ms=contact.los_ms,
            elevation_deg=look.elevation_deg,
            azimuth_deg=look.azimuth_deg,
            range_km=look.range_km,
            range_rate_mps=look.range_rate_mps,
        ))
    return PassDetail(summary=contact, samples=tuple(rows))


def tracking_state(
    config: TrackingConfig,
    *,
    time_ms: int,
    doppler_mode: DopplerMode = "disconnected",
    pass_count: int = 10,
) -> TrackingState:
    satellite = build_satellite(config)
    station = config.selected_station
    point = satellite_point_at(satellite, time_ms)
    look = look_angles_at(satellite, station, time_ms)
    visible = look.elevation_deg >= station.min_elevation_deg
    above_horizon = look.elevation_deg >= 0.0
    contact_state = "IN CONTACT" if visible else "LOW ELEVATION" if above_horizon else "OUT OF VIEW"
    doppler = doppler_correction(
        time_ms=time_ms,
        station=station,
        satellite_name=config.tle.name,
        mode=doppler_mode,
        look=look,
        rx_hz=config.frequencies.rx_hz,
        tx_hz=config.frequencies.tx_hz,
    )
    radius = footprint_radius_deg(point.altitude_km, station.min_elevation_deg)
    passes = upcoming_passes(satellite, station, time_ms, count=pass_count)
    return TrackingState(
        ts_ms=int(time_ms),
        config=config,
        tle_epoch_ms=tle_epoch_ms(satellite),
        satellite=point,
        look=look,
        contact_state=contact_state,
        above_horizon=above_horizon,
        visible_from_station=visible,
        doppler=doppler,
        footprint=Footprint(
            center_lat_deg=point.lat_deg,
            center_lon_deg=point.lon_deg,
            radius_deg=radius,
            visible_from_station=visible,
        ),
        sun=sun_point(time_ms),
        ground_track=ground_track(satellite, time_ms),
        upcoming_passes=passes,
    )
