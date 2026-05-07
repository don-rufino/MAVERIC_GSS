"""Side-effect bundle for AlarmRegistry transitions.

Holds two pluggable sinks (audit + broadcast) and the asyncio loop the
broadcast schedules onto. Lives in the platform package so it has no
FastAPI / WS / I/O imports — sinks and the loop are injected.

Author:  Irfan Annuar - USC ISI SERC
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from threading import RLock
from typing import Awaitable, Protocol

from mav_gss_lib.platform.alarms.contract import AlarmChange, AlarmState, Severity
from mav_gss_lib.platform.alarms.serialization import serialize_change


class AuditSink(Protocol):
    def write_alarm(self, change: AlarmChange, ts_ms: int) -> None: ...


class BroadcastTarget(Protocol):
    """Anything that knows how to async-broadcast a JSON string.

    The server adapter is ``WebRuntime.alarm_clients`` paired with
    ``broadcast_safe``; tests can pass a no-op.
    """
    def broadcast_text(self, text: str) -> Awaitable[None]: ...


@dataclass(frozen=True, slots=True)
class _AuditMark:
    state: AlarmState
    severity: Severity
    detail: str
    written_ms: int


@dataclass(frozen=True, slots=True)
class AlarmDispatch:
    audit_sink: AuditSink
    broadcast_target: BroadcastTarget
    loop: asyncio.AbstractEventLoop | None  # None disables broadcast (test mode)
    _last_audit: dict[str, _AuditMark] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )
    _audit_lock: RLock = field(
        default_factory=RLock, init=False, repr=False, compare=False,
    )

    def emit(self, change: AlarmChange | None, now_ms: int) -> None:
        if change is None:
            return
        if self._should_write_audit(change, now_ms):
            self.audit_sink.write_alarm(change, now_ms)
        if self.loop is None:
            return
        text = json.dumps(serialize_change(change))
        self.loop.call_soon_threadsafe(
            asyncio.ensure_future,
            self.broadcast_target.broadcast_text(text),
        )

    def _should_write_audit(self, change: AlarmChange, now_ms: int) -> bool:
        event = change.event
        with self._audit_lock:
            previous = self._last_audit.get(event.id)
            should_write = (
                previous is None
                or change.removed
                or bool(change.operator)
                or previous.state != event.state
                or previous.severity != event.severity
            )
            if not should_write:
                return False
            if change.removed:
                self._last_audit.pop(event.id, None)
            else:
                self._last_audit[event.id] = _AuditMark(
                    state=event.state,
                    severity=event.severity,
                    detail=event.detail,
                    written_ms=now_ms,
                )
            return True


def make_dispatch(
    audit_sink: AuditSink,
    broadcast_target: BroadcastTarget,
    loop: asyncio.AbstractEventLoop | None,
) -> AlarmDispatch:
    return AlarmDispatch(audit_sink=audit_sink,
                         broadcast_target=broadcast_target, loop=loop)


__all__ = ["AlarmDispatch", "AuditSink", "BroadcastTarget", "make_dispatch"]
