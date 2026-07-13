"""Verifier-toggle behavior at the RX projection boundary."""

from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from mav_gss_lib.platform.tx.verifiers import (
    CheckWindow,
    CommandInstance,
    VerifierOutcome,
    VerifierRegistry,
    VerifierSet,
    VerifierSpec,
)
from mav_gss_lib.server.rx.projections import RxProjectionDeps, _apply_verifier_matches


class TestRxVerifierToggle(unittest.TestCase):
    def test_matching_is_suppressed_while_verifiers_are_disabled(self):
        registry = VerifierRegistry()
        verifier_set = VerifierSet(verifiers=(
            VerifierSpec(
                "uppm_ack", "received", CheckWindow(0, 30_000),
                "UPPM", "info",
            ),
        ))
        instance = CommandInstance(
            instance_id="old-command",
            correlation_key=("com_ping", "LPPM"),
            t0_ms=1_000_000,
            cmd_event_id="old-event",
            verifier_set=verifier_set,
            outcomes={"uppm_ack": VerifierOutcome.pending()},
            stage="released",
        )
        registry.register(instance)
        registry.consume_dirty()

        matcher = MagicMock(side_effect=AssertionError(
            "mission verifier matching must not run while disabled",
        ))
        tx_log = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = SimpleNamespace(
                tx_verifiers_enabled=False,
                mission=SimpleNamespace(
                    packets=SimpleNamespace(match_verifiers=matcher),
                ),
                platform=SimpleNamespace(verifiers=registry),
                log_dir=tmp,
            )
            deps = RxProjectionDeps(
                runtime=runtime,
                last_arrival_ms={},
                crc_window=[],
                dup_window=[],
                get_rx_log=lambda: None,
                get_tx_log=lambda: tx_log,
            )

            changed = _apply_verifier_matches(
                deps,
                SimpleNamespace(seq=7),
                now_ms=1_001_000,
                event_id="rx-from-unverified-spam",
            )

        matcher.assert_not_called()
        tx_log.write_cmd_verifier.assert_not_called()
        self.assertEqual(changed, [])
        self.assertEqual(instance.outcomes["uppm_ack"].state, "pending")
