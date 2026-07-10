"""
mav_gss_lib.server.api.logs -- Log Browsing Routes

Endpoints:
  GET /api/logs                          -- list sessions
  GET /api/logs/{session_id}             -- stream rx_packet / tx_command events
  GET /api/logs/{session_id}/parameters  -- stream parameter events

Records on disk use the unified envelope built by
``mav_gss_lib.platform.log_records``; the API does no RX/TX forking —
every record already carries ``event_id``, ``event_kind``, ``ts_ms``,
``ts_iso``, ``session_id``, ``seq``, ``v``, ``mission_id``, ``operator``,
``station`` and can be streamed straight to the viewer.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from ..state import get_runtime


def _strip_nonfinite(value: Any) -> Any:
    """Replace NaN / +Inf / -Inf with None so the response is strict-JSON.

    Starlette's JSONResponse uses ``allow_nan=False`` and aborts with a
    500 if any fragment contains a non-finite float (e.g. a sensor that
    returned NaN when unpowered). We walk the response once and coerce
    those sentinels to null — the SQL team reads them as ``NULL`` on
    ingest, which is the right semantics for "no reading"."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [_strip_nonfinite(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_nonfinite(v) for k, v in value.items()}
    return value

router = APIRouter()

_DEFAULT_KINDS = "rx_packet,tx_command,radio,tracking"
_MAX_LIMIT = 10000


def _session_date_ms(session_id: str) -> int:
    """Extract UTC midnight of the session's calendar day in milliseconds.

    Used to translate HH:MM / HH:MM:SS query filters into ts_ms. Session IDs
    look like `session_YYYYMMDD_HHMMSS[_mission][_station][_op]`; the date
    token is always the second underscore-separated field.
    """
    try:
        parts = session_id.split("_")
        ymd = parts[1]
        dt = datetime(int(ymd[0:4]), int(ymd[4:6]), int(ymd[6:8]),
                      tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (IndexError, ValueError):
        return 0


def _hhmm_to_ms(hhmm: str, base_ms: int) -> Optional[int]:
    """Translate "HH:MM" or "HH:MM:SS" into ts_ms using *base_ms* as midnight.

    Returns ``None`` on malformed input so the caller can skip the filter
    rather than reject the request — the operator types free-form values."""
    hhmm = hhmm.strip()
    if not hhmm:
        return None
    parts = hhmm.split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
        s = int(parts[2]) if len(parts) == 3 else 0
    except ValueError:
        return None
    return base_ms + int(timedelta(hours=h, minutes=m, seconds=s).total_seconds() * 1000)


def _entry_labels(entry: dict) -> list[str]:
    """Return generic searchable labels for a log entry.

    Reads only ``mission.cmd_id`` — the canonical envelope field, populated
    symmetrically by both RX and TX record builders. The older fallback
    into ``mission.facts.header.cmd_id`` was MAVERIC-shape-specific
    reach-through and is no longer needed: ``build_mission_facts`` emits
    ``cmd_id`` at the top level for RX, mirroring what the codec emits for
    TX (see commit 833421e for the TX side).
    """
    labels: list[str] = []
    mission = entry.get("mission")
    if not isinstance(mission, dict):
        return labels
    cmd_id = mission.get("cmd_id")
    if isinstance(cmd_id, str) and cmd_id:
        labels.append(cmd_id)
    return labels


def _label_matches(entry: dict, needle: str) -> bool:
    """Case-insensitive substring match against derived generic labels."""
    needle = needle.lower()
    return any(needle in value.lower() for value in _entry_labels(entry))


def _iter_entries(path: Path) -> Iterable[dict]:
    """Yield one JSON object per non-blank line; silently skip malformed lines."""
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _resolve_log_file(runtime: Any, session_id: str) -> Path | JSONResponse:
    """Resolve a session id to its on-disk path, defending against path traversal."""
    log_dir = (Path(runtime.log_dir) / "json").resolve()
    log_file = (log_dir / f"{session_id}.jsonl").resolve()
    if log_file.parent != log_dir:
        return JSONResponse(status_code=400, content={"error": "invalid session_id"})
    if not log_file.is_file():
        return JSONResponse(status_code=404, content={"error": "session not found"})
    return log_file


_SESSION_STAMP_RE = (  # matches the YYYYMMDD_HHMMSS token in a session_id
    r"(\d{8}_\d{6})"
)


def _session_sort_key(stem: str) -> str:
    """Lexicographic-chronological key from the session_id stamp.

    The stamp token sorts sessions by capture time rather than by mtime,
    which can change when files are copied or touched."""
    import re
    m = re.search(_SESSION_STAMP_RE, stem)
    return m.group(1) if m else stem


@router.get("/api/logs")
async def api_logs(request: Request) -> list[dict[str, Any]]:
    """List all sessions in <log_dir>/json, newest first.

    Sort by the stamp embedded in the filename rather than file mtime so
    sessions stay in chronological order even if files are copied or touched.
    """
    runtime = get_runtime(request)
    log_dir = Path(runtime.log_dir) / "json"
    if not log_dir.is_dir():
        return []
    sessions = []
    for path in log_dir.glob("*.jsonl"):
        stem = path.stem
        sessions.append({
            "session_id": stem,
            "filename": path.name,
            "size": path.stat().st_size,
            "mtime": path.stat().st_mtime,
            "direction": "session",
        })
    sessions.sort(key=lambda item: _session_sort_key(item["session_id"]), reverse=True)
    return sessions


@router.get("/api/logs/{session_id}", response_model=None)
async def api_log_entries(
    session_id: str,
    request: Request,
    label: Optional[str] = None,
    time_from: Optional[str] = Query(None, alias="from"),
    time_to: Optional[str] = Query(None, alias="to"),
    event_kind: str = _DEFAULT_KINDS,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=_MAX_LIMIT),
) -> dict[str, Any] | JSONResponse:
    """Stream event records from one session's JSONL file.

    Filters:
      - ``label``: substring match against derived generic labels, including
        TX and RX ``mission.cmd_id`` (the canonical envelope field).
      - ``from`` / ``to``: HH:MM or HH:MM:SS, compared against ts_ms using
        the session's calendar day as the reference midnight.
      - ``event_kind``: comma-separated whitelist. Defaults to
        ``rx_packet,tx_command,radio,tracking`` so parameter rows do not flood
        the viewer's packet list (they are fetched separately via the
        ``/parameters`` endpoint), while system audit events stay visible.
    """
    runtime = get_runtime(request)
    log_file = _resolve_log_file(runtime, session_id)
    if isinstance(log_file, JSONResponse):
        return log_file

    allowed_kinds = {k.strip() for k in event_kind.split(",") if k.strip()}
    base_ms = _session_date_ms(session_id)
    ts_from = _hhmm_to_ms(time_from, base_ms) if time_from else None
    ts_to = _hhmm_to_ms(time_to, base_ms) if time_to else None

    entries = []
    has_more = False
    matched = 0
    for entry in _iter_entries(log_file):
        if entry.get("event_kind") not in allowed_kinds:
            continue
        if label and not _label_matches(entry, label):
            continue
        ts_ms = entry.get("ts_ms")
        if ts_from is not None and isinstance(ts_ms, int) and ts_ms < ts_from:
            continue
        if ts_to is not None and isinstance(ts_ms, int) and ts_ms > ts_to:
            continue

        if matched < offset:
            matched += 1
            continue
        if len(entries) < limit:
            entries.append(entry)
            matched += 1
        else:
            has_more = True
            break

    return _strip_nonfinite(
        {"entries": entries, "has_more": has_more, "offset": offset, "limit": limit}
    )


@router.get("/api/logs/{session_id}/parameters", response_model=None)
async def api_log_parameters(
    session_id: str,
    request: Request,
    name: Optional[str] = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=_MAX_LIMIT),
) -> dict[str, Any] | JSONResponse:
    """Return flat parameter rows from a session, optionally filtered by name.

    Cheap because parameters are already one-event-per-row on disk. Intended
    for ad-hoc graph queries before the SQL archive is online.
    """
    runtime = get_runtime(request)
    log_file = _resolve_log_file(runtime, session_id)
    if isinstance(log_file, JSONResponse):
        return log_file

    entries = []
    has_more = False
    matched = 0
    for entry in _iter_entries(log_file):
        if entry.get("event_kind") != "parameter":
            continue
        if name and entry.get("name") != name:
            continue
        if matched < offset:
            matched += 1
            continue
        if len(entries) < limit:
            entries.append(entry)
            matched += 1
        else:
            has_more = True
            break

    return _strip_nonfinite(
        {"entries": entries, "has_more": has_more, "offset": offset, "limit": limit}
    )
