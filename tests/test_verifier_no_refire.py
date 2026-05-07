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


if __name__ == "__main__":
    unittest.main()
