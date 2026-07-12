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
