"""MAVERIC tracking defaults.

Mission-owned constants for the MAVERIC spacecraft. The platform's
`mav_gss_lib/platform/tracking/` module ships neutral fallback defaults
(`Sample LEO` TLE, `Reference Station` placeholder); MAVERIC's
`mission.build()` hook seeds these values into `platform.tracking`
when the operator's gss.yml hasn't already set them.

Operator overrides via `gss.yml::platform.tracking` always win — these
constants are first-run convenience for fresh checkouts and do not
override an existing config.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

from typing import Any


MAVERIC_TLE_LINE1 = "1 99999U 26001A   26182.53800926  .00000000  00000-0  15000-3 0  9999"
MAVERIC_TLE_LINE2 = "2 99999  97.8250 154.7171 0058009 348.1000 351.9980 14.91466332000019"
MAVERIC_TLE_NAME = "MAVERIC"
MAVERIC_TLE_SOURCE = "MAVERIC local TLE"
MAVERIC_FREQ_HZ = 437_575_000.0

# USC / Southern California ground-station reference. MAVERIC's
# operations site; operators at other stations override via gss.yml.
MAVERIC_STATION_ID = "usc"
MAVERIC_STATION_NAME = "USC / Southern California"
MAVERIC_STATION_LAT_DEG = 34.0205
MAVERIC_STATION_LON_DEG = -118.2856
MAVERIC_STATION_ALT_M = 70.0
MAVERIC_STATION_MIN_ELEVATION_DEG = 5.0


def seed_tracking_defaults(platform_cfg: dict[str, Any]) -> None:
    """Gap-fill MAVERIC tracking defaults into `platform_cfg["tracking"]`.

    Sets fields ONLY when absent. Operator's `gss.yml` values take
    precedence; this helper exists so a fresh checkout (no operator
    config yet) starts with MAVERIC's actual TLE/station/frequency
    instead of the platform's neutral `Sample LEO` placeholder.
    """
    if not isinstance(platform_cfg, dict):
        return
    tracking = platform_cfg.setdefault("tracking", {})
    if not isinstance(tracking, dict):
        return

    tle = tracking.setdefault("tle", {})
    if isinstance(tle, dict):
        tle.setdefault("source", MAVERIC_TLE_SOURCE)
        tle.setdefault("name", MAVERIC_TLE_NAME)
        tle.setdefault("line1", MAVERIC_TLE_LINE1)
        tle.setdefault("line2", MAVERIC_TLE_LINE2)

    frequencies = tracking.setdefault("frequencies", {})
    if isinstance(frequencies, dict):
        frequencies.setdefault("rx_hz", MAVERIC_FREQ_HZ)
        frequencies.setdefault("tx_hz", MAVERIC_FREQ_HZ)

    stations = tracking.get("stations")
    if not isinstance(stations, list) or not stations:
        tracking["stations"] = [{
            "id": MAVERIC_STATION_ID,
            "name": MAVERIC_STATION_NAME,
            "lat_deg": MAVERIC_STATION_LAT_DEG,
            "lon_deg": MAVERIC_STATION_LON_DEG,
            "alt_m": MAVERIC_STATION_ALT_M,
            "min_elevation_deg": MAVERIC_STATION_MIN_ELEVATION_DEG,
        }]
        tracking.setdefault("selected_station_id", MAVERIC_STATION_ID)


__all__ = [
    "MAVERIC_TLE_LINE1",
    "MAVERIC_TLE_LINE2",
    "MAVERIC_TLE_NAME",
    "MAVERIC_TLE_SOURCE",
    "MAVERIC_FREQ_HZ",
    "MAVERIC_STATION_ID",
    "MAVERIC_STATION_NAME",
    "MAVERIC_STATION_LAT_DEG",
    "MAVERIC_STATION_LON_DEG",
    "MAVERIC_STATION_ALT_M",
    "MAVERIC_STATION_MIN_ELEVATION_DEG",
    "seed_tracking_defaults",
]
