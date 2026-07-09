"""Mission switching: list deployable missions, restart the server into one.

POST /api/mission/switch replies first, then a background thread tears down
the supervised radio child and the Doppler sink (``os.execv`` never runs the
lifespan shutdown, so skipping this would orphan the old mission's flowgraph
on the SDR) and re-execs MAV_WEB with ``--mission <id>`` so the new process
loads that mission's own operator config (``gss.<id>.yml``).

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mav_gss_lib.platform.loader import discover_missions
from mav_gss_lib.server.security import require_api_token
from mav_gss_lib.server.state import get_runtime
from mav_gss_lib.updater import _helpers


router = APIRouter()

# Let the POST response flush to the browser before teardown begins.
_RESPONSE_FLUSH_S = 0.4


def _argv_with_mission(argv: list[str], mission_id: str) -> list[str]:
    """Return argv with any existing --mission flag replaced by mission_id."""
    out: list[str] = []
    skip = False
    for token in argv:
        if skip:
            skip = False
            continue
        if token == "--mission":
            skip = True
            continue
        if token.startswith("--mission="):
            continue
        out.append(token)
    return [*out, "--mission", mission_id]


def _restart_into(runtime: Any, mission_id: str) -> None:
    time.sleep(_RESPONSE_FLUSH_S)
    try:
        runtime.tracking.disengage()
    except Exception:
        pass
    try:
        runtime.radio.shutdown()
    except Exception:
        pass
    _helpers._reexec(
        extra_env={"GSS_MISSION": mission_id, "MAV_MISSION_SWITCHED": mission_id},
        argv=_argv_with_mission(sys.argv, mission_id),
    )


# Test seam: monkeypatched so endpoint tests never hit os.execv.
_restart_fn = _restart_into


@router.get("/api/missions")
async def api_missions(request: Request) -> dict[str, Any]:
    """Deployable missions for the switcher dropdown (unauthenticated,
    matching /api/status)."""
    runtime = get_runtime(request)
    missions = discover_missions()
    if not any(m["id"] == runtime.mission_id for m in missions):
        missions.insert(0, {"id": runtime.mission_id, "name": runtime.mission_name})
    return {"active": runtime.mission_id, "missions": missions}


@router.post("/api/mission/switch")
async def api_mission_switch(body: dict, request: Request) -> Any:
    denied = require_api_token(request)
    if denied:
        return denied
    runtime = get_runtime(request)
    target = str(body.get("id") or "").strip()
    known = {m["id"] for m in discover_missions()}
    if target not in known:
        return JSONResponse(status_code=400, content={"error": f"unknown mission {target!r}"})
    if target == runtime.mission_id:
        return JSONResponse(status_code=400, content={"error": f"{target} is already the active mission"})
    threading.Thread(
        target=_restart_fn, args=(runtime, target),
        name="mission-switch-restart", daemon=True,
    ).start()
    return {"ok": True, "switching_to": target}
