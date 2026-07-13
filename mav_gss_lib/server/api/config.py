"""
mav_gss_lib.server.api.config -- Status / Config Routes

Endpoints: api_status, api_selfcheck, api_config_get, api_config_put

/api/config speaks native split shape `{platform, mission: {id, config}}` —
the same shape the backend stores on disk and holds in `WebRuntime`.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mav_gss_lib.config import (
    get_rx_zmq_addr,
    get_tx_zmq_addr,
    save_operator_config,
    split_to_persistable,
)
from mav_gss_lib.platform.config import (
    DEFAULT_PLATFORM_CONFIG_SPEC,
    apply_mission_config_update,
    apply_platform_config_update,
    persist_mission_config,
)
from mav_gss_lib.platform.tx.verifiers import write_instances
from ..state import get_runtime
from ..security import require_api_token

if TYPE_CHECKING:
    from ..state import WebRuntime

router = APIRouter()


def _schema_count(runtime: "WebRuntime") -> int:
    if runtime.mission.commands is None:
        return 0
    return len(runtime.mission.commands.schema())


def _mission_meta(runtime: "WebRuntime", key: str, default: str = "") -> str:
    value = runtime.mission_cfg.get(key) if isinstance(runtime.mission_cfg, dict) else None
    return str(value) if value else default


@router.get("/api/status")
async def api_status(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {
        "mission": runtime.mission_id,
        "mission_name": runtime.mission_name,
        "version": runtime.version,
        "zmq_rx": runtime.rx.status.get(),
        "zmq_tx": runtime.tx.status.get(),
        "frame_label": runtime.frame_label,
        "frequency": runtime.tx_frequency,
        "rx_frequency": runtime.rx_frequency,
        "tx_frequency": runtime.tx_frequency,
        "schema_path": _mission_meta(runtime, "command_defs_resolved")
            or _mission_meta(runtime, "command_defs"),
        "schema_count": _schema_count(runtime),
        "schema_warning": _mission_meta(runtime, "command_defs_warning"),
        "auth_token": runtime.session_token,
        "log_dir": runtime.log_dir,
        "logging": runtime.rx.log is not None,
        "session_log_json": runtime.rx.log.jsonl_path if runtime.rx.log else None,
    }


@router.get("/api/selfcheck")
async def api_selfcheck(request: Request) -> dict[str, Any]:
    """Lightweight diagnostic for verifying runtime environment."""
    runtime = get_runtime(request)

    from mav_gss_lib.config import get_operator_config_path
    config_path = str(get_operator_config_path())
    config_exists = os.path.isfile(config_path)

    schema_resolved = _mission_meta(runtime, "command_defs_resolved")
    schema_exists = os.path.isfile(schema_resolved) if schema_resolved else False

    from ..state import WEB_DIR
    web_build = (WEB_DIR / "index.html").is_file()
    asset_dir = (WEB_DIR / "assets").is_dir()

    return {
        "mission": runtime.mission_id,
        "mission_name": runtime.mission_name,
        "version": runtime.version,
        "config_path": config_path,
        "config_exists": config_exists,
        "schema_path": schema_resolved,
        "schema_exists": schema_exists,
        "schema_count": _schema_count(runtime),
        "schema_warning": _mission_meta(runtime, "command_defs_warning"),
        "web_build": web_build,
        "web_assets": asset_dir,
        "zmq_rx_addr": get_rx_zmq_addr(runtime.platform_cfg),
        "zmq_tx_addr": get_tx_zmq_addr(runtime.platform_cfg),
        "zmq_rx_status": runtime.rx.status.get(),
        "zmq_tx_status": runtime.tx.status.get(),
        "log_dir": runtime.log_dir,
    }


@router.get("/api/config")
async def api_config_get(request: Request) -> dict[str, Any]:
    """Return the operator config in native split shape.

    Response shape matches `WebRuntime`'s primary state:
        {"platform": {...}, "mission": {"id": "...", "config": {...}}}

    Runtime-derived fields (`version`, `build_sha`) are included on
    `platform.general` so the UI can display them.
    """
    runtime = get_runtime(request)
    return {
        "platform": runtime.platform_cfg,
        "mission": {
            "id": runtime.mission_id,
            "name": runtime.mission_name,
            "config": runtime.mission_cfg,
        },
    }


@router.put("/api/config", response_model=None)
async def api_config_put(update: dict[str, Any], request: Request) -> dict[str, Any] | JSONResponse:
    """Apply a native-shape config update.

    Accepted shape:
        {"platform": {...}, "mission": {"config": {...}}}

    Platform bucket is filtered through `DEFAULT_PLATFORM_CONFIG_SPEC`
    (strips runtime-derived keys and install-time sections). Mission
    bucket runs through `MissionSpec.config` (mission-declared editable
    paths win; protected paths are refused).
    """
    runtime = get_runtime(request)
    denied = require_api_token(request)
    if denied:
        return denied

    platform_update = update.get("platform") if isinstance(update.get("platform"), dict) else {}
    mission_section = update.get("mission") if isinstance(update.get("mission"), dict) else {}
    mission_update = mission_section.get("config") if isinstance(mission_section.get("config"), dict) else {}

    old_rx_addr = get_rx_zmq_addr(runtime.platform_cfg)
    old_tx_addr = get_tx_zmq_addr(runtime.platform_cfg)
    requested_rx = platform_update.get("rx") if isinstance(platform_update.get("rx"), dict) else {}
    requested_tx = platform_update.get("tx") if isinstance(platform_update.get("tx"), dict) else {}
    requested_rx_addr = requested_rx.get("zmq_addr", old_rx_addr)
    requested_tx_addr = requested_tx.get("zmq_addr", old_tx_addr)
    with runtime.tx.send_lock:
        sending_active = runtime.tx.sending["active"]
    if sending_active and (requested_rx_addr != old_rx_addr or requested_tx_addr != old_tx_addr):
        return JSONResponse(status_code=409, content={"error": "cannot change ZMQ addresses during active send"})

    verification_reset = False
    with runtime.cfg_lock:
        old_verifiers_enabled = bool(
            (runtime.platform_cfg.get("tx") or {}).get("verifiers_enabled", True)
        )
        apply_platform_config_update(runtime.platform_cfg, platform_update, DEFAULT_PLATFORM_CONFIG_SPEC)
        new_verifiers_enabled = bool(
            (runtime.platform_cfg.get("tx") or {}).get("verifiers_enabled", True)
        )
        if old_verifiers_enabled and not new_verifiers_enabled:
            verification_reset = True
            runtime.platform.verifiers.clear_open()
            try:
                write_instances(
                    Path(runtime.log_dir) / ".pending_instances.jsonl", []
                )
            except Exception as exc:
                logging.warning("pending_instances clear failed: %s", exc)
        if mission_update:
            # apply_mission_config_update returns a full-state merge starting
            # from deepcopy(current), so .update() overwrites every top-level
            # key with the new value. We do NOT .clear() first — the TX
            # framer reads runtime.mission_cfg without holding cfg_lock, and
            # a clear()+update() window would let it observe an empty dict
            # mid-update. Top-level dict identity stays stable so MissionSpec
            # captures of this reference keep seeing live state.
            updated = apply_mission_config_update(
                runtime.mission_cfg,
                mission_update,
                runtime.mission.config,
            )
            runtime.mission_cfg.update(updated)

        # Filter mission_cfg to operator-editable paths only — seeded mission
        # constants (nodes, ptypes, mission_name, ui titles, ...) live in the
        # mission's defaults.py and must not drift onto disk.
        mission_persistable = persist_mission_config(
            runtime.mission_cfg, runtime.mission.config,
        )
        # Ephemeral mode (GSS_EPHEMERAL=1) keeps mutations in-memory so
        # the operator's real gss.yml / mission.yml stay untouched during
        # a fake_flight test session. The next non-ephemeral start sees
        # the original on-disk values.
        if not getattr(runtime, "config_save_disabled", False):
            save_operator_config(
                split_to_persistable(
                    runtime.platform_cfg,
                    runtime.mission_id,
                    mission_persistable,
                )
            )
        new_rx_addr = get_rx_zmq_addr(runtime.platform_cfg)
        new_tx_addr = get_tx_zmq_addr(runtime.platform_cfg)

    if verification_reset:
        await runtime.tx.broadcast_verification_reset()
    if new_tx_addr != old_tx_addr:
        runtime.tx.restart_pub(new_tx_addr)
        if runtime.tx.log:
            runtime.tx.log.set_zmq_addr(new_tx_addr)
    if new_rx_addr != old_rx_addr:
        runtime.rx.restart_receiver()
        if runtime.rx.log:
            runtime.rx.log.set_zmq_addr(new_rx_addr)
    return {"ok": True}
