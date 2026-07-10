"""SNIPE constellation mission assembly (KASI, 2023-072).

RX-only AX100 Mode 5 family mission: 4k8 FSK, CSP downlink, payload format
not public — frames log raw with CSP header facts. One mission covers the
whole formation; the seeded default is SNIPE-1 (the object cataloged as
2023-072G). To work another member, edit the RX frequency and the tracking
TLE identifier, then restart the radio:

    SNIPE-1  NORAD 56749  435.450 MHz   (default)
    SNIPE-2  NORAD 56745  436.000 MHz
    SNIPE-3  NORAD 56746  436.950 MHz
    SNIPE-4  NORAD 56744  437.800 MHz
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
    mission_id="snipe",
    mission_name="SNIPE",
    norad=56749,
    tle_name="SNIPE-1",
    tle_source="CelesTrak (seeded 2026-07-10)",
    tle_line1="1 56749U 23072G   26190.83469594  .00024299  00000+0  40629-3 0  9990",
    tle_line2="2 56749  97.4885  39.6063 0001923 228.0801 132.0287 15.51904147173974",
    freq_hz=435_450_000.0,
)


def build(ctx: MissionContext) -> MissionSpec:
    return build_ax100_mission(
        ctx, TARGET, MISSION_YML_PATH, Ax100RxPacketOps(TARGET.mission_id)
    )
