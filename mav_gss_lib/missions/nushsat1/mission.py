"""NUSHSat-1 mission assembly (NUS High School Singapore, NORAD 63211).

RX-only AX100 Mode 5 family mission on 436.200 MHz at 2k4 and 4k8 FSK
(the bird also beacons at 1k2, below the decoder's branch set).
gr-satellites classifies its telemetry as bare CSP — no public payload
format — so frames log raw with CSP header facts.
"""

from __future__ import annotations

from pathlib import Path

from mav_gss_lib.platform import MissionContext, MissionSpec

from mav_gss_lib.missions.ax100_rx import (
    Ax100RxPacketOps,
    Ax100Target,
    build_ax100_mission,
)


MISSION_DIR = Path(__file__).resolve().parent
MISSION_YML_PATH = MISSION_DIR / "mission.yml"

TARGET = Ax100Target(
    mission_id="nushsat1",
    mission_name="NUSHSat-1",
    norad=63211,
    tle_name="NUSHSAT1",
    tle_source="CelesTrak (seeded 2026-07-11)",
    tle_line1="1 63211U 25052B   26192.15660378  .00013323  00000+0  37086-3 0  9991",
    tle_line2="2 63211  97.3958  89.6043 0003622  48.0760 312.0792 15.36934779 73822",
    freq_hz=436_200_000.0,
)


def build(ctx: MissionContext) -> MissionSpec:
    return build_ax100_mission(
        ctx, TARGET, MISSION_YML_PATH, Ax100RxPacketOps(TARGET.mission_id)
    )
