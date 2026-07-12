"""SNIPE constellation mission assembly (KASI, 2023-072).

RX-only AX100 Mode 5 family mission: 4k8 FSK, CSP downlink, payload format
not public — frames log raw with CSP header facts. One mission covers the
whole formation; the seeded default is SNIPE-1 (the object cataloged as
2023-072G). Members are selectable from the Mission pane's Target-satellite
dropdown (fills the TLE identifier + RX frequency; each bird has its own
frequency, so a swap needs a radio restart after Save; see TARGET.birds).
"""

from __future__ import annotations

from pathlib import Path

from mav_gss_lib.platform import MissionContext, MissionSpec

from mav_gss_lib.missions.ax100_rx import (
    Ax100Bird,
    Ax100RxPacketOps,
    Ax100Target,
    build_ax100_mission,
)


MISSION_DIR = Path(__file__).resolve().parent
MISSION_YML_PATH = MISSION_DIR / "mission.yml"

TARGET = Ax100Target(
    mission_id="snipe",
    mission_name="SNIPE",
    norad=56749,
    tle_name="SNIPE-1",
    tle_source="CelesTrak (seeded 2026-07-10)",
    tle_line1="1 56749U 23072G   26190.83469594  .00024299  00000+0  40629-3 0  9990",
    tle_line2="2 56749  97.4885  39.6063 0001923 228.0801 132.0287 15.51904147173974",
    freq_hz=435_450_000.0,
    birds=(
        Ax100Bird("snipe1", "SNIPE-1", 56749, 435_450_000.0),
        Ax100Bird("snipe2", "SNIPE-2", 56745, 436_000_000.0),
        Ax100Bird("snipe3", "SNIPE-3", 56746, 436_950_000.0),
        Ax100Bird("snipe4", "SNIPE-4", 56744, 437_800_000.0),
    ),
)


def build(ctx: MissionContext) -> MissionSpec:
    return build_ax100_mission(
        ctx, TARGET, MISSION_YML_PATH, Ax100RxPacketOps(TARGET.mission_id)
    )
