"""Shared library for RX-only GomSpace AX100 Mode 5 mission packages.

Not a mission itself (ships no mission.yml, so `discover_missions()` never
surfaces it). The snipe / catsat / suomi100 / luojia1 / roads /
aistechsat2 / innocube / nushsat1 packages compose these helpers into thin
per-satellite missions: frames arrive from MAV_DUO's "AX100 ASM+Golay"
decoder branches as raw CSP packets; the CSP v1 header always parses into
facts, and the payload decodes into parameters only when the mission
supplies a housekeeping decoder (payload formats for some of these birds
are not public — those log raw, the astrocast opaque pattern).
"""

from mav_gss_lib.missions.ax100_rx.packets import (
    Ax100Payload,
    Ax100RxPacketOps,
    HkDecode,
    TokenPacket,
)
from mav_gss_lib.missions.ax100_rx.mission_support import (
    Ax100Bird,
    Ax100Target,
    build_ax100_mission,
    seed_ax100_defaults,
)

__all__ = [
    "Ax100Bird",
    "Ax100Payload",
    "Ax100RxPacketOps",
    "Ax100Target",
    "HkDecode",
    "TokenPacket",
    "build_ax100_mission",
    "seed_ax100_defaults",
]
