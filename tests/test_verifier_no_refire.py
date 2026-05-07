"""Regression: a verifier_id that already passed must not re-match when the
same instance receives another packet matching the same expected verifier."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from mav_gss_lib.missions.maveric.packets import match_verifiers
from mav_gss_lib.platform.tx.verifiers import (
    CheckWindow, CommandInstance, VerifierOutcome, VerifierSet, VerifierSpec,
)


def _envelope(cmd_id: str, src: str, ptype: str):
    return SimpleNamespace(
        mission_payload=SimpleNamespace(
            header={"cmd_id": cmd_id, "src": src, "ptype": ptype}
        )
    )


class TestNoRefire(unittest.TestCase):
    def test_complete_does_not_rematch_after_passed(self):
        spec = VerifierSpec(
            "file_from_astr", "complete", CheckWindow(0, 60_000),
            "FILE", "success",
        )
        inst = CommandInstance(
            instance_id="i1", correlation_key=("img_get_chunks", "ASTR"),
            t0_ms=0, cmd_event_id="cmd1",
            verifier_set=VerifierSet(verifiers=(spec,)),
            outcomes={"file_from_astr": VerifierOutcome.pending()},
            stage="released",
        )
        # First chunk arrives — should produce a transition.
        first = match_verifiers(
            _envelope("img_get_chunks", "ASTR", "FILE"),
            [inst], now_ms=1000, rx_event_id="rx1",
        )
        self.assertEqual(len(first), 1)
        # Simulate apply: outcome now passed.
        inst.outcomes["file_from_astr"] = VerifierOutcome.passed(
            matched_at_ms=1000, match_event_id="rx1",
        )
        # Second chunk for the same file — must NOT re-match.
        second = match_verifiers(
            _envelope("img_get_chunks", "ASTR", "FILE"),
            [inst], now_ms=2000, rx_event_id="rx2",
        )
        self.assertEqual(second, [])


class TestCorrelationKeyNormalization(unittest.TestCase):
    def test_numeric_and_symbolic_dest_produce_same_key(self):
        from mav_gss_lib.missions.maveric.declarative import _MaverCommandOpsWrapper
        # Construct a wrapper with just enough plumbing to call
        # correlation_key. _resolve_node_value walks self._codec, so
        # we mock it: integer 1 → "LPPM", everything else passthrough.
        class _StubCodec:
            def node_name_for(self, n):
                return {1: "LPPM", 0: "GS"}.get(n, str(n))
        w = _MaverCommandOpsWrapper.__new__(_MaverCommandOpsWrapper)
        w._codec = _StubCodec()
        from mav_gss_lib.platform.contract import EncodedCommand

        sym = EncodedCommand(raw=b"", cmd_id="mtq_set_1", src="GS",
                             mission_facts={"header": {"dest": "LPPM"}}, parameters=())
        num = EncodedCommand(raw=b"", cmd_id="mtq_set_1", src="GS",
                             mission_facts={"header": {"dest": 1}}, parameters=())
        self.assertEqual(w.correlation_key(sym), w.correlation_key(num))
        self.assertEqual(w.correlation_key(sym), ("mtq_set_1", "LPPM"))


class TestNoCrossInstanceMisattribution(unittest.TestCase):
    def test_late_response_only_matches_same_dest_instance(self):
        spec = VerifierSpec(
            "lppm_ack", "received", CheckWindow(0, 10_000),
            "LPPM", "info",
        )
        inst_lppm = CommandInstance(
            instance_id="i_lppm", correlation_key=("com_ping", "LPPM"),
            t0_ms=0, cmd_event_id="cmd1",
            verifier_set=VerifierSet(verifiers=(spec,)),
            outcomes={"lppm_ack": VerifierOutcome.pending()},
            stage="released",
        )
        inst_uppm = CommandInstance(
            instance_id="i_uppm", correlation_key=("com_ping", "UPPM"),
            t0_ms=1000, cmd_event_id="cmd2",  # newer
            verifier_set=VerifierSet(verifiers=(VerifierSpec(
                "uppm_ack", "received", CheckWindow(0, 10_000), "UPPM", "info",
            ),)),
            outcomes={"uppm_ack": VerifierOutcome.pending()},
            stage="released",
        )
        # ACK from LPPM — must attach to inst_lppm even though inst_uppm
        # is newer with the same cmd_id.
        result = match_verifiers(
            _envelope("com_ping", "LPPM", "ACK"),
            [inst_lppm, inst_uppm],
            now_ms=2000, rx_event_id="rx1",
        )
        self.assertEqual(len(result), 1)
        instance_id, _, _ = result[0]
        self.assertEqual(instance_id, "i_lppm")


class TestTlmAcrossSources(unittest.TestCase):
    """TLM emissions can originate from any subsystem, not just the
    instance's destination. The matcher's TLM branch filters by cmd_id
    only (no dest filter) — this regression test pins that semantic so a
    future "tighten everything to full-key" refactor doesn't silently
    break TLM matching.
    """

    def test_tlm_matches_when_src_differs_from_original_dest(self):
        # Instance for `eps_hk` originally sent to LPPM (the routing
        # peer). The TLM beacon for eps_hk arrives from EPS itself, not
        # LPPM — TLM must still match.
        spec = VerifierSpec(
            "tlm_eps_hk", "complete", CheckWindow(0, 60_000),
            "TLM", "success",
        )
        inst = CommandInstance(
            instance_id="i_eps", correlation_key=("eps_hk", "LPPM"),
            t0_ms=0, cmd_event_id="cmd1",
            verifier_set=VerifierSet(verifiers=(spec,)),
            outcomes={"tlm_eps_hk": VerifierOutcome.pending()},
            stage="released",
        )
        result = match_verifiers(
            _envelope("eps_hk", "EPS", "TLM"),  # src=EPS, original dest=LPPM
            [inst], now_ms=1000, rx_event_id="rx_tlm",
        )
        self.assertEqual(len(result), 1)
        instance_id, verifier_id, _ = result[0]
        self.assertEqual(instance_id, "i_eps")
        self.assertEqual(verifier_id, "tlm_eps_hk")


if __name__ == "__main__":
    unittest.main()
