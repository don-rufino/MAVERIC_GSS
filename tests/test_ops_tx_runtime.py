"""Operations-focused TX runtime tests for MAVERIC GSS."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mav_gss_lib.server.tx._send_coordinator as tx_service
from mav_gss_lib.platform.spec import HeaderFieldNotOverridable
from mav_gss_lib.platform.tx.verifiers import (
    CheckWindow,
    CommandInstance,
    VerifierOutcome,
    VerifierSet,
    VerifierSpec,
)
from mav_gss_lib.server.tx.queue import make_checkpoint, make_mission_cmd, sanitize_queue_items, validate_mission_cmd
from mav_gss_lib.server.state import create_runtime


def _make_payload(cmd_id, args="", dest="LPPM", guard=False):
    """Build a mission payload dict for testing."""
    return {
        "cmd_id": cmd_id,
        "args": args if isinstance(args, dict) else {},
        "packet": {"dest": dest},
        "guard": guard,
    }


class TestTxRuntime(unittest.TestCase):
    """Covers TX validation plus send/guard/delay/abort lifecycle behavior."""

    def setUp(self):
        self.runtime = create_runtime()
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime.platform_cfg.setdefault("general", {})["log_dir"] = self.tmp.name
        self.runtime.platform_cfg.setdefault("tx", {})["delay_ms"] = 0
        self.runtime.tx.queue.clear()
        self.runtime.tx.history.clear()
        self.runtime.tx.zmq_sock = object()
        self.sent_payloads = []
        self.messages = []
        self.queue_updates = []

        # Events signalled by the broadcast mock when the production code
        # announces a state transition. Tests `await` the event instead of
        # polling `sending[...]` in a sleep loop.
        self.guard_confirm_event = asyncio.Event()
        self.waiting_event = asyncio.Event()

        async def _capture(msg):
            self.messages.append(msg)
            if isinstance(msg, dict):
                if msg.get("type") == "guard_confirm":
                    self.guard_confirm_event.set()
                elif msg.get("type") == "send_progress" and msg.get("waiting"):
                    self.waiting_event.set()

        async def _capture_queue_update():
            # Capture snapshots instead of broadcasting over websockets.
            self.queue_updates.append(self.runtime.tx.sending.copy())

        self.runtime.tx.broadcast = _capture
        self.runtime.tx.send_queue_update = _capture_queue_update
        self.runtime.tx.log = None
        self._orig_send_pdu = tx_service.send_pdu
        tx_service.send_pdu = self._fake_send_pdu

    def tearDown(self):
        tx_service.send_pdu = self._orig_send_pdu
        self.tmp.cleanup()

    def _fake_send_pdu(self, _sock, payload):
        """Record sent payloads while reporting success to the TX service."""
        self.sent_payloads.append(payload)
        return True

    def _make_item(self, cmd_id="com_ping", args="", dest="LPPM", guard=False):
        """Build a validated mission_cmd queue item."""
        payload = _make_payload(cmd_id, args, dest, guard)
        return validate_mission_cmd(payload, runtime=self.runtime)

    def test_unknown_command_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown command"):
            validate_mission_cmd(_make_payload("definitely_not_real", "REQ"), runtime=self.runtime)

    def test_tlm_beacon_is_commandable_with_fixed_dest(self):
        item = validate_mission_cmd(
            {"cmd_id": "tlm_beacon", "args": {}, "guard": False},
            runtime=self.runtime,
        )

        self.assertEqual(item["cmd_id"], "tlm_beacon")
        self.assertEqual(item["mission"]["facts"]["header"]["dest"], "UPPM")

    def test_mission_block_carries_canonical_cmd_id(self):
        """Contract pin (symmetric with RX, see test_platform_maveric_spec.py):
        the TX queue item's ``mission`` dict carries cmd_id at the envelope
        level (``mission.cmd_id``) and NOT under ``mission.facts.header.cmd_id``.
        The frontend ``kind: cmd_id`` column resolver reads only the envelope
        field; dropping it here surfaces as ``--`` in the TX cmd column.
        """
        item = self._make_item("com_ping")
        self.assertEqual(item["mission"]["cmd_id"], "com_ping")
        self.assertNotIn("cmd_id", item["mission"]["facts"]["header"])

    def test_fixed_dest_command_rejects_dest_override(self):
        with self.assertRaisesRegex(HeaderFieldNotOverridable, "does not allow operator override"):
            validate_mission_cmd(_make_payload("tlm_beacon"), runtime=self.runtime)

    def test_missing_required_args_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_mission_cmd(_make_payload("set_voltage", ""), runtime=self.runtime)

    def test_asm_golay_size_limit_is_enforced(self):
        # MAVERIC is locked to ASM+Golay framing — oversize payloads must be
        # rejected at queue admission via the mission framer's MTU check.
        payload = {
            "cmd_id": "cfg_set_tle",
            "args": {"tle": "A" * 220},
            "guard": False,
        }
        with self.assertRaisesRegex(ValueError, "too large for ASM\\+Golay"):
            validate_mission_cmd(payload, runtime=self.runtime)

    def test_queue_restore_sanitizes_invalid_entries(self):
        valid = self._make_item("com_ping", "")
        invalid = {
            "type": "mission_cmd",
            "payload": _make_payload("not_real", "REQ"),
        }
        checkpoint = {"type": "checkpoint", "text": "operator check"}
        items, skipped = sanitize_queue_items([valid, {"type": "delay", "delay_ms": 250}, checkpoint, invalid], runtime=self.runtime)
        self.assertEqual(skipped, 1)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["type"], "mission_cmd")
        self.assertEqual(items[0]["cmd_id"], "com_ping")
        self.assertEqual(items[1]["type"], "delay")
        self.assertEqual(items[2]["type"], "checkpoint")

    def test_run_send_waits_for_checkpoint_confirmation(self):
        self.runtime.tx.queue = [
            make_checkpoint("Confirm EPS bus"),
            self._make_item("com_ping", ""),
        ]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(active=True, idx=-1, total=2, guarding=False, sent_at=0, waiting=False)

        async def _run():
            task = asyncio.create_task(self.runtime.tx.run_send())
            await asyncio.wait_for(self.guard_confirm_event.wait(), timeout=5.0)
            await asyncio.sleep(0)
            self.assertTrue(self.runtime.tx.sending["guarding"])
            self.runtime.tx.guard_ok.set()
            await task

        asyncio.run(_run())

        self.assertEqual(len(self.sent_payloads), 1)
        self.assertEqual(self.runtime.tx.queue, [])
        guard_msgs = [msg for msg in self.messages if isinstance(msg, dict) and msg.get("type") == "guard_confirm"]
        self.assertTrue(guard_msgs)
        self.assertEqual(guard_msgs[0].get("kind"), "checkpoint")

    def test_run_send_abort_during_checkpoint_keeps_queue_item(self):
        self.runtime.tx.queue = [
            make_checkpoint("Confirm EPS bus"),
            self._make_item("com_ping", ""),
        ]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(active=True, idx=-1, total=2, guarding=False, sent_at=0, waiting=False)

        async def _run():
            task = asyncio.create_task(self.runtime.tx.run_send())
            await asyncio.wait_for(self.guard_confirm_event.wait(), timeout=5.0)
            await asyncio.sleep(0)
            self.assertTrue(self.runtime.tx.sending["guarding"])
            self.runtime.tx.abort.set()
            await task

        asyncio.run(_run())

        self.assertEqual(len(self.sent_payloads), 0)
        self.assertEqual(len(self.runtime.tx.queue), 2)
        self.assertEqual(self.runtime.tx.queue[0]["type"], "checkpoint")

    def test_run_send_processes_delay_then_command(self):
        self.runtime.tx.queue = [
            {"type": "delay", "delay_ms": 100},
            self._make_item("com_ping", ""),
        ]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(active=True, idx=-1, total=len(self.runtime.tx.queue), guarding=False, sent_at=0, waiting=False)

        asyncio.run(self.runtime.tx.run_send())

        self.assertEqual(len(self.sent_payloads), 1)
        self.assertEqual(self.runtime.tx.queue, [])
        self.assertEqual(len(self.runtime.tx.history), 1)
        self.assertEqual(self.runtime.tx.history[0]["type"], "mission_cmd")
        self.assertEqual(self.runtime.tx.history[0]["cmd_id"], "com_ping")
        self.assertTrue(any(msg.get("type") == "send_complete" for msg in self.messages if isinstance(msg, dict)))

    def test_run_send_handles_cli_string_payload(self):
        item = validate_mission_cmd("LPPM NONE CMD com_ping", runtime=self.runtime)
        self.runtime.tx.queue = [item]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(active=True, idx=-1, total=1, guarding=False, sent_at=0, waiting=False)

        asyncio.run(self.runtime.tx.run_send())

        self.assertEqual(len(self.sent_payloads), 1)
        self.assertEqual(self.runtime.tx.queue, [])
        self.assertEqual(self.runtime.tx.history[0]["payload"], "LPPM NONE CMD com_ping")
        self.assertEqual(
            self.runtime.tx.history[0]["mission"]["facts"]["header"]["dest"],
            "LPPM",
        )

    def test_run_send_waits_for_guard_confirmation(self):
        self.runtime.tx.queue = [
            self._make_item("com_ping", "", guard=True),
        ]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(active=True, idx=-1, total=1, guarding=False, sent_at=0, waiting=False)

        async def _run():
            task = asyncio.create_task(self.runtime.tx.run_send())
            await asyncio.wait_for(self.guard_confirm_event.wait(), timeout=5.0)
            # Yield the loop once so any continuation the production code
            # has queued (e.g. the set-guarding-flag ordering relative to the
            # broadcast) drains before we assert. Belt-and-suspenders safety
            # against a future refactor that swaps the flag/broadcast order.
            await asyncio.sleep(0)
            self.assertTrue(self.runtime.tx.sending["guarding"])
            self.runtime.tx.guard_ok.set()
            await task

        asyncio.run(_run())

        self.assertEqual(len(self.sent_payloads), 1)
        self.assertEqual(self.runtime.tx.queue, [])
        self.assertTrue(any(msg.get("type") == "guard_confirm" for msg in self.messages if isinstance(msg, dict)))

    def test_run_send_abort_during_guard_keeps_queue_item(self):
        self.runtime.tx.queue = [
            self._make_item("com_ping", "", guard=True),
        ]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(active=True, idx=-1, total=1, guarding=False, sent_at=0, waiting=False)

        async def _run():
            task = asyncio.create_task(self.runtime.tx.run_send())
            await asyncio.wait_for(self.guard_confirm_event.wait(), timeout=5.0)
            # Yield the loop once; see comment in
            # test_run_send_waits_for_guard_confirmation.
            await asyncio.sleep(0)
            self.assertTrue(self.runtime.tx.sending["guarding"])
            self.runtime.tx.abort.set()
            await task

        asyncio.run(_run())

        self.assertEqual(len(self.sent_payloads), 0)
        self.assertEqual(len(self.runtime.tx.queue), 1)
        self.assertTrue(any(msg.get("type") == "send_aborted" for msg in self.messages if isinstance(msg, dict)))

    def test_run_send_abort_during_delay_keeps_following_items(self):
        self.runtime.tx.queue = [
            {"type": "delay", "delay_ms": 500},
            self._make_item("com_ping", ""),
        ]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(active=True, idx=-1, total=2, guarding=False, sent_at=0, waiting=False)

        async def _run():
            task = asyncio.create_task(self.runtime.tx.run_send())
            await asyncio.wait_for(self.waiting_event.wait(), timeout=5.0)
            # Yield the loop once; see comment in
            # test_run_send_waits_for_guard_confirmation.
            await asyncio.sleep(0)
            self.assertTrue(self.runtime.tx.sending["waiting"])
            self.runtime.tx.abort.set()
            await task

        asyncio.run(_run())

        self.assertEqual(len(self.sent_payloads), 0)
        self.assertEqual(len(self.runtime.tx.queue), 2)
        self.assertTrue(any(msg.get("type") == "send_aborted" for msg in self.messages if isinstance(msg, dict)))

    def test_history_entry_includes_payload(self):
        """History entries must include payload for faithful requeue."""
        self.runtime.tx.queue = [self._make_item("com_ping", "")]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(active=True, idx=-1, total=1, guarding=False, sent_at=0, waiting=False)

        asyncio.run(self.runtime.tx.run_send())

        self.assertEqual(len(self.runtime.tx.history), 1)
        hist = self.runtime.tx.history[0]
        self.assertIn("payload", hist)
        self.assertEqual(hist["payload"]["cmd_id"], "com_ping")

    def test_queue_projection_includes_payload(self):
        """Queue projection must include payload for faithful duplicate."""
        item = self._make_item("com_ping", "")
        self.runtime.tx.queue = [item]
        self.runtime.tx.renumber_queue()

        projected = self.runtime.tx.queue_items_json()
        self.assertEqual(len(projected), 1)
        self.assertIn("payload", projected[0])
        self.assertEqual(projected[0]["payload"]["cmd_id"], "com_ping")


    def test_delay_ms_zero_has_no_post_send_dwell(self):
        """With `tx.delay_ms=0`, back-to-back commands must not accrue dwell time.

        The post-send dwell is bound to `tx.delay_ms`; setting it to 0
        opts out entirely for max-throughput scenarios. Two zero-delay
        commands should clear well under the 500 ms dwell floor. Threshold
        0.6 s tolerates slow-CI disk latency on `save_queue` tempfile writes.
        """
        import time as _time

        self.runtime.tx.queue = [
            self._make_item("com_ping", ""),
            self._make_item("com_ping", ""),
        ]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(active=True, idx=-1, total=2, guarding=False, sent_at=0, waiting=False)

        t0 = _time.monotonic()
        asyncio.run(self.runtime.tx.run_send())
        elapsed = _time.monotonic() - t0

        self.assertEqual(len(self.sent_payloads), 2)
        self.assertLess(elapsed, 0.6, f"run_send took {elapsed:.3f}s — expected <0.6s with delay_ms=0")

    def test_verifiers_disabled_sends_repeats_without_wait_or_new_instances(self):
        verifier_set = VerifierSet(verifiers=(
            VerifierSpec(
                "uppm_ack", "received", CheckWindow(0, 30_000), "UPPM", "info",
            ),
        ))
        existing = CommandInstance(
            instance_id="existing",
            correlation_key=("com_ping", "LPPM"),
            t0_ms=0,
            cmd_event_id="existing-event",
            verifier_set=verifier_set,
            outcomes={"uppm_ack": VerifierOutcome.pending()},
            stage="released",
        )
        registry = self.runtime.platform.verifiers
        registry.register(existing)
        registry.consume_dirty()

        self.runtime.platform_cfg["tx"]["verifiers_enabled"] = False
        registry.clear_open()
        self.runtime.verifier_sweep_task = object()
        self.runtime.tx.broadcast_verifier_instance = AsyncMock()
        self.runtime.tx.queue = [
            self._make_item("com_ping", ""),
            self._make_item("com_ping", ""),
        ]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(
            active=True,
            idx=-1,
            total=2,
            guarding=False,
            sent_at=0,
            waiting=False,
        )

        with patch.object(
            tx_service._SendCoordinator,
            "_wait_for_pending_verifications_clear",
            new_callable=AsyncMock,
        ) as wait:
            asyncio.run(self.runtime.tx.run_send())

        wait.assert_not_awaited()
        self.assertEqual(len(self.sent_payloads), 2)
        self.assertEqual(registry.open_instances(), [])
        self.runtime.tx.broadcast_verifier_instance.assert_not_called()
        self.assertEqual(len(self.runtime.tx.history), 2)

    def test_reenable_during_disabled_publish_does_not_register_verifier(self):
        registry = self.runtime.platform.verifiers
        self.runtime.platform_cfg["tx"]["verifiers_enabled"] = False
        self.runtime.verifier_sweep_task = object()
        self.runtime.tx.broadcast_verifier_instance = AsyncMock()
        self.runtime.tx.queue = [self._make_item("com_ping", "")]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(
            active=True, idx=-1, total=1, guarding=False, sent_at=0, waiting=False,
        )

        def _send_and_reenable(_sock, payload):
            self.sent_payloads.append(payload)
            self.runtime.platform_cfg["tx"]["verifiers_enabled"] = True
            return True

        tx_service.send_pdu = _send_and_reenable
        with patch.object(
            tx_service._SendCoordinator,
            "_wait_for_pending_verifications_clear",
            new_callable=AsyncMock,
            return_value=False,
        ):
            asyncio.run(self.runtime.tx.run_send())

        self.assertEqual(len(self.sent_payloads), 1)
        self.assertEqual(registry.open_instances(), [])
        self.runtime.tx.broadcast_verifier_instance.assert_not_called()

    def test_disable_reenable_during_enabled_publish_invalidates_snapshot(self):
        registry = self.runtime.platform.verifiers
        self.runtime.platform_cfg["tx"]["verifiers_enabled"] = True
        self.runtime.tx.broadcast_verifier_instance = AsyncMock()
        self.runtime.tx.queue = [self._make_item("com_ping", "")]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(
            active=True, idx=-1, total=1, guarding=False, sent_at=0, waiting=False,
        )

        def _send_across_cancellation(_sock, payload):
            self.sent_payloads.append(payload)
            self.runtime.platform_cfg["tx"]["verifiers_enabled"] = False
            registry.clear_open()
            self.runtime.platform_cfg["tx"]["verifiers_enabled"] = True
            return True

        tx_service.send_pdu = _send_across_cancellation
        asyncio.run(self.runtime.tx.run_send())

        self.assertEqual(len(self.sent_payloads), 1)
        self.assertEqual(registry.open_instances(), [])
        self.runtime.tx.broadcast_verifier_instance.assert_not_called()

    def test_enabled_send_registers_and_broadcasts_verifier(self):
        self.runtime.platform_cfg["tx"]["verifiers_enabled"] = True
        self.runtime.tx.broadcast_verifier_instance = AsyncMock()
        self.runtime.tx.queue = [self._make_item("com_ping", "")]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(
            active=True, idx=-1, total=1, guarding=False, sent_at=0, waiting=False,
        )

        asyncio.run(self.runtime.tx.run_send())

        instances = self.runtime.platform.verifiers.open_instances()
        self.assertEqual(len(instances), 1)
        self.runtime.tx.broadcast_verifier_instance.assert_called_once_with(instances[0])
        self.assertEqual(
            instances[0].cmd_event_id,
            self.runtime.tx.history[0]["event_id"],
        )

    def test_disabling_verifiers_releases_an_active_verifier_wait(self):
        verifier_set = VerifierSet(verifiers=(
            VerifierSpec(
                "uppm_ack", "received", CheckWindow(0, 30_000), "UPPM", "info",
            ),
        ))
        self.runtime.platform.verifiers.register(CommandInstance(
            instance_id="existing",
            correlation_key=("com_ping", "LPPM"),
            t0_ms=0,
            cmd_event_id="existing-event",
            verifier_set=verifier_set,
            outcomes={"uppm_ack": VerifierOutcome.pending()},
            stage="released",
        ))
        self.runtime.platform_cfg["tx"]["verifiers_enabled"] = True

        async def _run():
            entered_wait = asyncio.Event()
            original_abort_wait = self.runtime.tx.abort.wait

            async def _signal_wait():
                entered_wait.set()
                await original_abort_wait()

            with patch.object(self.runtime.tx.abort, "wait", side_effect=_signal_wait):
                wait_task = asyncio.create_task(
                    self.runtime.tx._send_coord._wait_for_pending_verifications_clear(
                        poll_ms=10,
                        max_wait_ms=5_000,
                    )
                )
                await asyncio.wait_for(entered_wait.wait(), timeout=0.5)
                self.assertFalse(wait_task.done())
                self.runtime.platform_cfg["tx"]["verifiers_enabled"] = False
                return await asyncio.wait_for(wait_task, timeout=0.5)

        self.assertFalse(asyncio.run(_run()))

    def test_post_send_dwell_broadcasts_waiting_and_blocks(self):
        """With `tx.delay_ms>0`, the sent item dwells at queue-front with `waiting=True`.

        The dwell is what lets the UI show the SENDING animation and the
        "— delay" indicator. This verifies (a) a `send_progress` event
        with `waiting=True` is broadcast after the actual send, and (b)
        the dwell actually blocks for ~delay_ms.
        """
        import time as _time

        self.runtime.platform_cfg["tx"]["delay_ms"] = 150
        self.runtime.tx.queue = [self._make_item("com_ping", "")]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(active=True, idx=-1, total=1, guarding=False, sent_at=0, waiting=False)

        t0 = _time.monotonic()
        asyncio.run(self.runtime.tx.run_send())
        elapsed = _time.monotonic() - t0

        self.assertEqual(len(self.sent_payloads), 1)
        self.assertGreaterEqual(elapsed, 0.13, f"run_send took {elapsed:.3f}s — expected ≥0.13s with delay_ms=150")

        waiting_events = [
            msg for msg in self.messages
            if isinstance(msg, dict)
            and msg.get("type") == "send_progress"
            and msg.get("waiting") is True
        ]
        self.assertTrue(waiting_events, f"expected a send_progress waiting=True broadcast after send, got {self.messages}")
        sent_idx = next(i for i, m in enumerate(self.messages) if isinstance(m, dict) and m.get("type") == "sent")
        waiting_idx = self.messages.index(waiting_events[0])
        self.assertGreater(waiting_idx, sent_idx, "waiting=True must come after the 'sent' event")

    def test_abort_during_post_send_dwell_still_pops_sent_item(self):
        """Aborting during the dwell must not keep the already-transmitted cmd in queue."""
        self.runtime.platform_cfg["tx"]["delay_ms"] = 5000  # long dwell so abort can land inside it
        self.runtime.tx.queue = [self._make_item("com_ping", "")]
        self.runtime.tx.renumber_queue()
        self.runtime.tx.sending.update(active=True, idx=-1, total=1, guarding=False, sent_at=0, waiting=False)

        async def _run():
            task = asyncio.create_task(self.runtime.tx.run_send())
            await asyncio.wait_for(self.waiting_event.wait(), timeout=5.0)
            self.runtime.tx.abort.set()
            await task

        asyncio.run(_run())

        self.assertEqual(len(self.sent_payloads), 1)
        self.assertEqual(self.runtime.tx.queue, [])
        self.assertTrue(any(msg.get("type") == "send_aborted" for msg in self.messages if isinstance(msg, dict)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
