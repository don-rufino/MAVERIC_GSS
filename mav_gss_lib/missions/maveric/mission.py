"""MAVERIC MissionSpec entry point — declarative pipeline.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mav_gss_lib.missions.maveric.declarative import build_declarative_capabilities
from mav_gss_lib.missions.maveric.files.events import MavericFileChunkEvents
from mav_gss_lib.missions.maveric.files.registry import (
    build_file_kind_adapters,
    validate_against_mission,
)
from mav_gss_lib.missions.maveric.files.router import get_files_router
from mav_gss_lib.missions.maveric.files.store import ChunkFileStore
from mav_gss_lib.missions.maveric.packets import DeclarativePacketsAdapter
from mav_gss_lib.missions.maveric.alarm_predicates import PLUGINS as ALARM_PLUGINS
from mav_gss_lib.missions.maveric.plugin_tx_builder import get_tx_builder_route
from mav_gss_lib.missions.maveric.preflight import build_preflight
from mav_gss_lib.missions.maveric.tracking_defaults import seed_tracking_defaults
from mav_gss_lib.platform import EventOps, MissionConfigSpec, MissionContext, MissionSpec
from mav_gss_lib.platform.contract.mission import HttpOps


MISSION_DIR = Path(os.path.abspath(os.path.dirname(__file__)))
MISSION_YML_PATH = MISSION_DIR / "mission.yml"

# Real MAVERIC uplink routing (spacecraft CSP node 8, command service port
# 24), not neutral zeros: if the operator csp block is ever missing, seeding
# these keeps commands correctly addressed instead of silently routing to
# node 0. Operator gss.yml values still override (merged.update(existing)).
_CSP_DEFAULTS = {
    "priority": 2, "source": 0, "destination": 8,
    "dest_port": 24, "src_port": 0, "flags": 0, "csp_crc": True,
}
_IMAGING_DEFAULTS = {"thumb_prefix": "tn_"}
_RX_DEFAULTS = {"frequency": "437.575 MHz"}
_TX_DEFAULTS = {"frequency": "437.575 MHz"}
_RADIO_SCRIPT = "gnuradio/MAV_DUO.py"
_DECODER_YML = "gnuradio/decoders/MAVERIC_DECODER.yml"


def _seed(mission_cfg: dict[str, Any], platform_cfg: dict[str, Any]) -> None:
    """Gap-fill operator-overridable defaults. Identity keys
    (nodes/ptypes/mission_name) live in mission.yml extensions and are
    NOT seeded into mission_cfg — the codec is the protection."""
    for key, defaults in (
        ("csp", _CSP_DEFAULTS),
        ("imaging", _IMAGING_DEFAULTS),
    ):
        existing = mission_cfg.get(key) if isinstance(mission_cfg.get(key), dict) else {}
        merged = dict(defaults)
        merged.update(existing)
        mission_cfg[key] = merged
    if isinstance(platform_cfg, dict):
        rx_cfg = platform_cfg.setdefault("rx", {})
        if isinstance(rx_cfg, dict):
            for k, v in _RX_DEFAULTS.items():
                rx_cfg.setdefault(k, v)
        tx_cfg = platform_cfg.setdefault("tx", {})
        if isinstance(tx_cfg, dict):
            for k, v in _TX_DEFAULTS.items():
                tx_cfg.setdefault(k, v)
        radio_cfg = platform_cfg.setdefault("radio", {})
        if isinstance(radio_cfg, dict):
            radio_cfg.setdefault("script", _RADIO_SCRIPT)
            radio_cfg["decoder_yml"] = _DECODER_YML
        # MAVERIC TLE / station / frequency. Platform ships neutral
        # Sample-LEO defaults; MAVERIC seeds its actual values here so a
        # fresh checkout starts with the right satellite without the
        # operator having to write a gss.yml. Operator overrides win.
        seed_tracking_defaults(platform_cfg)


def _files_root(ctx: MissionContext) -> str:
    """File artifacts live under <log_dir>/files/.

    ``ctx.data_dir`` is the resolved log_dir (set by
    ``PlatformRuntime.from_split`` when constructing the MissionSpec).
    """
    return str(ctx.data_dir / "files")


def build(ctx: MissionContext) -> MissionSpec:
    _seed(ctx.mission_config, ctx.platform_config)

    capabilities = build_declarative_capabilities(
        mission_yml_path=MISSION_YML_PATH,
        mission_cfg=ctx.mission_config,
    )

    validate_against_mission(capabilities.mission)
    file_store = ChunkFileStore(_files_root(ctx))
    file_adapters = build_file_kind_adapters(ctx.mission_config)
    routers = [
        get_files_router(file_store, file_adapters),
        get_tx_builder_route(capabilities.packet_codec, capabilities.mission),
    ]

    preflight_hook = build_preflight(
        platform_config=ctx.platform_config,
        mission_config=ctx.mission_config,
        mission_dir=MISSION_DIR,
    )

    return MissionSpec(
        id="maveric",
        name=str(ctx.mission_config.get("mission_name") or "MAVERIC"),
        packets=DeclarativePacketsAdapter(
            codec=capabilities.packet_codec,
            mission=capabilities.mission,
        ),
        commands=capabilities.command_ops,
        spec_root=capabilities.mission,
        spec_plugins=capabilities.plugins,
        alarm_plugins=ALARM_PLUGINS,
        events=EventOps(sources=[MavericFileChunkEvents(
            store=file_store,
            adapters=file_adapters,
        )]),
        http=HttpOps(routers=routers),
        config=MissionConfigSpec(
            editable_paths={"csp.*", "imaging.thumb_prefix"},
            # mission.yml is the protection — operators can't edit
            # identity (nodes/ptypes/mission_name) via /api/config because
            # those keys live in mission.yml extensions, not mission_cfg.
            protected_paths=set(),
        ),
        preflight=preflight_hook,
    )
