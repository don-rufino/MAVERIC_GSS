"""TxService._log_tx_tracking_sample: TX attempts get their own
tracking_sample row (source="tx_attempt") from the last-published Doppler
tick, without recomputing or re-publishing a fresh tune command."""
import unittest
from unittest.mock import MagicMock

from mav_gss_lib.server.tx.service import TxService


def _runtime_with_latest(doppler: dict | None):
    r = MagicMock()
    r.platform_cfg = {"tx": {"delay_ms": 100, "verifiers_enabled": True},
                       "general": {"log_dir": "/tmp"}}
    r.doppler_broadcaster.latest = (
        {"type": "doppler", "doppler": doppler} if doppler is not None else None
    )
    return TxService(r)


class TxTrackingSampleTests(unittest.TestCase):
    def test_logs_sample_from_latest_broadcast(self):
        doppler = {"mode": "connected", "elevation_deg": 12.3, "rx_tune_hz": 437_583_900.0}
        tx = _runtime_with_latest(doppler)
        tx.log = MagicMock()

        tx._log_tx_tracking_sample()

        tx.log.write_tracking_sample.assert_called_once_with(doppler, source="tx_attempt")

    def test_does_not_recompute_or_republish(self):
        """Reads the broadcaster's cached latest value; never touches
        runtime.tracking (which would trigger a fresh propagation + an
        extra ZMQ tune publish as a side effect)."""
        doppler = {"mode": "connected"}
        tx = _runtime_with_latest(doppler)
        tx.log = MagicMock()

        tx._log_tx_tracking_sample()

        tx.runtime.tracking.doppler.assert_not_called()

    def test_no_broadcast_yet_is_a_noop(self):
        tx = _runtime_with_latest(None)
        tx.log = MagicMock()

        tx._log_tx_tracking_sample()

        tx.log.write_tracking_sample.assert_not_called()

    def test_no_log_configured_is_a_noop(self):
        doppler = {"mode": "connected"}
        tx = _runtime_with_latest(doppler)
        tx.log = None  # RadioService/app not yet wired a SessionLog

        tx._log_tx_tracking_sample()  # must not raise


if __name__ == "__main__":
    unittest.main()
