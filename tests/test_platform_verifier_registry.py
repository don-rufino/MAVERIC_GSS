"""VerifierRegistry core: register, apply outcomes, stage derivation.

Stage rules (spec §4.4):
  - Any FailedVerifier passed  → instance Failed (NACK wins, even post-Complete)
  - Else CompleteVerifier passed → Complete (regardless of intermediate stages)
  - Else ACK-only sets complete when any ReceivedVerifier passes
  - Else all windows closed    → TimedOut
  - Else transient markers (Released / Received / Accepted)
"""
import unittest
from mav_gss_lib.platform.tx.verifiers import (
    CheckWindow, VerifierSpec, VerifierSet, VerifierOutcome, CommandInstance,
    VerifierRegistry,
)


def _set_lppm_full() -> VerifierSet:
    """Five verifiers: uppm_ack + lppm_ack + res_from_lppm + nack_uppm + nack_lppm."""
    return VerifierSet(verifiers=(
        VerifierSpec("uppm_ack",      "received", CheckWindow(0, 10000), "UPPM", "info"),
        VerifierSpec("lppm_ack",      "received", CheckWindow(0, 15000), "LPPM", "info"),
        VerifierSpec("res_from_lppm", "complete", CheckWindow(0, 30000), "RES",  "success"),
        VerifierSpec("nack_uppm",     "failed",   CheckWindow(0, 30000), "NACK", "danger"),
        VerifierSpec("nack_lppm",     "failed",   CheckWindow(0, 30000), "NACK", "danger"),
    ))


def _instance() -> CommandInstance:
    vs = _set_lppm_full()
    return CommandInstance(
        instance_id="i1",
        correlation_key=("mtq_set_1", "LPPM"),
        t0_ms=0,
        cmd_event_id="c1",
        verifier_set=vs,
        outcomes={v.verifier_id: VerifierOutcome.pending() for v in vs.verifiers},
        stage="released",
    )


def _ack_only_instance() -> CommandInstance:
    vs = VerifierSet(verifiers=(
        VerifierSpec("uppm_ack", "received", CheckWindow(0, 10000), "UPPM", "info"),
        VerifierSpec("hlnv_ack", "received", CheckWindow(0, 10000), "HLNV", "info"),
        VerifierSpec("nack_uppm", "failed", CheckWindow(0, 30000), "NACK", "danger"),
    ))
    return CommandInstance(
        instance_id="i_ack",
        correlation_key=("eps_cut", "EPS"),
        t0_ms=0,
        cmd_event_id="c_ack",
        verifier_set=vs,
        outcomes={v.verifier_id: VerifierOutcome.pending() for v in vs.verifiers},
        stage="released",
    )


class RegisterAndLookup(unittest.TestCase):
    def test_register_appears_in_open(self):
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        self.assertEqual(len(reg.open_instances()), 1)
        self.assertIs(reg.open_instances()[0], inst)

    def test_lookup_open_by_key(self):
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        found = reg.lookup_open(inst.correlation_key)
        self.assertIsNotNone(found)
        self.assertEqual(found.instance_id, "i1")

    def test_lookup_open_misses_unknown_key(self):
        reg = VerifierRegistry()
        reg.register(_instance())
        self.assertIsNone(reg.lookup_open(("other", b"", "UPPM")))


class StageDerivation(unittest.TestCase):
    def test_first_received_verifier_transitions_to_received(self):
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        reg.apply("i1", "uppm_ack", VerifierOutcome.passed(matched_at_ms=500, match_event_id="e1"))
        self.assertEqual(inst.stage, "received")

    def test_all_received_transitions_to_accepted(self):
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        reg.apply("i1", "uppm_ack", VerifierOutcome.passed(matched_at_ms=500, match_event_id="e1"))
        reg.apply("i1", "lppm_ack", VerifierOutcome.passed(matched_at_ms=1200, match_event_id="e2"))
        self.assertEqual(inst.stage, "accepted")

    def test_complete_verifier_transitions_to_complete(self):
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        reg.apply("i1", "res_from_lppm", VerifierOutcome.passed(matched_at_ms=8000, match_event_id="e3"))
        self.assertEqual(inst.stage, "complete")

    def test_complete_without_acks_still_complete(self):
        """RES received, both ACK verifiers still pending — instance is Complete.

        Stage is NOT a linear chain. Ack visibility is orthogonal to
        instance success (spec §4.4 partial-success rules).
        """
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        reg.apply("i1", "res_from_lppm", VerifierOutcome.passed(matched_at_ms=8000, match_event_id="e3"))
        self.assertEqual(inst.stage, "complete")
        self.assertEqual(inst.outcomes["uppm_ack"].state, "pending")

    def test_ack_only_any_received_transitions_to_complete(self):
        reg = VerifierRegistry()
        inst = _ack_only_instance()
        reg.register(inst)
        reg.apply("i_ack", "uppm_ack", VerifierOutcome.passed(matched_at_ms=500, match_event_id="e1"))
        self.assertEqual(inst.stage, "complete")

    def test_nack_transitions_to_failed(self):
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        reg.apply("i1", "nack_uppm", VerifierOutcome.passed(matched_at_ms=500, match_event_id="e-nack"))
        self.assertEqual(inst.stage, "failed")

    def test_nack_after_complete_downgrades_to_failed(self):
        """Late NACK overrides Complete."""
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        reg.apply("i1", "res_from_lppm", VerifierOutcome.passed(matched_at_ms=8000, match_event_id="e3"))
        self.assertEqual(inst.stage, "complete")
        reg.apply("i1", "nack_uppm", VerifierOutcome.passed(matched_at_ms=9000, match_event_id="e4"))
        self.assertEqual(inst.stage, "failed")

    def test_all_windows_expired_without_terminal_transitions_to_timed_out(self):
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        # Simulate the sweeper marking every verifier as window_expired.
        for v in inst.verifier_set.verifiers:
            reg.apply("i1", v.verifier_id, VerifierOutcome.window_expired())
        self.assertEqual(inst.stage, "timed_out")


class EmptyVerifierSetTerminalImmediately(unittest.TestCase):
    """Instances with no verifiers (FTDI / fixture missions) must be
    treated as Complete so lookup_open never returns them and admission
    isn't blocked forever."""

    def test_empty_set_is_complete(self):
        inst = CommandInstance(
            instance_id="i_empty",
            correlation_key=("ftdi_log", "FTDI"),
            t0_ms=0, cmd_event_id="c",
            verifier_set=VerifierSet(verifiers=()),
            outcomes={},
            stage="released",  # initial
        )
        reg = VerifierRegistry()
        reg.register(inst)
        # Stage transitions happen on apply(); for empty sets, there's
        # never an apply, so the register path should normally skip
        # registration entirely (see Task 16). Defensive: the derivation
        # would return 'complete' if called.
        from mav_gss_lib.platform.tx.verifiers import _derive_stage
        self.assertEqual(_derive_stage(inst), "complete")


class TerminalDropsFromOpen(unittest.TestCase):
    def test_complete_is_still_in_open_until_explicit_finalize(self):
        """apply() transitions stage but does not drop from open.

        The sweeper (Task 3) or explicit finalize() drops terminal
        instances once all windows have closed. Keeping terminal
        instances briefly open lets the UI show the final state before
        the row becomes a pure history entry.
        """
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        reg.apply("i1", "res_from_lppm", VerifierOutcome.passed(matched_at_ms=8000, match_event_id="e3"))
        self.assertEqual(inst.stage, "complete")
        self.assertEqual(len(reg.open_instances()), 1)

    def test_finalize_drops_terminals(self):
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        reg.apply("i1", "res_from_lppm", VerifierOutcome.passed(matched_at_ms=8000, match_event_id="e3"))
        reg.finalize_terminals()
        self.assertEqual(len(reg.open_instances()), 0)


class Sweeper(unittest.TestCase):
    def test_sweep_marks_expired_verifiers(self):
        """At t=11000ms, the uppm_ack window (0-10000ms) is expired."""
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        reg.sweep(now_ms=inst.t0_ms + 11000)
        self.assertEqual(inst.outcomes["uppm_ack"].state, "window_expired")
        # lppm_ack (0-15000ms) still pending.
        self.assertEqual(inst.outcomes["lppm_ack"].state, "pending")

    def test_sweep_preserves_passed_outcomes(self):
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        reg.apply("i1", "uppm_ack",
                  VerifierOutcome.passed(matched_at_ms=500, match_event_id="e1"))
        reg.sweep(now_ms=inst.t0_ms + 11000)
        self.assertEqual(inst.outcomes["uppm_ack"].state, "passed")

    def test_sweep_transitions_stage_on_last_expiry(self):
        """Every window expired → stage = timed_out."""
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        reg.sweep(now_ms=inst.t0_ms + 35000)  # past every stop_ms
        self.assertEqual(inst.stage, "timed_out")


class IsSettled(unittest.TestCase):
    def test_active_instance_is_not_settled(self):
        from mav_gss_lib.platform.tx.verifiers import is_settled
        inst = _instance()
        self.assertFalse(is_settled(inst))

    def test_terminal_with_pending_outcome_is_not_settled(self):
        from mav_gss_lib.platform.tx.verifiers import is_settled
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        reg.apply("i1", "res_from_lppm",
                  VerifierOutcome.passed(matched_at_ms=8000, match_event_id="e3"))
        self.assertEqual(inst.stage, "complete")
        # NACK + LPPM ack still pending → not settled.
        self.assertFalse(is_settled(inst))

    def test_terminal_with_all_outcomes_decided_is_settled(self):
        from mav_gss_lib.platform.tx.verifiers import is_settled
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        reg.apply("i1", "res_from_lppm",
                  VerifierOutcome.passed(matched_at_ms=8000, match_event_id="e3"))
        reg.apply("i1", "uppm_ack",
                  VerifierOutcome.passed(matched_at_ms=500, match_event_id="e1"))
        reg.apply("i1", "lppm_ack", VerifierOutcome.window_expired())
        reg.apply("i1", "nack_uppm", VerifierOutcome.window_expired())
        reg.apply("i1", "nack_lppm", VerifierOutcome.window_expired())
        self.assertTrue(is_settled(inst))


class FinalizeSettled(unittest.TestCase):
    def test_terminal_with_pending_verifier_does_not_finalize(self):
        """Late-NACK semantics: stage=complete but NACK still pending must
        stay open so a late NACK can override."""
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        # Pass the complete verifier; NACK + ACKs still pending.
        reg.apply("i1", "res_from_lppm",
                  VerifierOutcome.passed(matched_at_ms=8000, match_event_id="e3"))
        self.assertEqual(inst.stage, "complete")
        # finalize_settled should NOT pop because nack_uppm/nack_lppm
        # outcomes are still pending (windows still open).
        finalized = reg.finalize_settled()
        self.assertEqual(finalized, [])
        self.assertEqual(len(reg.open_instances()), 1)

    def test_all_settled_finalizes(self):
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        # Drive every verifier out of pending: pass complete + ack, expire NACKs/LPPM ack.
        reg.apply("i1", "res_from_lppm",
                  VerifierOutcome.passed(matched_at_ms=8000, match_event_id="e3"))
        reg.apply("i1", "uppm_ack",
                  VerifierOutcome.passed(matched_at_ms=500, match_event_id="e1"))
        reg.apply("i1", "lppm_ack", VerifierOutcome.window_expired())
        reg.apply("i1", "nack_uppm", VerifierOutcome.window_expired())
        reg.apply("i1", "nack_lppm", VerifierOutcome.window_expired())
        finalized = reg.finalize_settled()
        self.assertEqual(len(finalized), 1)
        self.assertEqual(finalized[0].instance_id, "i1")
        self.assertEqual(finalized[0].stage, "complete")
        self.assertEqual(reg.open_instances(), [])

    def test_sweep_then_settle_for_pure_timeout(self):
        """A command that gets nothing back: sweep expires every window,
        derive_stage returns timed_out, finalize_settled pops it."""
        reg = VerifierRegistry()
        inst = _instance()
        reg.register(inst)
        reg.sweep(now_ms=10**6)  # well past every check_window.stop_ms
        finalized = reg.finalize_settled()
        self.assertEqual(len(finalized), 1)
        self.assertEqual(finalized[0].stage, "timed_out")


if __name__ == "__main__":
    unittest.main()
