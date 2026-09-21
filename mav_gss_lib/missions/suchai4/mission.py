"""SUCHAI-4 mission assembly (Universidad de Chile, NORAD 69911 — believed;
officially unclaimed as of 2026-08-25).

RX-only AX100 Mode 5 family mission on 437.250 MHz, 4k8 FSK (deviation
1600 Hz, matching gr-satellites' SUCHAI-3.yml transmitter entry — the
closest known predecessor in the same bus family). Real captures decode
as a consistent big-endian CSP v1 header (prio=2, src=1, dest=30, dport=20
across every frame captured 2026-08-28, 2026-09-04, 2026-09-11). Type-105
status beacons from the OBC decode into 39 housekeeping parameters via
telemetry.py — see its module docstring for the field source (SPEL's own
suchai4.ksy) and the caveats on two fields. Any other frame type still
logs raw with CSP header facts only, same as luojia1/roads/suomi100.

dest=30 is legitimate, not noise: SPEL's own suchai4.ksy
(gitlab.com/spel-uchile/satnogs-decoders, branch adding-suchai-4) maps it
via its `nodes` enum to `suchai_4_ground_station` — the common libcsp
convention of reserving 29-31 for the ground segment. The shared AX100
parser's dest<=20 plausibility heuristic doesn't know about that
convention and isn't being widened mission-wide (other AX100 missions
haven't independently confirmed their own dest range), so this mission
overrides it locally via max_plausible_dest.
"""

from __future__ import annotations

from pathlib import Path

from mav_gss_lib.platform import MissionContext, MissionSpec

from mav_gss_lib.missions.ax100_rx import (
    Ax100RxPacketOps,
    Ax100Target,
    build_ax100_mission,
)
from mav_gss_lib.missions.suchai4.telemetry import decode_beacon


MISSION_DIR = Path(__file__).resolve().parent
MISSION_YML_PATH = MISSION_DIR / "mission.yml"

TARGET = Ax100Target(
    mission_id="suchai4",
    mission_name="SUCHAI-4 (unclaimed)",
    norad=69911,
    tle_name="TRANSPORTER-17 OBJECT AU",
    tle_source="CelesTrak (seeded 2026-08-28)",
    tle_line1="1 69911U 26156AU  26239.69079370  .00001412  00000-0  14921-3 0  9997",
    tle_line2="2 69911  97.7481 139.2128 0001863  11.2754 348.8506 14.90782364  7654",
    freq_hz=437_250_000.0,
)


def build(ctx: MissionContext) -> MissionSpec:
    return build_ax100_mission(
        ctx, TARGET, MISSION_YML_PATH,
        Ax100RxPacketOps(
            TARGET.mission_id, hk_decoder=decode_beacon, max_plausible_dest=31
        ),
    )
