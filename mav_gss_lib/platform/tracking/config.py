"""Normalize platform.tracking config into typed tracking models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import (
    SAMPLE_TLE_LINE1,
    SAMPLE_TLE_LINE2,
    TrackingConfig,
    TrackingDisplay,
    TrackingFrequencies,
    TrackingStation,
    TrackingTle,
)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if value is not None and not isinstance(value, (dict, list, tuple)):
        text = str(value).strip()
        if text:
            return text
    return fallback


def _float(value: Any, fallback: float, *, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = fallback
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def _bool(value: Any, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback


def _tle_method(value: Any) -> str:
    return value if value in ("manual", "fetched", "seed") else "manual"


def default_station() -> TrackingStation:
    # Reference station with sample coordinates. Operators override via
    # gss.yml `platform.tracking.stations[]`; this default is a
    # placeholder so a fresh checkout has a non-empty stations catalog.
    return TrackingStation(
        id="ref",
        name="Reference Station",
        lat_deg=0.0,
        lon_deg=0.0,
        alt_m=0.0,
        min_elevation_deg=5.0,
    )


def default_tracking_config() -> TrackingConfig:
    station = default_station()
    return TrackingConfig(
        enabled=True,
        selected_station_id=station.id,
        stations=(station,),
        tle=TrackingTle(
            source="platform sample TLE",
            name="Sample LEO",
            line1=SAMPLE_TLE_LINE1,
            line2=SAMPLE_TLE_LINE2,
            method="seed",
        ),
        frequencies=TrackingFrequencies(rx_hz=437_000_000.0, tx_hz=437_000_000.0),
        display=TrackingDisplay(day_night_map=True),
    )


def default_tracking_config_dict() -> dict[str, Any]:
    cfg = default_tracking_config()
    return {
        "enabled": cfg.enabled,
        "selected_station_id": cfg.selected_station_id,
        "stations": [
            {
                "id": station.id,
                "name": station.name,
                "lat_deg": station.lat_deg,
                "lon_deg": station.lon_deg,
                "alt_m": station.alt_m,
                "min_elevation_deg": station.min_elevation_deg,
            }
            for station in cfg.stations
        ],
        "tle": {
            "source": cfg.tle.source,
            "name": cfg.tle.name,
            "line1": cfg.tle.line1,
            "line2": cfg.tle.line2,
            "method": cfg.tle.method,
            "fetched_at_ms": cfg.tle.fetched_at_ms,
        },
        "frequencies": {
            "rx_hz": cfg.frequencies.rx_hz,
            "tx_hz": cfg.frequencies.tx_hz,
        },
        "display": {
            "day_night_map": cfg.display.day_night_map,
        },
    }


def _station_from_raw(value: Any) -> TrackingStation | None:
    raw = _as_mapping(value)
    station_id = _string(raw.get("id"), "")
    name = _string(raw.get("name"), "")
    if not station_id or not name:
        return None
    return TrackingStation(
        id=station_id,
        name=name,
        lat_deg=_float(raw.get("lat_deg"), 34.0205, min_value=-90.0, max_value=90.0),
        lon_deg=_float(raw.get("lon_deg"), -118.2856, min_value=-180.0, max_value=180.0),
        alt_m=_float(raw.get("alt_m"), 70.0, min_value=-500.0, max_value=9000.0),
        min_elevation_deg=_float(raw.get("min_elevation_deg"), 5.0, min_value=0.0, max_value=45.0),
    )


def normalize_tracking_config(value: Any) -> TrackingConfig:
    defaults = default_tracking_config()
    raw = _as_mapping(value)
    stations_raw = raw.get("stations")
    stations: list[TrackingStation] = []
    if isinstance(stations_raw, list):
        stations = [
            station for station in (_station_from_raw(item) for item in stations_raw)
            if station is not None
        ]
    if not stations:
        stations = list(defaults.stations)

    selected_station_id = _string(raw.get("selected_station_id"), stations[0].id)
    if selected_station_id not in {station.id for station in stations}:
        selected_station_id = stations[0].id

    tle_raw = _as_mapping(raw.get("tle"))
    frequencies_raw = _as_mapping(raw.get("frequencies"))
    display_raw = _as_mapping(raw.get("display"))

    return TrackingConfig(
        enabled=_bool(raw.get("enabled"), defaults.enabled),
        selected_station_id=selected_station_id,
        stations=tuple(stations),
        tle=TrackingTle(
            source=_string(tle_raw.get("source"), defaults.tle.source),
            name=_string(tle_raw.get("name"), defaults.tle.name),
            line1=_string(tle_raw.get("line1"), defaults.tle.line1),
            line2=_string(tle_raw.get("line2"), defaults.tle.line2),
            method=_tle_method(tle_raw.get("method")),
            fetched_at_ms=int(_float(tle_raw.get("fetched_at_ms"), 0.0, min_value=0.0)),
        ),
        frequencies=TrackingFrequencies(
            rx_hz=_float(frequencies_raw.get("rx_hz"), defaults.frequencies.rx_hz, min_value=1.0),
            tx_hz=_float(frequencies_raw.get("tx_hz"), defaults.frequencies.tx_hz, min_value=1.0),
        ),
        display=TrackingDisplay(
            day_night_map=_bool(display_raw.get("day_night_map"), defaults.display.day_night_map),
        ),
    )
