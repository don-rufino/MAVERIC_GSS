"""Astrocast 0.1 tracking defaults.

Gap-filled into `platform.tracking` by `mission.build()` the same way
MAVERIC's tracking_defaults works: setdefault-only, so operator values
in gss.yml always win. TLE seeded from CelesTrak on 2026-07-09; the
tle_fetch identifier is pre-filled so operators can refresh in-app.
"""

from __future__ import annotations

from typing import Any


ASTROCAST_NORAD = 43798
ASTROCAST_TLE_NAME = "ASTROCAST 0.1"
ASTROCAST_TLE_SOURCE = "CelesTrak (seeded 2026-07-09)"
ASTROCAST_TLE_LINE1 = "1 43798U 18099AS  26189.75535981  .00031250  00000+0  53623-3 0  9999"
ASTROCAST_TLE_LINE2 = "2 43798  97.4369 268.9463 0005634 145.5224 214.6394 15.51109059417771"
ASTROCAST_FREQ_HZ = 437_150_000.0

STATION_ID = "usc"
STATION_NAME = "USC / Southern California"
STATION_LAT_DEG = 34.0205
STATION_LON_DEG = -118.2856
STATION_ALT_M = 70.0
STATION_MIN_ELEVATION_DEG = 5.0


def seed_tracking_defaults(platform_cfg: dict[str, Any]) -> None:
    """Gap-fill Astrocast tracking defaults; never override operator values."""
    if not isinstance(platform_cfg, dict):
        return
    tracking = platform_cfg.setdefault("tracking", {})
    if not isinstance(tracking, dict):
        return

    tle = tracking.setdefault("tle", {})
    if isinstance(tle, dict):
        seeded = "line1" not in tle
        tle.setdefault("source", ASTROCAST_TLE_SOURCE)
        tle.setdefault("name", ASTROCAST_TLE_NAME)
        tle.setdefault("line1", ASTROCAST_TLE_LINE1)
        tle.setdefault("line2", ASTROCAST_TLE_LINE2)
        if seeded:
            tle.setdefault("method", "seed")

    fetch = tracking.setdefault("tle_fetch", {})
    if isinstance(fetch, dict):
        fetch.setdefault("identifier", str(ASTROCAST_NORAD))
        fetch.setdefault("auto_refresh", False)
        fetch.setdefault("refresh_interval_hours", 12)

    frequencies = tracking.setdefault("frequencies", {})
    if isinstance(frequencies, dict):
        frequencies.setdefault("rx_hz", ASTROCAST_FREQ_HZ)
        frequencies.setdefault("tx_hz", ASTROCAST_FREQ_HZ)

    stations = tracking.get("stations")
    if not isinstance(stations, list) or not stations:
        tracking["stations"] = [{
            "id": STATION_ID,
            "name": STATION_NAME,
            "lat_deg": STATION_LAT_DEG,
            "lon_deg": STATION_LON_DEG,
            "alt_m": STATION_ALT_M,
            "min_elevation_deg": STATION_MIN_ELEVATION_DEG,
        }]
        tracking.setdefault("selected_station_id", STATION_ID)
