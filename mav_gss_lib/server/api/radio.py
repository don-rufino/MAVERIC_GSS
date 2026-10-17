"""Radio process control REST endpoints."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..security import require_api_token
from ..state import get_runtime

router = APIRouter()


def _maybe_require_read_token(request: Request):
    cfg = get_runtime(request).platform_cfg.get("auth", {})
    if cfg.get("require_token_for_reads"):
        return require_api_token(request)
    return None


@router.get("/api/radio/status")
async def api_radio_status(request: Request) -> Any:
    denied = _maybe_require_read_token(request)
    if denied:
        return denied
    return get_runtime(request).radio.status()


@router.get("/api/radio/noise-floor")
async def api_radio_noise_floor(request: Request, limit: int = 10) -> Any:
    """Live pass (with its per-second series), the latest logged pass and the
    recent logged passes of the active mission."""
    denied = _maybe_require_read_token(request)
    if denied:
        return denied
    radio = get_runtime(request).radio
    snapshot = await asyncio.to_thread(radio.noise_floor.snapshot, limit=max(1, min(int(limit), 50)))
    snapshot["radio_names"] = radio.radios.names()
    return snapshot


@router.get("/api/radio/radios")
async def api_radio_radios(request: Request) -> Any:
    """Station-wide list of the USRPs the flowgraph has run on, with their names."""
    denied = _maybe_require_read_token(request)
    if denied:
        return denied
    radio = get_runtime(request).radio
    return {"radios": radio.radios.snapshot(), "usrp": radio.status().get("usrp")}


@router.put("/api/radio/radios")
async def api_radio_radios_update(request: Request) -> Any:
    """Rename radios: body {"names": {serial: name}}."""
    denied = require_api_token(request)
    if denied:
        return denied
    body = await request.json()
    names = body.get("names") if isinstance(body, dict) else None
    if not isinstance(names, dict):
        return JSONResponse({"error": "expected {names: {serial: name}}"}, status_code=400)
    radio = get_runtime(request).radio
    await asyncio.to_thread(radio.set_radio_names, {str(k): str(v) for k, v in names.items()})
    return {"radios": radio.radios.snapshot()}


@router.get("/api/radio/logs")
async def api_radio_logs(request: Request) -> Any:
    denied = _maybe_require_read_token(request)
    if denied:
        return denied
    return {"lines": get_runtime(request).radio.log_snapshot()}


def _action_response(status: dict[str, Any], action: str) -> Any:
    err = str(status.get("error") or "")
    if not status.get("enabled"):
        return JSONResponse({"error": err or "radio integration disabled", "status": status}, status_code=409)
    if action in ("start", "restart") and status.get("state") not in ("running",) and err:
        return JSONResponse({"error": err, "status": status}, status_code=500)
    return status


@router.post("/api/radio/start")
async def api_radio_start(request: Request) -> Any:
    denied = require_api_token(request)
    if denied:
        return denied
    status = await asyncio.to_thread(get_runtime(request).radio.start)
    return _action_response(status, "start")


@router.post("/api/radio/stop")
async def api_radio_stop(request: Request) -> Any:
    denied = require_api_token(request)
    if denied:
        return denied
    status = await asyncio.to_thread(get_runtime(request).radio.stop)
    return _action_response(status, "stop")


@router.post("/api/radio/restart")
async def api_radio_restart(request: Request) -> Any:
    denied = require_api_token(request)
    if denied:
        return denied
    status = await asyncio.to_thread(get_runtime(request).radio.restart)
    return _action_response(status, "restart")
