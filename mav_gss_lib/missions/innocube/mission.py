"""InnoCube mission assembly (TU Berlin / Uni Würzburg, NORAD 62616).

RX-only AX100 Mode 5 family mission on 435.950 MHz, 9k6 FSK. gr-satellites
classifies its telemetry as bare CSP — no public payload format — so
frames log raw with CSP header facts.
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
    mission_id="innocube",
    mission_name="InnoCube",
    norad=62616,
    tle_name="INNOCUBE",
    tle_source="CelesTrak (seeded 2026-07-11)",
    tle_line1="1 62616U 25009H   26192.46931697  .00004382  00000+0  16895-3 0  9990",
    tle_line2="2 62616  97.3903 272.2107 0004613 235.5599 124.5205 15.26833292 82524",
    freq_hz=435_950_000.0,
)


def build(ctx: MissionContext) -> MissionSpec:
    return build_ax100_mission(
        ctx, TARGET, MISSION_YML_PATH, Ax100RxPacketOps(TARGET.mission_id)
    )
