"""Tracking and Doppler REST endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from mav_gss_lib.platform.tracking import TrackingError
from mav_gss_lib.server.security import require_api_token
from mav_gss_lib.server.state import get_runtime

router = APIRouter()


def _tracking_or_error(fn):
    try:
        return fn()
    except TrackingError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})


@router.get("/api/tracking/config", response_model=None)
async def api_tracking_config(request: Request) -> dict[str, Any] | JSONResponse:
    runtime = get_runtime(request)
    return _tracking_or_error(runtime.tracking.config)


@router.get("/api/tracking/state", response_model=None)
async def api_tracking_state(
    request: Request,
    at_ms: int | None = Query(default=None),
    pass_count: int = Query(default=10, ge=0, le=20),
) -> dict[str, Any] | JSONResponse:
    runtime = get_runtime(request)
    return _tracking_or_error(lambda: runtime.tracking.state(time_ms=at_ms, pass_count=pass_count))


@router.get("/api/tracking/passes", response_model=None)
async def api_tracking_passes(
    request: Request,
    from_ms: int | None = Query(default=None),
    count: int = Query(default=10, ge=1, le=30),
) -> dict[str, Any] | JSONResponse:
    runtime = get_runtime(request)
    return _tracking_or_error(lambda: runtime.tracking.passes(from_ms=from_ms, count=count))


@router.get("/api/tracking/pass/{pass_id}", response_model=None)
async def api_tracking_pass_detail(
    pass_id: str,
    request: Request,
    from_ms: int | None = Query(default=None),
) -> dict[str, Any] | JSONResponse:
    runtime = get_runtime(request)
    result = _tracking_or_error(lambda: runtime.tracking.pass_by_id(pass_id, from_ms=from_ms))
    if result is None:
        return JSONResponse(status_code=404, content={"error": "pass not found"})
    return result


@router.get("/api/tracking/doppler", response_model=None)
async def api_tracking_doppler(
    request: Request,
    at_ms: int | None = Query(default=None),
) -> dict[str, Any] | JSONResponse:
    runtime = get_runtime(request)
    return _tracking_or_error(lambda: runtime.tracking.doppler(time_ms=at_ms))


@router.post("/api/tracking/doppler/connection/{state}", response_model=None)
async def api_tracking_doppler_connection(state: str, request: Request) -> dict[str, Any] | JSONResponse:
    denied = require_api_token(request)
    if denied:
        return denied
    normalized = state.strip().lower()
    if normalized in {"connect", "connected"}:
        connected = True
    elif normalized in {"disconnect", "disconnected"}:
        connected = False
    else:
        return JSONResponse(status_code=422, content={"error": f"unsupported Doppler connection state: {state}"})
    runtime = get_runtime(request)
    try:
        result = runtime.tracking.set_doppler_connected(connected)
    except (TrackingError, OSError, RuntimeError) as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})
    # Broadcast unconditionally (even on idempotent no-op) so a subscriber that
    # missed the original transition catches up immediately instead of waiting
    # for the next 1 Hz doppler tick to carry the mode forward.
    await runtime.doppler_broadcaster.publish({"type": "status", **runtime.tracking.status()})
    return {"connected": connected, "mode": result}


@router.post("/api/tracking/tle/fetch", response_model=None)
async def api_tracking_tle_fetch(request: Request) -> dict[str, Any] | JSONResponse:
    denied = require_api_token(request)
    if denied:
        return denied
    runtime = get_runtime(request)
    # Blocking urllib runs off the event loop.
    return await run_in_threadpool(runtime.tle_fetch.fetch_preview)


@router.get("/api/tracking/tle/status", response_model=None)
async def api_tracking_tle_status(request: Request) -> dict[str, Any]:
    runtime = get_runtime(request)
    return {**runtime.tle_fetch.status(), "spacetrack": runtime.tle_fetch.spacetrack_env_status()}


__all__ = ["router"]
