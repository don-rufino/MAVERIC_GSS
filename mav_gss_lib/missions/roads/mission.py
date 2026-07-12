"""ROADS pair mission assembly (University of North Dakota, 2025-135).

RX-only AX100 Mode 5 family mission: 4k8/9k6 FSK, CSP downlink. Type-0x66
full-table housekeeping beacons (the on-air format, reverse-engineered
from the first received frame; telemetry.py) decode into 47 parameters
across obc/gnss/eps/uhf/vhf domains; other frames log raw with CSP header
facts. One mission covers both spacecraft; the seeded
default is ROADS 1. Both share one downlink frequency, so working ROADS 2
is a tracking-only change (TLE identifier + re-engage Doppler — the radio
needs no retune):

    ROADS 1  NORAD 64535  435.400 MHz   (default)
    ROADS 2  NORAD 64549  435.400 MHz
"""

from __future__ import annotations

from pathlib import Path

from mav_gss_lib.platform import MissionContext, MissionSpec

from mav_gss_lib.missions.ax100_rx import (
    Ax100RxPacketOps,
    Ax100Target,
    build_ax100_mission,
)
from mav_gss_lib.missions.roads.telemetry import decode_beacon


MISSION_DIR = Path(__file__).resolve().parent
MISSION_YML_PATH = MISSION_DIR / "mission.yml"

TARGET = Ax100Target(
    mission_id="roads",
    mission_name="ROADS",
    norad=64535,
    tle_name="ROADS 1",
    tle_source="CelesTrak (seeded 2026-07-11)",
    tle_line1="1 64535U 25135H   26192.25625457  .00003849  00000+0  16789-3 0  9992",
    tle_line2="2 64535  97.4692 307.1505 0005939  96.6332 263.5581 15.22749399 58529",
    freq_hz=435_400_000.0,
)


def build(ctx: MissionContext) -> MissionSpec:
    return build_ax100_mission(
        ctx, TARGET, MISSION_YML_PATH,
        Ax100RxPacketOps(TARGET.mission_id, hk_decoder=decode_beacon),
    )
