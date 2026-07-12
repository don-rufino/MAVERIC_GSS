"""Seed and build helpers shared by the AX100 Mode 5 mission family.

Each mission package declares one `Ax100Target` (identity, downlink
frequency, seeded TLE) and calls `build_ax100_mission` from its
`build(ctx)`. Seeding is setdefault-only, matching the astrocast /
sharjahsat pattern: operator values in the per-mission gss.<id>.yml
always win.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mav_gss_lib.platform import MissionConfigSpec, MissionContext, MissionSpec
from mav_gss_lib.platform.spec import parse_yaml

from mav_gss_lib.missions.ax100_rx.packets import Ax100RxPacketOps


STATION_ID = "usc"
STATION_NAME = "USC / Southern California"
STATION_LAT_DEG = 34.0205
STATION_LON_DEG = -118.2856
STATION_ALT_M = 70.0
STATION_MIN_ELEVATION_DEG = 5.0

_RADIO_SCRIPT = "gnuradio/MAV_DUO.py"


@dataclass(frozen=True, slots=True)
class Ax100Bird:
    """One selectable spacecraft of a formation mission (SNIPE, ROADS)."""

    bird_id: str
    label: str
    norad: int
    freq_hz: float


@dataclass(frozen=True, slots=True)
class Ax100Target:
    mission_id: str
    mission_name: str
    norad: int
    tle_name: str
    tle_source: str
    tle_line1: str
    tle_line2: str
    freq_hz: float
    # Formation members selectable from the Mission pane's Target-satellite
    # dropdown; empty for single-spacecraft missions. The TARGET fields above
    # stay the seeded default.
    birds: tuple[Ax100Bird, ...] = ()

    @property
    def frequency_label(self) -> str:
        return f"{self.freq_hz / 1e6:.3f} MHz"


def seed_ax100_defaults(
    mission_cfg: dict[str, Any], platform_cfg: dict[str, Any], target: Ax100Target
) -> None:
    """Gap-fill mission name, RX frequency, radio script, and tracking."""
    if isinstance(mission_cfg, dict):
        mission_cfg.setdefault("mission_name", target.mission_name)
        if target.birds:
            # UI-facing formation table (Mission pane Target-satellite select).
            # Not in editable_paths, so it never persists — reseeded from code
            # every boot; selection itself lives in the platform fields the
            # select fills (tle_fetch.identifier + rx.frequency).
            mission_cfg.setdefault("target_birds", [
                {
                    "id": bird.bird_id,
                    "label": bird.label,
                    "norad": bird.norad,
                    "rx_frequency": f"{bird.freq_hz / 1e6:.3f} MHz",
                }
                for bird in target.birds
            ])
    if not isinstance(platform_cfg, dict):
        return

    rx = platform_cfg.setdefault("rx", {})
    if isinstance(rx, dict):
        rx.setdefault("frequency", target.frequency_label)
    radio = platform_cfg.setdefault("radio", {})
    if isinstance(radio, dict):
        radio.setdefault("script", _RADIO_SCRIPT)

    tracking = platform_cfg.setdefault("tracking", {})
    if not isinstance(tracking, dict):
        return

    tle = tracking.setdefault("tle", {})
    if isinstance(tle, dict):
        seeded = "line1" not in tle
        tle.setdefault("source", target.tle_source)
        tle.setdefault("name", target.tle_name)
        tle.setdefault("line1", target.tle_line1)
        tle.setdefault("line2", target.tle_line2)
        if seeded:
            tle.setdefault("method", "seed")

    fetch = tracking.setdefault("tle_fetch", {})
    if isinstance(fetch, dict):
        fetch.setdefault("identifier", str(target.norad))
        fetch.setdefault("auto_refresh", False)
        fetch.setdefault("refresh_interval_hours", 12)

    frequencies = tracking.setdefault("frequencies", {})
    if isinstance(frequencies, dict):
        frequencies.setdefault("rx_hz", target.freq_hz)
        frequencies.setdefault("tx_hz", target.freq_hz)

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


def build_ax100_mission(
    ctx: MissionContext,
    target: Ax100Target,
    yml_path: Path,
    packet_ops: Ax100RxPacketOps,
) -> MissionSpec:
    """Assemble the RX-only MissionSpec for one AX100 Mode 5 target."""
    seed_ax100_defaults(ctx.mission_config, ctx.platform_config, target)
    mission = parse_yaml(yml_path, plugins={})
    return MissionSpec(
        id=target.mission_id,
        name=ctx.mission_config.get("mission_name") or target.mission_name,
        packets=packet_ops,
        spec_root=mission,
        config=MissionConfigSpec(),
    )
