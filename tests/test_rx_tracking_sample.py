"""RxProjectionRunner._log_rx_tracking_sample: RX decodes get their own
tracking_sample row (source="rx_decode") from the last-published Doppler
tick, mirroring TxService._log_tx_tracking_sample — without recomputing or
re-publishing a fresh tune command."""
import unittest
from unittest.mock import MagicMock

from mav_gss_lib.server.rx.projections import RxProjectionDeps, _log_rx_tracking_sample


def _deps_with_latest(doppler: dict | None, *, log: object | None) -> RxProjectionDeps:
    runtime = MagicMock()
    runtime.doppler_broadcaster.latest = (
        {"type": "doppler", "doppler": doppler} if doppler is not None else None
    )
    return RxProjectionDeps(
        runtime=runtime,
        last_arrival_ms={},
        crc_window=MagicMock(),
        dup_window=MagicMock(),
        get_rx_log=lambda: log,
        get_tx_log=lambda: MagicMock(),
    )


class RxTrackingSampleTests(unittest.TestCase):
    def test_logs_sample_from_latest_broadcast(self):
        doppler = {"mode": "connected", "elevation_deg": 15.2, "rx_tune_hz": 437_259_756.0}
        log = MagicMock()
        deps = _deps_with_latest(doppler, log=log)

        _log_rx_tracking_sample(deps)

        log.write_tracking_sample.assert_called_once_with(doppler, source="rx_decode")

    def test_does_not_recompute_or_republish(self):
        """Reads the broadcaster's cached latest value; never touches
        runtime.tracking (which would trigger a fresh propagation + an
        extra ZMQ tune publish as a side effect)."""
        doppler = {"mode": "connected"}
        log = MagicMock()
        deps = _deps_with_latest(doppler, log=log)

        _log_rx_tracking_sample(deps)

        deps.runtime.tracking.doppler.assert_not_called()

    def test_no_broadcast_yet_is_a_noop(self):
        log = MagicMock()
        deps = _deps_with_latest(None, log=log)

        _log_rx_tracking_sample(deps)

        log.write_tracking_sample.assert_not_called()

    def test_no_log_configured_is_a_noop(self):
        doppler = {"mode": "connected"}
        deps = _deps_with_latest(doppler, log=None)

        _log_rx_tracking_sample(deps)  # must not raise


if __name__ == "__main__":
    unittest.main()
