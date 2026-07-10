"""Sharjahsat-1 mission assembly.

RX-only MissionSpec: PacketOps + declarative mission.yml (parameters,
ascii_tokens beacon container, rx_columns). No CommandOps — the platform
rejects TX admission with a clean error (astrocast pattern). spec_root
is required for server boot: the alarm environment dereferences it.
"""

from __future__ import annotations

from pathlib import Path

from mav_gss_lib.platform import MissionConfigSpec, MissionContext, MissionSpec
from mav_gss_lib.platform.spec import parse_yaml

from mav_gss_lib.missions.sharjahsat.packets import SharjahsatPacketOps
from mav_gss_lib.missions.sharjahsat.tracking_defaults import seed_tracking_defaults


MISSION_DIR = Path(__file__).resolve().parent
MISSION_YML_PATH = MISSION_DIR / "mission.yml"

_RX_DEFAULTS = {"frequency": "437.325 MHz"}
_RADIO_DEFAULTS = {"script": "gnuradio/MAV_DUO.py"}
_MISSION_NAME = "SharjahSat-1"


def _seed(mission_cfg: dict, platform_cfg: dict) -> None:
    if isinstance(mission_cfg, dict):
        mission_cfg.setdefault("mission_name", _MISSION_NAME)
    rx = platform_cfg.setdefault("rx", {})
    if isinstance(rx, dict):
        for key, value in _RX_DEFAULTS.items():
            rx.setdefault(key, value)
    radio = platform_cfg.setdefault("radio", {})
    if isinstance(radio, dict):
        for key, value in _RADIO_DEFAULTS.items():
            radio.setdefault(key, value)
    seed_tracking_defaults(platform_cfg)


def build(ctx: MissionContext) -> MissionSpec:
    _seed(ctx.mission_config, ctx.platform_config)
    mission = parse_yaml(MISSION_YML_PATH, plugins={})
    return MissionSpec(
        id="sharjahsat",
        name=ctx.mission_config.get("mission_name") or _MISSION_NAME,
        packets=SharjahsatPacketOps(),
        spec_root=mission,
        config=MissionConfigSpec(),
    )
