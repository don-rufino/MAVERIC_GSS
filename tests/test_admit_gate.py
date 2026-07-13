"""TxService.admit gate rules (spec §6):
  1. Active-send: reject ALL queue additions.
  2. No active-send: reject re-queue of the same mission-provided
     correlation key while its CheckWindow is still open. Args are not part
     of the key in this fixture.
  3. Otherwise: accept.
"""
import unittest
from unittest.mock import MagicMock

from mav_gss_lib.server.tx.service import TxService, AdmitResult
from mav_gss_lib.platform.tx.verifiers import (
    CheckWindow, VerifierSpec, VerifierSet, VerifierOutcome, CommandInstance,
    VerifierRegistry,
)


def _runtime_with(registry, active=False, verifiers_enabled=True):
    r = MagicMock()
    r.platform_cfg = {
        "tx": {"delay_ms": 100, "verifiers_enabled": verifiers_enabled},
        "general": {"log_dir": "/tmp"},
    }
    r.mission_cfg = {}
    r.tx_delay_ms = 100
    r.tx_verifiers_enabled = verifiers_enabled
    r.tx_blackout_ms = 0
    r.platform.verifiers = registry
    tx = TxService(r)
    tx.sending["active"] = active
    return tx


def _open_instance_for(cmd_id="mtq_set_1", dest="LPPM"):
    vs = VerifierSet(verifiers=(
        VerifierSpec("uppm_ack", "received", CheckWindow(0, 10000), "UPPM", "info"),
    ))
    return CommandInstance(
        instance_id="i1",
        correlation_key=(cmd_id, dest),
        t0_ms=0, cmd_event_id="c1",
        verifier_set=vs,
        outcomes={"uppm_ack": VerifierOutcome.pending()},
        stage="released",
    )


def _full_lppm_instance(cmd_id="mtq_set_1", dest="LPPM"):
    """Mirror tests/test_platform_verifier_registry._instance — full set:
    uppm_ack + lppm_ack + res_from_lppm + nack_uppm + nack_lppm.
    Required so res_from_lppm has a verifier to apply against."""
    vs = VerifierSet(verifiers=(
        VerifierSpec("uppm_ack",      "received", CheckWindow(0, 10000), "UPPM", "info"),
        VerifierSpec("lppm_ack",      "received", CheckWindow(0, 15000), "LPPM", "info"),
        VerifierSpec("res_from_lppm", "complete", CheckWindow(0, 30000), "RES",  "success"),
        VerifierSpec("nack_uppm",     "failed",   CheckWindow(0, 30000), "NACK", "danger"),
        VerifierSpec("nack_lppm",     "failed",   CheckWindow(0, 30000), "NACK", "danger"),
    ))
    return CommandInstance(
        instance_id="i_full",
        correlation_key=(cmd_id, dest),
        t0_ms=0, cmd_event_id="c_full",
        verifier_set=vs,
        outcomes={v.verifier_id: VerifierOutcome.pending() for v in vs.verifiers},
        stage="released",
    )


def _item(cmd_id="mtq_set_1", args="", dest="LPPM"):
    """Queue-item shape: payload is mission-owned; key is precomputed by CommandOps."""
    return {"type": "mission_cmd",
            "cmd_id": cmd_id,
            "correlation_key": [cmd_id, dest],
            "payload": {
                "cmd_id": cmd_id,
                "args": args if isinstance(args, dict) else {},
                "packet": {"dest": dest},
            }}


class AdmitResults(unittest.TestCase):
    def test_active_send_blocks_everything(self):
        reg = VerifierRegistry()
        tx = _runtime_with(reg, active=True)
        result, info = tx.admit(_item())
        self.assertEqual(result, AdmitResult.REJECTED_SEND_ACTIVE)

    def test_disabled_verifiers_do_not_bypass_active_send_lock(self):
        reg = VerifierRegistry()
        tx = _runtime_with(reg, active=True, verifiers_enabled=False)
        result, info = tx.admit(_item())
        self.assertEqual(result, AdmitResult.REJECTED_SEND_ACTIVE)

    def test_open_window_blocks_same_cmd_id_and_dest(self):
        reg = VerifierRegistry()
        reg.register(_open_instance_for())
        tx = _runtime_with(reg, active=False)
        result, info = tx.admit(_item())
        self.assertEqual(result, AdmitResult.REJECTED_WINDOW_OPEN)

    def test_disabled_verifiers_allow_repeated_same_key_with_window_open(self):
        reg = VerifierRegistry()
        reg.register(_open_instance_for("com_ping", dest="LPPM"))
        tx = _runtime_with(reg, active=False, verifiers_enabled=False)

        for _ in range(3):
            result, info = tx.admit(_item(cmd_id="com_ping", dest="LPPM"))
            self.assertEqual(result, AdmitResult.ACCEPTED)
            self.assertEqual(info, {})

    def test_same_cmd_id_different_dest_allowed(self):
        reg = VerifierRegistry()
        reg.register(_open_instance_for("com_ping", dest="LPPM"))
        tx = _runtime_with(reg, active=False)
        result, info = tx.admit(_item(cmd_id="com_ping", dest="UPPM"))
        self.assertEqual(result, AdmitResult.ACCEPTED)

    def test_different_args_still_blocked_same_cmd_id_dest(self):
        """Strict admission: args differ but (cmd_id, dest) match → block.
        Responses from spacecraft can't distinguish args anyway."""
        reg = VerifierRegistry()
        reg.register(_open_instance_for("mtq_set_1", dest="LPPM"))
        tx = _runtime_with(reg, active=False)
        result, info = tx.admit(_item(cmd_id="mtq_set_1", args="2", dest="LPPM"))
        self.assertEqual(result, AdmitResult.REJECTED_WINDOW_OPEN)

    def test_released_after_complete(self):
        """Admission frees on stage=complete. Protocol invariant: spacecraft
        never emits NACK after ACK+RES, so a pending NACK outcome after RES
        is impossible on the wire — no reason to hold the slot."""
        reg = VerifierRegistry()
        inst = _full_lppm_instance()
        reg.register(inst)
        reg.apply(inst.instance_id, "res_from_lppm",
                  VerifierOutcome.passed(matched_at_ms=8000, match_event_id="e1"))
        self.assertEqual(inst.stage, "complete")
        tx = _runtime_with(reg, active=False)
        result, _ = tx.admit(_item())
        self.assertEqual(result, AdmitResult.ACCEPTED)

    def test_released_after_timed_out(self):
        """A command that gets nothing back: sweeper expires every window,
        stage flips to timed_out, slot frees."""
        reg = VerifierRegistry()
        inst = _full_lppm_instance()
        reg.register(inst)
        for spec in inst.verifier_set.verifiers:
            reg.apply(inst.instance_id, spec.verifier_id, VerifierOutcome.window_expired())
        self.assertEqual(inst.stage, "timed_out")
        tx = _runtime_with(reg, active=False)
        result, _ = tx.admit(_item())
        self.assertEqual(result, AdmitResult.ACCEPTED)

    def test_non_command_items_allowed_during_idle(self):
        reg = VerifierRegistry()
        tx = _runtime_with(reg, active=False)
        result, _ = tx.admit({"type": "note", "text": "stage-break"})
        self.assertEqual(result, AdmitResult.ACCEPTED)
        result, _ = tx.admit({"type": "checkpoint", "text": "confirm state"})
        self.assertEqual(result, AdmitResult.ACCEPTED)


if __name__ == "__main__":
    unittest.main()
