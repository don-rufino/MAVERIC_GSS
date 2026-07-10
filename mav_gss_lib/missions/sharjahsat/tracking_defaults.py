"""Sharjahsat-1 tracking defaults.

Gap-filled into `platform.tracking` by `mission.build()` the same way
the MAVERIC and Astrocast tracking_defaults work: setdefault-only, so
operator values in gss.sharjahsat.yml always win. TLE seeded from
CelesTrak on 2026-07-10; the tle_fetch identifier is pre-filled so
operators can refresh in-app.
"""

from __future__ import annotations

from typing import Any


SHARJAHSAT_NORAD = 55104
SHARJAHSAT_TLE_NAME = "SHARJAHSAT-1"
SHARJAHSAT_TLE_SOURCE = "CelesTrak (seeded 2026-07-10)"
SHARJAHSAT_TLE_LINE1 = "1 55104U 23001CZ  26190.84460823  .00015391  00000+0  27627-3 0  9999"
SHARJAHSAT_TLE_LINE2 = "2 55104  97.3160 259.9369 0005330  38.0653 322.0974 15.49975316196005"
SHARJAHSAT_FREQ_HZ = 437_325_000.0

STATION_ID = "usc"
STATION_NAME = "USC / Southern California"
STATION_LAT_DEG = 34.0205
STATION_LON_DEG = -118.2856
STATION_ALT_M = 70.0
STATION_MIN_ELEVATION_DEG = 5.0


def seed_tracking_defaults(platform_cfg: dict[str, Any]) -> None:
    """Gap-fill Sharjahsat-1 tracking defaults; never override operator values."""
    if not isinstance(platform_cfg, dict):
        return
    tracking = platform_cfg.setdefault("tracking", {})
    if not isinstance(tracking, dict):
        return

    tle = tracking.setdefault("tle", {})
    if isinstance(tle, dict):
        seeded = "line1" not in tle
        tle.setdefault("source", SHARJAHSAT_TLE_SOURCE)
        tle.setdefault("name", SHARJAHSAT_TLE_NAME)
        tle.setdefault("line1", SHARJAHSAT_TLE_LINE1)
        tle.setdefault("line2", SHARJAHSAT_TLE_LINE2)
        if seeded:
            tle.setdefault("method", "seed")

    fetch = tracking.setdefault("tle_fetch", {})
    if isinstance(fetch, dict):
        fetch.setdefault("identifier", str(SHARJAHSAT_NORAD))
        fetch.setdefault("auto_refresh", False)
        fetch.setdefault("refresh_interval_hours", 12)

    frequencies = tracking.setdefault("frequencies", {})
    if isinstance(frequencies, dict):
        frequencies.setdefault("rx_hz", SHARJAHSAT_FREQ_HZ)
        frequencies.setdefault("tx_hz", SHARJAHSAT_FREQ_HZ)

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
