"""SCION-X mission assembly.

RX-only MissionSpec: PacketOps over MAV_DUO's SCION-X burst decoder output
plus a declarative mission.yml that carries only identity and RX columns —
the beacon field layout is team-supplied and read from the local
beacon_fields.json at runtime (see telemetry.py). No CommandOps — the
platform rejects TX admission with a clean error (astrocast pattern).
spec_root is required for server boot: the alarm environment dereferences it.

IQ recording is switched on the first time SCION-X runs on a machine: every
pass is kept for decoder work whether or not it demodulates. The operator
can turn it off; that choice sticks once the config is saved.
"""

from __future__ import annotations

from pathlib import Path

from mav_gss_lib.platform import MissionConfigSpec, MissionContext, MissionSpec
from mav_gss_lib.platform.spec import parse_yaml

from mav_gss_lib.missions.scionx.packets import ScionxPacketOps
from mav_gss_lib.missions.scionx.tracking_defaults import seed_tracking_defaults


MISSION_DIR = Path(__file__).resolve().parent
MISSION_YML_PATH = MISSION_DIR / "mission.yml"

_RX_DEFAULTS = {"frequency": "437.500 MHz"}
_RADIO_SCRIPT = "gnuradio/MAV_DUO.py"
_DECODER_YML = "gnuradio/decoders/SCIONX_DECODER.yml"
_MISSION_NAME = "SCION-X"
_IQ_DEFAULT_MARKER = "iq_record_default_applied"


def _seed(mission_cfg: dict, platform_cfg: dict) -> None:
    if isinstance(mission_cfg, dict):
        mission_cfg.setdefault("mission_name", _MISSION_NAME)
    rx = platform_cfg.setdefault("rx", {})
    if isinstance(rx, dict):
        for key, value in _RX_DEFAULTS.items():
            rx.setdefault(key, value)
    radio = platform_cfg.setdefault("radio", {})
    if isinstance(radio, dict):
        radio.setdefault("script", _RADIO_SCRIPT)
        radio["decoder_yml"] = _DECODER_YML
        # Platform defaults already hold iq_record: False, so a setdefault
        # would never fire. Turn it on once; a saved operator choice wins.
        if isinstance(mission_cfg, dict) and not mission_cfg.get(_IQ_DEFAULT_MARKER):
            radio["iq_record"] = True
            mission_cfg[_IQ_DEFAULT_MARKER] = True
    seed_tracking_defaults(platform_cfg)


def build(ctx: MissionContext) -> MissionSpec:
    _seed(ctx.mission_config, ctx.platform_config)
    mission = parse_yaml(MISSION_YML_PATH, plugins={})
    return MissionSpec(
        id="scionx",
        name=ctx.mission_config.get("mission_name") or _MISSION_NAME,
        packets=ScionxPacketOps(),
        spec_root=mission,
        config=MissionConfigSpec(),
    )
