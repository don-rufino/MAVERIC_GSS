"""
mav_gss_lib.server.tx._send_coordinator -- TX send-loop coordinator.

Owns the serialized send-loop and its supporting helpers (per-item handlers,
guard wait, blackout arming, verifier-pending wait, finalize cleanup). State
lives on the parent ``TxService`` (queue, history, sending dict, locks); this
module's role is to orchestrate the loop, not to own the data.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, NamedTuple

from mav_gss_lib.platform import EncodedCommand, FramedCommand
from mav_gss_lib.platform.tx.verifiers import is_settled, is_terminal
from mav_gss_lib.transport import send_pdu

from .queue import QueueItem

if TYPE_CHECKING:
    from .service import TxService


class _RunResult(NamedTuple):
    aborted: bool
    sent_delta: int


@dataclass
class _SendContext:
    """Per-run state shared across per-item handlers."""
    sent: int
    total: int
    sock: object
    default_delay: int
    blackout_ms: int


class _SendCoordinator:
    """Owns the TX send-loop. Sibling-only — instantiated by ``TxService``."""

    def __init__(self, service: "TxService") -> None:
        self.service = service

    async def _wait_ms(self, ms: int) -> bool:
        """Sleep up to *ms* ms or until abort fires. Returns True if aborted."""
        svc = self.service
        if ms <= 0:
            return svc.abort.is_set()
        if svc.abort.is_set():
            return True
        try:
            await asyncio.wait_for(svc.abort.wait(), timeout=ms / 1000.0)
            return True
        except asyncio.TimeoutError:
            return False

    async def _wait_for_terminal_stage(
        self, *, poll_ms: int = 250, max_wait_ms: int = 35_000,
    ) -> bool:
        """Block until every open instance has reached terminal stage, or abort.

        Weaker gate than ``_wait_for_pending_verifications_clear``: terminal
        stage means the command has succeeded / failed / timed out, but
        declared late-NACK / NACK-window outcomes may still be pending. Used
        by the post-last-item UI dwell so the SENDING / Abort banner clears
        as soon as the operator knows the outcome, rather than waiting on
        NACK-window expiry that adds no information to a succeeded command.

        Returns True if aborted.
        """
        svc = self.service
        if svc.abort.is_set():
            return True
        registry = svc.runtime.platform.verifiers
        deadline = time.time() + max_wait_ms / 1000.0
        while any(not is_terminal(inst) for inst in registry.open_instances()):
            if time.time() >= deadline:
                logging.warning(
                    "terminal-stage wait timed out after %dms — proceeding with cleanup",
                    max_wait_ms,
                )
                return False
            try:
                await asyncio.wait_for(svc.abort.wait(), timeout=poll_ms / 1000.0)
                return True
            except asyncio.TimeoutError:
                continue
        return False

    async def _wait_for_pending_verifications_clear(
        self, *, poll_ms: int = 250, max_wait_ms: int = 35_000,
        # max_wait_ms ≥ max(every verifier window) + 5_000
    ) -> bool:
        """Block until every verifier instance is settled, or abort.

        Honors the mission correlation invariant via the simpler global rule
        operators expect: at most one verification window is open at a time.
        The next mission_cmd waits until every prior instance is settled
        (terminal stage AND every verifier outcome decided) before publishing.

        Cross-target batches serialize too — accepted tradeoff for predictable
        per-row UI: each row's rail/dots tell a clean story without overlap.

        Hard cap of `max_wait_ms` prevents the queue from stalling forever if
        the periodic sweeper somehow fails to advance an instance to settled;
        proceed with the next send rather than freeze.

        Returns True if aborted.
        """
        svc = self.service
        if svc.abort.is_set():
            return True
        registry = svc.runtime.platform.verifiers
        deadline = time.time() + max_wait_ms / 1000.0
        while any(not is_settled(inst) for inst in registry.open_instances()):
            if time.time() >= deadline:
                logging.warning(
                    "verifier wait timed out after %dms — proceeding with next send",
                    max_wait_ms,
                )
                return False
            try:
                await asyncio.wait_for(svc.abort.wait(), timeout=poll_ms / 1000.0)
                return True
            except asyncio.TimeoutError:
                continue
        return False

    async def _run_note_item(self, item: QueueItem, ctx: _SendContext) -> _RunResult:
        """Drop a front-of-queue ``note`` item without numbering impact."""
        self.service._pop_unnumbered_note()
        return _RunResult(aborted=False, sent_delta=0)

    async def _run_delay_item(self, item: QueueItem, ctx: _SendContext) -> _RunResult:
        """Execute a ``delay`` queue item."""
        svc = self.service
        with svc.send_lock:
            svc.sending["sent_at"] = 0
            svc.sending["waiting"] = True  # MUST be set before broadcast — tests assume the flag is already True.
        await svc.broadcast({
            "type": "send_progress", "sent": ctx.sent, "total": ctx.total,
            "current": f"delay {item['delay_ms']}ms", "waiting": True,
        })
        aborted = await self._wait_ms(item["delay_ms"])
        with svc.send_lock:
            svc.sending["waiting"] = False
        if aborted:
            return _RunResult(aborted=True, sent_delta=0)
        svc._pop_and_renumber()
        return _RunResult(aborted=False, sent_delta=0)

    async def _run_checkpoint_item(self, item: QueueItem, ctx: _SendContext) -> _RunResult:
        """Pause the queue until the operator confirms the checkpoint."""
        aborted = await self._run_guard_wait(
            item,
            kind="checkpoint",
            label=item.get("text", "Confirm before continuing"),
        )
        if aborted:
            return _RunResult(aborted=True, sent_delta=0)
        self.service._pop_and_renumber()
        return _RunResult(aborted=False, sent_delta=0)

    async def _run_guard_wait(
        self,
        item: QueueItem,
        *,
        kind: str = "command",
        label: str | None = None,
    ) -> bool:
        """Broadcast guard prompt and block until approved or aborted.

        Uses asyncio.wait on both events so either approval or abort wakes
        the waiter immediately, without the previous 100 ms polling loop.
        """
        svc = self.service
        with svc.send_lock:
            svc.sending["guarding"] = True  # MUST be set before broadcast — tests assume the flag is already True.
        svc.guard_ok.clear()
        prompt = label if label is not None else item.get("cmd_id", "")
        await svc.broadcast({
            "type": "guard_confirm", "index": 0,
            "cmd_id": prompt,
            "kind": kind,
            "text": prompt,
            "mission": item.get("mission", {}),
        })
        guard_task = asyncio.ensure_future(svc.guard_ok.wait())
        abort_task = asyncio.ensure_future(svc.abort.wait())
        try:
            await asyncio.wait(
                {guard_task, abort_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (guard_task, abort_task):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            with svc.send_lock:
                svc.sending["guarding"] = False
        return svc.abort.is_set()

    def _frame_mission_cmd(self, item: QueueItem) -> tuple[FramedCommand, EncodedCommand]:
        """Ask the active mission to frame the command's encoded bytes.

        Queue items persist mission-opaque bytes only, not the EncodedCommand
        object. We rebuild it through the mission command pipeline, then
        prefer the persisted raw bytes if present so guard-time rendering
        stays stable even if the mission encoder is non-deterministic.
        """
        svc = self.service
        encoded_raw = item.get("raw_cmd", b"")
        encoded = svc.runtime.mission.commands.encode(
            svc.runtime.mission.commands.parse_input(item.get("payload") or {})
        )
        if encoded_raw and encoded.raw != encoded_raw:
            encoded = EncodedCommand(
                raw=encoded_raw,
                cmd_id=encoded.cmd_id,
                src=encoded.src,
                guard=encoded.guard,
                mission_facts=encoded.mission_facts,
                parameters=encoded.parameters,
            )
        return svc.runtime.mission.commands.frame(encoded), encoded

    async def _finalize_send(self, sent: int) -> None:
        """Common cleanup path for send_complete / send_aborted."""
        svc = self.service
        with svc.send_lock:
            svc.save_queue()
            svc.sending.update(active=False, idx=-1, total=0, guarding=False, sent_at=0, waiting=False)

        remaining = len(svc.queue)
        if svc.abort.is_set():
            await svc.broadcast({"type": "send_aborted", "sent": sent, "remaining": remaining})
        else:
            await svc.broadcast({"type": "send_complete", "sent": sent})

        await svc.send_queue_update()
        await svc.broadcast({"type": "history", "items": svc.history[-svc.runtime.max_history:]})

    async def _run_mission_cmd_item(self, item: QueueItem, ctx: _SendContext) -> _RunResult:
        """Execute one ``mission_cmd`` queue item end-to-end: optional guard,
        same-target wait, frame build, ZMQ send, blackout arm, history record,
        post-send dwell."""
        svc = self.service
        if item.get("guard"):
            if await self._run_guard_wait(item):
                return _RunResult(aborted=True, sent_delta=0)

        raw_cmd = item.get("raw_cmd", b"")
        if not raw_cmd:
            await svc.broadcast({"type": "send_error", "error": f"empty raw_cmd for {item.get('cmd_id', '?')}"})
            svc._pop_and_renumber()
            return _RunResult(aborted=True, sent_delta=0)

        try:
            framed, encoded_for_verifier = self._frame_mission_cmd(item)
        except Exception as exc:
            logging.error("Frame build failed for %s: %s", item.get("cmd_id", "?"), exc)
            await svc.broadcast({"type": "send_error", "error": f"frame build failed: {exc}"})
            svc._pop_and_renumber()
            return _RunResult(aborted=True, sent_delta=0)

        # Sequential-verification gate: only one command's CheckWindow is open
        # at a time. Block here if any prior instance is still non-terminal,
        # so the next send only fires after the previous command is confirmed
        # / failed / timed_out. The visible "waiting" state mirrors
        # `_run_delay_item` so the operator sees the queue is paced, not stuck.
        #
        # The wait is gated on the periodic sweeper actually being live —
        # without it, non-terminal instances never advance and the wait would
        # block forever. The lifespan startup attaches `verifier_sweep_task`;
        # unit-test runtimes (which skip lifespan) don't, and so bypass the
        # wait entirely.
        if getattr(svc.runtime, "verifier_sweep_task", None) is not None:
            if any(not is_settled(inst)
                   for inst in svc.runtime.platform.verifiers.open_instances()):
                svc.sending["waiting"] = True
                await svc.send_queue_update()
                try:
                    if await self._wait_for_pending_verifications_clear():
                        return _RunResult(aborted=True, sent_delta=0)
                finally:
                    svc.sending["waiting"] = False
                    await svc.send_queue_update()

        if not send_pdu(ctx.sock, framed.wire):
            logging.error("ZMQ send failed for %s", item.get("cmd_id", "?"))
            await svc.broadcast({"type": "send_error", "error": "ZMQ send failed"})
            svc._pop_and_renumber()
            return _RunResult(aborted=True, sent_delta=0)

        # Arm (or clear) the TX→RX blackout window so RxService drops
        # packets arriving while the simulated radio is transmitting.
        if ctx.blackout_ms > 0:
            svc.runtime.tx_blackout_until = time.time() + ctx.blackout_ms / 1000.0
            await svc.runtime.rx.broadcast({"type": "blackout", "ms": ctx.blackout_ms})
        else:
            # Feature disabled at send time. If a prior batch armed a
            # deadline that is still in the future, emit an explicit
            # clear (ms=0) so every connected RX view — main dashboard
            # and pop-out alike — hides its indicator deterministically
            # instead of waiting for the old timer to drain.
            if svc.runtime.tx_blackout_until > time.time():
                await svc.runtime.rx.broadcast({"type": "blackout", "ms": 0})
            svc.runtime.tx_blackout_until = 0.0

        with svc.send_lock:
            svc.sending["sent_at"] = time.time()
        svc.count += 1

        # Generate ONE shared event_id for the tx_command log record, the
        # CommandInstance back-pointer, and the WS history broadcast so the
        # frontend (Task 26) can join verifier rows to history rows.
        from pathlib import Path as _Path
        import uuid as _uuid
        from mav_gss_lib.platform._log_envelope import new_event_id
        from mav_gss_lib.platform.tx.verifiers import (
            CommandInstance,
            VerifierOutcome,
            write_instances,
        )

        tx_event_id = new_event_id()

        from mav_gss_lib.platform.spec import derive_verifier_set
        from mav_gss_lib.platform.tx.verifiers import VerifierSet
        try:
            spec_root = getattr(svc.runtime.mission, "spec_root", None)
            if spec_root is None:
                vset = VerifierSet(verifiers=())
            else:
                vset = derive_verifier_set(
                    spec_root,
                    cmd_id=encoded_for_verifier.cmd_id,
                    mission_facts=encoded_for_verifier.mission_facts,
                )
        except Exception as exc:
            logging.warning("verifier_set derivation failed: %s", exc)
            vset = VerifierSet(verifiers=())

        instance: CommandInstance | None = None
        if vset.verifiers:  # skip register when mission declares "verification disabled"
            key = svc.runtime.mission.commands.correlation_key(encoded_for_verifier)
            instance = CommandInstance(
                instance_id=_uuid.uuid4().hex,
                correlation_key=key,
                t0_ms=int(time.time() * 1000),
                cmd_event_id=tx_event_id,
                verifier_set=vset,
                outcomes={v.verifier_id: VerifierOutcome.pending() for v in vset.verifiers},
                stage="released",
            )
            svc.runtime.platform.verifiers.register(instance)
            try:
                write_instances(
                    _Path(svc.runtime.log_dir) / ".pending_instances.jsonl",
                    svc.runtime.platform.verifiers.open_instances(),
                )
            except Exception as exc:
                logging.warning("pending_instances write failed: %s", exc)

        # Broadcast everything dirty — register() marked the new instance dirty,
        # so consume_dirty() returns exactly the just-registered instance here.
        # Using consume_dirty() (rather than an explicit broadcast) keeps
        # `_dirty` as the single source of truth for "needs broadcasting" so
        # the next RX tick's consume_dirty() doesn't re-broadcast it.
        for _inst in svc.runtime.platform.verifiers.consume_dirty():
            asyncio.create_task(svc.broadcast_verifier_instance(_inst))

        # Stamp the shared event_id onto the still-queued item so the
        # frontend can render the verifier tick strip immediately, while the
        # row is in 'sending' state. Without this the dots stay empty until
        # the item transitions to history. Push a fresh queue_update so
        # clients pick up the new id without waiting for the post-send dwell.
        if instance is not None:
            item["event_id"] = tx_event_id
            await svc.send_queue_update()

        hist_entry = svc._record_sent(item, raw_cmd, framed, event_id=tx_event_id)

        if instance is not None and svc.log:
            try:
                svc.log.write_cmd_verifier({
                    "seq": svc.count,
                    "cmd_event_id": instance.cmd_event_id,
                    "instance_id": instance.instance_id,
                    "stage": "released",
                    "verifier_id": "",
                    "outcome": "passed",
                    "elapsed_ms": 0,
                    "match_event_id": None,
                })
            except Exception as exc:
                logging.warning("cmd_verifier release log failed: %s", exc)

        new_sent = ctx.sent + 1
        await svc.broadcast({"type": "sent", "data": hist_entry})
        current_label = item.get("cmd_id", "?")
        await svc.broadcast({"type": "send_progress", "sent": new_sent, "total": ctx.total, "current": current_label, "waiting": False})

        # Post-send visible dwell. The sent item stays at queue-front for
        # `tx.delay_ms` so operators see the SENDING animation and the
        # "— delay" indicator; aborts wake immediately. `delay_ms=0` skips
        # the dwell for zero-overhead back-to-back sends.
        dwell_aborted = False
        if ctx.default_delay > 0:
            with svc.send_lock:
                svc.sending["waiting"] = True
            await svc.broadcast({
                "type": "send_progress", "sent": new_sent, "total": ctx.total,
                "current": current_label, "waiting": True,
            })
            dwell_aborted = await self._wait_ms(ctx.default_delay)
            with svc.send_lock:
                svc.sending["waiting"] = False

        with svc.send_lock:
            if svc.queue:
                svc.queue.pop(0)
            svc.sending["sent_at"] = 0
            svc.renumber_queue()
            svc.save_queue()
        return _RunResult(aborted=dwell_aborted, sent_delta=1)

    async def run_send(self) -> None:
        """Run the serialized TX send loop until queue exhaustion or abort."""
        svc = self.service
        if svc.zmq_sock is None:
            await svc.broadcast({"type": "send_error", "error": "TX ZMQ socket not initialized"})
            with svc.send_lock:
                svc.sending.update(active=False, idx=-1, total=0, guarding=False, sent_at=0, waiting=False)
            await svc.send_queue_update()
            return

        with svc.runtime.cfg_lock:
            default_delay = svc.runtime.tx_delay_ms
            blackout_ms = svc.runtime.tx_blackout_ms

        ctx = _SendContext(
            sent=0,
            total=svc.sending.get("total", len(svc.queue)),
            sock=svc.zmq_sock,
            default_delay=default_delay,
            blackout_ms=blackout_ms,
        )

        try:
            while not svc.abort.is_set():
                with svc.send_lock:
                    if not svc.queue:
                        break
                    item = svc.queue[0]
                    svc.sending["idx"] = 0
                    svc.sending["waiting"] = False

                await svc.send_queue_update()

                handler = _ITEM_DISPATCH.get(item["type"])
                if handler is None:
                    logging.error("Unknown queue item type: %r", item.get("type"))
                    break
                result = await handler(self, item, ctx)
                ctx.sent += result.sent_delta
                if result.aborted:
                    break

            # Last-item UI dwell: hold `sending` true until the final
            # command's verifier instance reaches terminal stage (complete
            # / failed / timed_out). The admission gate above gates the
            # NEXT send on `is_settled` (NACK window must close); here we
            # only hold the UI banner long enough for the operator to see
            # the outcome — clearing on `is_settled` would add ~10s of
            # stale UI to every successful command while unfired NACK
            # windows tick down to window_expired. Gated on the periodic
            # sweeper actually running so unit tests (which skip lifespan)
            # don't hang.
            if (not svc.abort.is_set()
                    and getattr(svc.runtime, "verifier_sweep_task", None) is not None):
                if any(not is_terminal(inst)
                       for inst in svc.runtime.platform.verifiers.open_instances()):
                    svc.sending["waiting"] = True
                    await svc.send_queue_update()
                    await self._wait_for_terminal_stage()
                    svc.sending["waiting"] = False
        finally:
            await self._finalize_send(ctx.sent)


_ItemHandler = Callable[[_SendCoordinator, dict, _SendContext], Awaitable[_RunResult]]
_ITEM_DISPATCH: dict[str, _ItemHandler] = {
    "note":        _SendCoordinator._run_note_item,
    "delay":       _SendCoordinator._run_delay_item,
    "checkpoint":  _SendCoordinator._run_checkpoint_item,
    "mission_cmd": _SendCoordinator._run_mission_cmd_item,
}
