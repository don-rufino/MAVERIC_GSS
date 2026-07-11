"""AISTECHSAT-2 mission assembly (Aistech Space, NORAD 43768).

RX-only AX100 Mode 5 family mission on 436.600 MHz, 4k8 FSK (deviation
1600 Hz) and 9k6 FSK. Standard housekeeping frames decode per the
gr-satellites `lume` telemetry stack ported in telemetry.py (PUS TM with
five payload tables — OBC / EPS / TTC+GSSB / AOCS / TEMPS); Aistech's
undocumented "custom telemetry" frames log raw with CSP header facts.
"""

from __future__ import annotations

from pathlib import Path

from mav_gss_lib.platform import MissionContext, MissionSpec

from mav_gss_lib.missions.ax100_rx import (
    Ax100RxPacketOps,
    Ax100Target,
    build_ax100_mission,
)
from mav_gss_lib.missions.aistechsat2.telemetry import decode_beacon


MISSION_DIR = Path(__file__).resolve().parent
MISSION_YML_PATH = MISSION_DIR / "mission.yml"

TARGET = Ax100Target(
    mission_id="aistechsat2",
    mission_name="AISTECHSAT-2",
    norad=43768,
    tle_name="AISTECHSAT-2",
    tle_source="CelesTrak (seeded 2026-07-11)",
    tle_line1="1 43768U 18099L   26190.40518050  .00004147  00000+0  22132-3 0  9994",
    tle_line2="2 43768  97.4285 237.9540 0004036 200.8293 159.2775 15.15677823415643",
    freq_hz=436_600_000.0,
)


def build(ctx: MissionContext) -> MissionSpec:
    return build_ax100_mission(
        ctx, TARGET, MISSION_YML_PATH,
        Ax100RxPacketOps(TARGET.mission_id, hk_decoder=decode_beacon),
    )
