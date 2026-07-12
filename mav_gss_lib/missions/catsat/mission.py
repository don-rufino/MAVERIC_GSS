"""CATSAT mission assembly (University of Arizona, NORAD 60246).

RX-only AX100 Mode 5 family mission on 437.185 MHz. CATSAT beacons at 2k4
(deviation 750 Hz) and bursts data at 9k6 / 38k4 — all AX100 ASM+Golay CSP,
with the production decoder intentionally scoped to the known 2k4 routine
beacon. All 18 beacon types decode into parameters via the ported
community catsat.ksy layout in `telemetry.py`; CATSAT's CSP header is
little-endian on the wire, so the shared ops parse it with
``csp_endianness="little"``.
"""

from __future__ import annotations

from pathlib import Path

from mav_gss_lib.platform import MissionContext, MissionSpec

from mav_gss_lib.missions.ax100_rx import (
    Ax100RxPacketOps,
    Ax100Target,
    build_ax100_mission,
)
from mav_gss_lib.missions.catsat.telemetry import decode_beacon


MISSION_DIR = Path(__file__).resolve().parent
MISSION_YML_PATH = MISSION_DIR / "mission.yml"

TARGET = Ax100Target(
    mission_id="catsat",
    mission_name="CATSAT",
    norad=60246,
    tle_name="CATSAT",
    tle_source="CelesTrak (seeded 2026-07-10)",
    tle_line1="1 60246U 24125J   26190.65375772  .00053285  00000+0  61137-3 0  9995",
    tle_line2="2 60246  97.2075  43.4908 0021055 102.4817 257.8799 15.61638798110894",
    freq_hz=437_185_000.0,
)


def build(ctx: MissionContext) -> MissionSpec:
    return build_ax100_mission(
        ctx,
        TARGET,
        MISSION_YML_PATH,
        Ax100RxPacketOps(
            TARGET.mission_id, hk_decoder=decode_beacon, csp_endianness="little"
        ),
    )
