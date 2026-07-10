"""LUOJIA-1 mission assembly (Wuhan University, NORAD 43485).

RX-only AX100 Mode 5 family mission on 437.250 MHz, 4k8 FSK (deviation
1600 Hz). gr-satellites classifies its telemetry as bare CSP — no public
payload format — so frames log raw with CSP header facts.
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
    mission_id="luojia1",
    mission_name="Luojia-1",
    norad=43485,
    tle_name="LUOJIA-1",
    tle_source="CelesTrak (seeded 2026-07-10)",
    tle_line1="1 43485U 18048B   26190.84627103  .00001719  00000+0  19611-3 0  9992",
    tle_line2="2 43485  97.8095 268.6707 0010147  53.5991 306.6159 14.87371655437589",
    freq_hz=437_250_000.0,
)


def build(ctx: MissionContext) -> MissionSpec:
    return build_ax100_mission(
        ctx, TARGET, MISSION_YML_PATH, Ax100RxPacketOps(TARGET.mission_id)
    )
