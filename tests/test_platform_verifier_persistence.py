"""Persist open instances. Restart resumes non-terminal ones; elapsed time
past CheckWindow stop_ms marks the verifier window_expired retroactively.
Terminal instances are dropped on persist (they live in the log).
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mav_gss_lib.platform.tx.verifiers import (
    CheckWindow, VerifierSpec, VerifierSet, VerifierOutcome, CommandInstance,
    VerifierRegistry, serialize_instance, parse_instance,
    write_instances, restore_instances,
)


def _sample_spec() -> VerifierSpec:
    return VerifierSpec("uppm_ack", "received", CheckWindow(0, 10000), "UPPM", "info")


def _sample_instance(stage="released") -> CommandInstance:
    return CommandInstance(
        instance_id="i1",
        correlation_key=("mtq_set_1", "LPPM"),  # tuple; JSON-safe for persistence
        t0_ms=1_000_000,
        cmd_event_id="c1",
        verifier_set=VerifierSet(verifiers=(_sample_spec(),)),
        outcomes={"uppm_ack": VerifierOutcome.pending()},
        stage=stage,
    )


class SerializeRoundTrip(unittest.TestCase):
    def test_roundtrip(self):
        inst = _sample_instance()
        inst.outcomes["uppm_ack"] = VerifierOutcome.passed(matched_at_ms=500, match_event_id="e1")
        line = serialize_instance(inst)
        parsed = parse_instance(json.loads(line))
        self.assertEqual(parsed.instance_id, inst.instance_id)
        self.assertEqual(parsed.correlation_key, inst.correlation_key)
        self.assertEqual(parsed.outcomes["uppm_ack"].matched_at_ms, 500)


class WriteThrough(unittest.TestCase):
    def test_rewrite_drops_terminal_keeps_active(self):
        """Persistence drops settled (terminal) instances. Protocol invariant:
        spacecraft never emits NACK after ACK+RES, so a stage=complete
        instance has nothing more to wait for and need not persist."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".pending_instances.jsonl"
            reg = VerifierRegistry()
            i1 = _sample_instance(stage="released")  # active → kept
            i2 = _sample_instance(stage="complete")  # terminal → dropped
            i2.instance_id = "i2"
            i2.correlation_key = ("com_ping", "UPPM")
            reg.register(i1); reg.register(i2)
            write_instances(path, reg.open_instances())
            lines = path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 1)  # i1 persisted; i2 dropped
            ids = sorted(parse_instance(json.loads(l)).instance_id for l in lines)
            self.assertEqual(ids, ["i1"])


class RestoreElapsed(unittest.TestCase):
    def test_restore_drops_fully_expired_single_verifier_instance(self):
        """An instance whose ONLY verifier window has closed by restore time
        derives to stage=timed_out and is dropped — it must not linger in the
        registry blocking admission for its (cmd_id, dest) key."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".pending_instances.jsonl"
            inst = _sample_instance()  # t0_ms=1_000_000, uppm_ack stop=10000
            write_instances(path, [inst])
            # Restart 12s later → the uppm_ack window has closed.
            restored = restore_instances(path, now_ms=1_012_000)
            self.assertEqual(len(restored), 0)

    def test_restore_keeps_instance_with_live_verifiers(self):
        """Multi-verifier instance where only some windows have expired:
        stage stays non-terminal (e.g. 'released'), instance is restored,
        expired outcomes are marked window_expired."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".pending_instances.jsonl"
            # Multi-verifier: uppm_ack (stop=10s) + res (stop=30s)
            inst = CommandInstance(
                instance_id="i1",
                correlation_key=("mtq_set_1", "LPPM"),
                t0_ms=1_000_000, cmd_event_id="c1",
                verifier_set=VerifierSet(verifiers=(
                    VerifierSpec("uppm_ack", "received", CheckWindow(0, 10000), "UPPM", "info"),
                    VerifierSpec("res_from_lppm", "complete", CheckWindow(0, 30000), "RES", "success"),
                )),
                outcomes={
                    "uppm_ack": VerifierOutcome.pending(),
                    "res_from_lppm": VerifierOutcome.pending(),
                },
                stage="released",
            )
            write_instances(path, [inst])
            # Restart 15s later → uppm_ack window closed, res still open.
            restored = restore_instances(path, now_ms=1_015_000)
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0].outcomes["uppm_ack"].state, "window_expired")
            self.assertEqual(restored[0].outcomes["res_from_lppm"].state, "pending")
            self.assertNotIn(restored[0].stage, ("complete", "failed", "timed_out"))

    def test_restore_preserves_pending_inside_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".pending_instances.jsonl"
            inst = _sample_instance()
            write_instances(path, [inst])
            restored = restore_instances(path, now_ms=1_005_000)  # 5s in
            self.assertEqual(restored[0].outcomes["uppm_ack"].state, "pending")

    def test_restore_drops_terminal_after_expiry(self):
        """If all windows would now be expired AND the instance had a
        Complete or Failed already recorded, drop it (it's terminal)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".pending_instances.jsonl"
            # Don't write terminals in the first place (write_instances drops them).
            # But if an old file has one (crash mid-write), skip it on restore.
            path.write_text(
                json.dumps({
                    "instance_id": "i1",
                    "correlation_key": ["x", "LPPM"],
                    "t0_ms": 1_000_000,
                    "cmd_event_id": "c1",
                    "verifier_set": {
                        "verifiers": [
                            {"verifier_id": "uppm_ack", "stage": "received",
                             "check_window": {"start_ms": 0, "stop_ms": 10000},
                             "display_label": "UPPM", "display_tone": "info"},
                        ],
                    },
                    "outcomes": {
                        "uppm_ack": {"state": "passed", "matched_at_ms": 1000, "match_event_id": "e1"},
                    },
                    "stage": "complete",
                }) + "\n"
            )
            restored = restore_instances(path, now_ms=1_020_000)
            self.assertEqual(len(restored), 0)


class StartupRestorePolicy(unittest.TestCase):
    def test_startup_with_verifiers_disabled_skips_restore_and_clears_disk(self):
        from fastapi.testclient import TestClient
        from mav_gss_lib.platform.runtime import PlatformRuntime
        from mav_gss_lib.server.app import create_app

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app()
            runtime = app.state.runtime
            runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
            runtime.platform_cfg.setdefault("tx", {})["verifiers_enabled"] = False
            pending_path = Path(tmp) / ".pending_instances.jsonl"
            write_instances(pending_path, [_sample_instance()])

            with (
                patch.object(
                    PlatformRuntime, "restore_verifiers", autospec=True,
                ) as restore,
                patch(
                    "mav_gss_lib.server.app.schedule_update_check",
                ),
                patch(
                    "mav_gss_lib.server.app.run_preflight_and_broadcast",
                    new=AsyncMock(),
                ),
                TestClient(app),
            ):
                self.assertEqual(runtime.platform.verifiers.open_instances(), [])

            restore.assert_not_called()
            self.assertTrue(pending_path.exists())
            self.assertEqual(pending_path.read_text(), "")


if __name__ == "__main__":
    unittest.main()
