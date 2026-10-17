"""SCION-X tracking defaults.

Gap-filled into `platform.tracking` by `mission.build()` like the other
missions' tracking_defaults: setdefault-only, so operator values in
gss.scionx.yml always win. TLE seeded from CelesTrak on 2026-10-07; the
tle_fetch identifier is pre-filled so operators can refresh in-app.
"""

from __future__ import annotations

from typing import Any


SCIONX_NORAD = 69880
SCIONX_TLE_NAME = "SCION-X (INSPIRESAT 6)"
SCIONX_TLE_SOURCE = "CelesTrak (seeded 2026-10-07)"
SCIONX_TLE_LINE1 = "1 69880U 26156M   26280.94624274  .00001380  00000-0  14421-3 0  9994"
SCIONX_TLE_LINE2 = "2 69880  97.7354 179.6484 0004959 155.4740 204.6715 14.91262875 13802"
SCIONX_FREQ_HZ = 437_500_000.0

STATION_ID = "usc"
STATION_NAME = "USC / Southern California"
STATION_LAT_DEG = 34.0205
STATION_LON_DEG = -118.2856
STATION_ALT_M = 70.0
STATION_MIN_ELEVATION_DEG = 5.0
STATION_MAX_ELEVATION_DEG = 55.0   # rotator elevation stop; azimuth keeps tracking above it


def seed_tracking_defaults(platform_cfg: dict[str, Any]) -> None:
    """Gap-fill SCION-X tracking defaults; never override operator values."""
    if not isinstance(platform_cfg, dict):
        return
    tracking = platform_cfg.setdefault("tracking", {})
    if not isinstance(tracking, dict):
        return

    tle = tracking.setdefault("tle", {})
    if isinstance(tle, dict):
        seeded = "line1" not in tle
        tle.setdefault("source", SCIONX_TLE_SOURCE)
        tle.setdefault("name", SCIONX_TLE_NAME)
        tle.setdefault("line1", SCIONX_TLE_LINE1)
        tle.setdefault("line2", SCIONX_TLE_LINE2)
        if seeded:
            tle.setdefault("method", "seed")

    fetch = tracking.setdefault("tle_fetch", {})
    if isinstance(fetch, dict):
        fetch.setdefault("identifier", str(SCIONX_NORAD))
        fetch.setdefault("auto_refresh", False)
        fetch.setdefault("refresh_interval_hours", 12)

    frequencies = tracking.setdefault("frequencies", {})
    if isinstance(frequencies, dict):
        frequencies.setdefault("rx_hz", SCIONX_FREQ_HZ)
        frequencies.setdefault("tx_hz", SCIONX_FREQ_HZ)

    stations = tracking.get("stations")
    if not isinstance(stations, list) or not stations:
        tracking["stations"] = [{
            "id": STATION_ID,
            "name": STATION_NAME,
            "lat_deg": STATION_LAT_DEG,
            "lon_deg": STATION_LON_DEG,
            "alt_m": STATION_ALT_M,
            "min_elevation_deg": STATION_MIN_ELEVATION_DEG,
            "max_elevation_deg": STATION_MAX_ELEVATION_DEG,
        }]
        tracking.setdefault("selected_station_id", STATION_ID)
