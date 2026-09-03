"""
mav_gss_lib.server.tx.service -- TX Service

Owns the TX side of the web runtime: queue, send state, history, and ZMQ PUB.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from mav_gss_lib.platform import FramedCommand, tx_command_record
from mav_gss_lib.transport import PUB_STATUS, init_zmq_pub, zmq_cleanup

from .._broadcast import broadcast_safe
from ._send_coordinator import _SendCoordinator
from .queue import QueueItem

if TYPE_CHECKING:
    from ..state import WebRuntime


class AdmitResult(Enum):
    ACCEPTED = "accepted"
    REJECTED_SEND_ACTIVE = "rejected_send_active"
    REJECTED_WINDOW_OPEN = "rejected_window_open"


class TxService:
    """Own the TX side of the web runtime: queue, send state, and history."""

    def __init__(self, runtime: "WebRuntime") -> None:
        self.runtime = runtime
        self.clients: list = []
        self.lock = threading.Lock()
        self.log = None
        self.count = 0
        self.zmq_ctx = None
        self.zmq_sock = None
        self.zmq_monitor = None
        self.queue: list[QueueItem] = []
        self.history: list = []
        self.sending = {"active": False, "idx": -1, "total": 0, "guarding": False, "sent_at": 0, "waiting": False}
        self.abort = asyncio.Event()
        self.guard_ok = asyncio.Event()
        self.send_lock = threading.Lock()
        self.send_task = None
        self._send_coord = _SendCoordinator(self)

    def queue_file(self):
        """Return the queue persistence file path used by this runtime."""
        return self.runtime.queue_file()

    def restart_pub(self, addr: str) -> None:
        """Recreate the TX PUB socket at *addr*."""
        if self.zmq_sock:
            try:
                zmq_cleanup(self.zmq_monitor, PUB_STATUS, self.status.get(), self.zmq_sock, self.zmq_ctx)
            except Exception:
                pass
        self.zmq_ctx = self.zmq_sock = self.zmq_monitor = None
        try:
            self.zmq_ctx, self.zmq_sock, self.zmq_monitor = init_zmq_pub(addr)
            self.status.set("BOUND")
        except Exception as exc:
            self.status.set("OFFLINE")
            logging.error("TX ZMQ PUB init failed: %s", exc)

    @property
    def status(self):
        return self.runtime.tx_status

    def save_queue(self) -> None:
        """Persist the current queue to disk as JSONL."""
        from . import queue as _tq
        _tq.save_queue(self.queue, self.queue_file())

    def load_queue(self):
        """Load any persisted queue items from disk."""
        from . import queue as _tq
        return _tq.load_queue(self.queue_file(), runtime=self.runtime)

    def renumber_queue(self) -> None:
        """Assign sequential display numbers to queued command items."""
        from . import queue as _tq
        _tq.renumber_queue(self.queue)

    def queue_summary(self):
        """Summarize queue size, guard count, and rough execution time."""
        from . import queue as _tq
        return _tq.queue_summary(self.queue, self.runtime.tx_delay_ms)

    def admit(self, item: dict) -> tuple[AdmitResult, dict]:
        """Admission gate.

        Rule 1: active send → reject all additions (incl. notes/delays;
                the operator's current batch should run without interference).
        Rule 2: mission_cmd + same mission-provided correlation key as an
                open CheckWindow instance → reject.
        Rule 3: accept.

        The key shape is opaque to the platform and is derived by CommandOps.
        """
        if self.sending.get("active"):
            return AdmitResult.REJECTED_SEND_ACTIVE, {}
        if item.get("type") != "mission_cmd":
            return AdmitResult.ACCEPTED, {}
        if not self.runtime.tx_verifiers_enabled:
            return AdmitResult.ACCEPTED, {}
        key = tuple(item.get("correlation_key") or ())
        open_inst = self.runtime.platform.verifiers.lookup_open(key)
        if open_inst is not None:
            now_ms = int(time.time() * 1000)
            elapsed = now_ms - open_inst.t0_ms
            max_stop = max(
                (v.check_window.stop_ms for v in open_inst.verifier_set.verifiers),
                default=0,
            )
            remaining = max(max_stop - elapsed, 0)
            return AdmitResult.REJECTED_WINDOW_OPEN, {
                "cmd_id": item.get("cmd_id", ""),
                "remaining_ms": remaining,
            }
        return AdmitResult.ACCEPTED, {}

    def queue_items_json(self):
        """Project the current queue into the websocket/API JSON shape."""
        from . import queue as _tq
        return _tq.queue_items_json(self.queue)

    async def broadcast(self, msg: dict[str, Any] | str) -> None:
        """Broadcast one JSON-serializable message to all TX websocket clients."""
        text = json.dumps(msg) if isinstance(msg, dict) else msg
        await broadcast_safe(self.clients, self.lock, text)

    async def send_queue_update(self):
        """Broadcast the current queue plus send-state snapshot."""
        await self.broadcast({"type": "queue_update", "items": self.queue_items_json(), "summary": self.queue_summary(), "sending": self.sending.copy()})

    async def broadcast_verifier_instance(self, instance) -> None:
        """Send a per-instance state snapshot to all /ws/tx clients.

        Shape:
          {"type": "verification_update",
           "instance": {
             "instance_id": ..., "correlation_key": [...],
             "t0_ms": ..., "cmd_event_id": ...,
             "stage": ..., "outcomes": {vid: {state, matched_at_ms, match_event_id}},
             "verifier_set": {"verifiers": [{...}]}
           }}
        """
        if not self.runtime.tx_verifiers_enabled:
            return
        from mav_gss_lib.platform.tx.verifiers import serialize_instance
        obj = json.loads(serialize_instance(instance))
        await self.broadcast({"type": "verification_update", "instance": obj})

    async def broadcast_verification_reset(self) -> None:
        """Clear verifier state in every connected TX client."""
        await self.broadcast({"type": "verification_restore", "instances": []})

    async def send_verification_restore(self, websocket) -> None:
        """One-shot snapshot to a freshly connected /ws/tx client."""
        from mav_gss_lib.platform.tx.verifiers import serialize_instance
        instances = []
        if self.runtime.tx_verifiers_enabled:
            instances = [
                json.loads(serialize_instance(i))
                for i in self.runtime.platform.verifiers.open_instances()
            ]
        await websocket.send_text(json.dumps({
            "type": "verification_restore",
            "instances": instances,
        }))

    # ── run_send helpers ──────────────────────────────────────────────

    def _pop_unnumbered_note(self) -> None:
        """Drop the front item without renumbering. Only valid for ``note`` items."""
        with self.send_lock:
            if self.queue:
                if self.queue[0]["type"] != "note":
                    raise TypeError(
                        f"_pop_unnumbered_note called on {self.queue[0]['type']!r}"
                    )
                self.queue.pop(0)
            self.save_queue()

    def _pop_and_renumber(self) -> None:
        """Drop the front item and renumber surviving commands.

        Not valid for ``note`` items — notes are unnumbered and do not
        participate in the mission-cmd numbering sequence.
        """
        with self.send_lock:
            if self.queue:
                if self.queue[0]["type"] == "note":
                    raise TypeError("_pop_and_renumber called on note item")
                self.queue.pop(0)
            self.renumber_queue()
            self.save_queue()


    def _record_sent(
        self,
        item: QueueItem,
        raw_cmd: bytes,
        framed: FramedCommand,
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Write the TX log entry and append a history item; return the history entry.

        ``event_id`` (if provided) is threaded through to ``tx_command_record`` and
        stamped onto ``hist_entry["event_id"]`` so callers can link history rows
        to verifier instances (the frontend joins on ``cmd_event_id``).
        """
        assert self.count > 0, "TX seq counter not incremented before _record_sent"
        assert len(raw_cmd) > 0, "TX record: raw_cmd is empty"
        assert len(framed.wire) > 0, "TX record: framed.wire is empty"
        assert framed.frame_label, "TX record: framed.frame_label is empty"

        if self.log:
            try:
                record = tx_command_record(
                    self.count,
                    cmd_id=item.get("cmd_id", ""),
                    mission_facts=item.get("mission", {}).get("facts", {}),
                    parameters=item.get("parameters", []),
                    raw_cmd=raw_cmd,
                    wire=framed.wire,
                    session_id=self.log.session_id,
                    ts_ms=int(time.time() * 1000),
                    version=self.runtime.version,
                    mission_id=self.runtime.mission_id,
                    operator=self.runtime.operator,
                    station=self.runtime.station,
                    frame_label=framed.frame_label,
                    log_fields=framed.log_fields,
                    event_id=event_id,
                )
                self.log.write_mission_command(
                    record,
                    raw_cmd=raw_cmd,
                    wire=framed.wire,
                    log_text=framed.log_text,
                )
            except Exception as exc:
                logging.warning("TX log write failed: %s", exc)
            self._log_tx_tracking_sample()

        hist_entry = {
            "n": self.count,
            "ts": datetime.now().strftime("%H:%M:%S"),
            "type": "mission_cmd",
            "operator": self.runtime.operator,
            "station": self.runtime.station,
            "cmd_id": item.get("cmd_id", ""),
            "mission": item.get("mission", {}),
            "parameters": item.get("parameters", []),
            "payload": item.get("payload", {}),
            "size": len(framed.wire),
            "wire_hex": framed.wire.hex(),
            "raw_hex": raw_cmd.hex(),
            "event_id": event_id or "",
        }
        self.history.append(hist_entry)
        if len(self.history) > self.runtime.max_history:
            del self.history[: len(self.history) - self.runtime.max_history]
        return hist_entry

    def _log_tx_tracking_sample(self) -> None:
        """Best-effort: log a tracking_sample tagged source="tx_attempt" at
        the moment a command is sent, so this uplink attempt's az/el and
        requested Doppler-corrected frequency are recorded even in throttled
        cadence mode (which otherwise only logs background ticks
        periodically). Reads the last-published tick rather than
        recomputing, so a TX send never triggers an extra Doppler-sink
        publish as a side effect."""
        broadcaster = getattr(self.runtime, "doppler_broadcaster", None)
        latest = getattr(broadcaster, "latest", None) if broadcaster is not None else None
        doppler = latest.get("doppler") if isinstance(latest, dict) else None
        if not doppler or not hasattr(self.log, "write_tracking_sample"):
            return
        try:
            self.log.write_tracking_sample(doppler, source="tx_attempt")
        except Exception as exc:
            logging.warning("TX tracking-sample log failed: %s", exc)

    async def run_send(self) -> None:
        """Public entry to the serialized TX send loop.

        The loop, per-item handlers, guard wait, blackout arm, and finalize
        cleanup live on ``_SendCoordinator`` (sibling-only). ``TxService``
        keeps the public surface (queue ops, history, ZMQ socket lifecycle).
        """
        await self._send_coord.run_send()

    async def clear_sent(self) -> int:
        """Drop all in-memory TX history and broadcast the empty list.

        Returns the count of cleared entries.

        Today this is unconditional. When command-verification ships,
        callers should reject with a structured error if any sent entry
        still has an open CheckWindow; that logic lives above this layer.
        """
        n = len(self.history)
        self.history.clear()
        await self.broadcast({
            "type": "history",
            "items": self.history[-self.runtime.max_history:],
        })
        return n
