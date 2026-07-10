"""Suomi 100 mission assembly (Aalto University / Univ. of Helsinki, NORAD 43804).

RX-only AX100 Mode 5 family mission on 437.775 MHz, 9k6 FSK (deviation
2400 Hz). The two GomSpace-style housekeeping beacons are fully decoded
into parameters — layouts ported from gr-satellites
`satellites/telemetry/suomi100.py` in `telemetry.py`.
"""

from __future__ import annotations

from pathlib import Path

from mav_gss_lib.platform import MissionContext, MissionSpec

from mav_gss_lib.missions.ax100_rx import (
    Ax100RxPacketOps,
    Ax100Target,
    build_ax100_mission,
)
from mav_gss_lib.missions.suomi100.telemetry import decode_beacon


MISSION_DIR = Path(__file__).resolve().parent
MISSION_YML_PATH = MISSION_DIR / "mission.yml"

TARGET = Ax100Target(
    mission_id="suomi100",
    mission_name="Suomi 100",
    norad=43804,
    tle_name="SUOMI-100",
    tle_source="CelesTrak (seeded 2026-07-10)",
    tle_line1="1 43804U 18099AY  26190.13490669  .00008296  00000+0  29743-3 0  9994",
    tle_line2="2 43804  97.4178 250.4522 0009924 193.1896 166.9086 15.28933488416770",
    freq_hz=437_775_000.0,
)


def build(ctx: MissionContext) -> MissionSpec:
    return build_ax100_mission(
        ctx,
        TARGET,
        MISSION_YML_PATH,
        Ax100RxPacketOps(TARGET.mission_id, hk_decoder=decode_beacon),
    )
