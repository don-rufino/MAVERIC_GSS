"""Astrocast 0.1 mission assembly.

RX-only MissionSpec: PacketOps + declarative mission.yml (parameters,
ascii_tokens beacon container, rx_columns). No CommandOps — the platform
rejects TX admission with a clean error (balloon_v2 pattern). spec_root
is required for server boot: the alarm environment dereferences it.
"""

from __future__ import annotations

from pathlib import Path

from mav_gss_lib.platform import MissionConfigSpec, MissionContext, MissionSpec
from mav_gss_lib.platform.spec import parse_yaml

from mav_gss_lib.missions.astrocast.packets import AstrocastPacketOps
from mav_gss_lib.missions.astrocast.tracking_defaults import seed_tracking_defaults


MISSION_DIR = Path(__file__).resolve().parent
MISSION_YML_PATH = MISSION_DIR / "mission.yml"

_RX_DEFAULTS = {"frequency": "437.150 MHz"}


def _seed(platform_cfg: dict) -> None:
    rx = platform_cfg.setdefault("rx", {})
    if isinstance(rx, dict):
        for key, value in _RX_DEFAULTS.items():
            rx.setdefault(key, value)
    seed_tracking_defaults(platform_cfg)


def build(ctx: MissionContext) -> MissionSpec:
    _seed(ctx.platform_config)
    mission = parse_yaml(MISSION_YML_PATH, plugins={})
    return MissionSpec(
        id="astrocast",
        name=ctx.mission_config.get("mission_name") or "Astrocast 0.1",
        packets=AstrocastPacketOps(),
        spec_root=mission,
        config=MissionConfigSpec(),
    )
