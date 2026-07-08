import threading
import unittest
from unittest.mock import MagicMock

from mav_gss_lib.platform.tracking.models import DopplerCorrection
from mav_gss_lib.server.tracking.service import (
    NullDopplerSink,
    TrackingService,
)


class _FakeRuntime:
    def __init__(self, control: dict | None = None) -> None:
        self.platform_cfg = {
            "tracking": {
                "selected_station_id": "usc",
                "stations": [
                    {"id": "usc", "name": "USC", "lat_deg": 34.02,
                     "lon_deg": -118.28, "alt_m": 70.0, "min_elevation_deg": 5.0}
                ],
                "tle": {
                    "source": "test",
                    "name": "MAVERIC",
                    "line1": "1 99999U 26001A   26182.53800926  .00000000  00000-0  15000-3 0  9999",
                    "line2": "2 99999  97.8250 154.7171 0058009 348.1000 351.9980 14.91466332000019",
                },
                "frequencies": {"rx_hz": 437_575_000.0, "tx_hz": 437_575_000.0},
                "display": {"day_night_map": True},
                "control": control or {
                    "rx_zmq_addr": "tcp://127.0.0.1:0",
                    "tx_zmq_addr": "tcp://127.0.0.1:0",
                    "tick_period_s": 1.0,
                },
            }
        }
        self.cfg_lock = threading.Lock()


class TrackingEngageTests(unittest.TestCase):
    def test_engage_swaps_in_active_sink_and_sets_mode(self) -> None:
        runtime = _FakeRuntime()
        active_sink = MagicMock()
        service = TrackingService(runtime, sink_factory=lambda **_: active_sink)

        mode = service.engage()

        self.assertEqual(mode, "connected")
        self.assertIs(service._sink, active_sink)
        self.assertEqual(service.doppler_mode, "connected")

    def test_engage_forwards_lo_offsets_from_control_config(self) -> None:
        runtime = _FakeRuntime(control={
            "rx_zmq_addr": "tcp://127.0.0.1:0",
            "tx_zmq_addr": "tcp://127.0.0.1:0",
            "tick_period_s": 1.0,
            "rx_lo_offset_hz": 250_000.0,
            "tx_lo_offset_hz": -400_000.0,
        })
        captured: dict = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        service = TrackingService(runtime, sink_factory=factory)
        service.engage()

        self.assertEqual(captured["rx_lo_offset_hz"], 250_000.0)
        self.assertEqual(captured["tx_lo_offset_hz"], -400_000.0)

    def test_disengage_restores_null_sink_and_closes_previous(self) -> None:
        runtime = _FakeRuntime()
        active_sink = MagicMock()
        service = TrackingService(runtime, sink_factory=lambda **_: active_sink)
        service.engage()

        mode = service.disengage()

        self.assertEqual(mode, "disconnected")
        self.assertIsInstance(service._sink, NullDopplerSink)
        active_sink.close.assert_called_once()

    def test_disengage_publishes_nominal_park_before_close(self) -> None:
        runtime = _FakeRuntime()
        active_sink = MagicMock()
        order: list[str] = []
        active_sink.publish.side_effect = lambda *_: order.append("publish")
        active_sink.close.side_effect = lambda: order.append("close")
        service = TrackingService(runtime, sink_factory=lambda **_: active_sink)
        service.engage()

        service.disengage()

        active_sink.publish.assert_called_once()
        self.assertEqual(order, ["publish", "close"])
        published: DopplerCorrection = active_sink.publish.call_args.args[0]
        self.assertEqual(published.mode, "disconnected")
        self.assertEqual(published.rx_tune_hz, 437_575_000.0)
        self.assertEqual(published.tx_tune_hz, 437_575_000.0)
        self.assertEqual(published.range_rate_mps, 0.0)

    def test_engage_failure_keeps_null_sink(self) -> None:
        runtime = _FakeRuntime()

        def boom(**_: object):
            raise RuntimeError("zmq bind failed")

        service = TrackingService(runtime, sink_factory=boom)

        with self.assertRaises(RuntimeError):
            service.engage()
        self.assertIsInstance(service._sink, NullDopplerSink)
        self.assertEqual(service.doppler_mode, "disconnected")

    def test_engage_is_idempotent(self) -> None:
        runtime = _FakeRuntime()
        calls: list[int] = []

        def factory(**_: object):
            calls.append(1)
            return MagicMock()

        service = TrackingService(runtime, sink_factory=factory)
        service.engage()
        service.engage()  # must not rebind
        self.assertEqual(len(calls), 1)
        self.assertEqual(service.doppler_mode, "connected")

    def test_disengage_when_already_disconnected_is_noop(self) -> None:
        runtime = _FakeRuntime()
        service = TrackingService(runtime)
        self.assertEqual(service.disengage(), "disconnected")

    def test_doppler_publishes_when_connected(self) -> None:
        runtime = _FakeRuntime()
        active_sink = MagicMock()
        service = TrackingService(runtime, sink_factory=lambda **_: active_sink)
        service.engage()

        result = service.doppler(time_ms=1_700_000_000_000)

        self.assertEqual(result["mode"], "connected")
        active_sink.publish.assert_called_once()
        published: DopplerCorrection = active_sink.publish.call_args.args[0]
        self.assertEqual(published.station_id, "usc")

    def test_engage_disengage_emit_tracking_log_events(self) -> None:
        runtime = _FakeRuntime()
        log = MagicMock()
        runtime.rx = MagicMock(log=log)
        active_sink = MagicMock()
        service = TrackingService(runtime, sink_factory=lambda **_: active_sink)

        service.engage()
        service.disengage()

        calls = log.write_tracking_event.call_args_list
        self.assertEqual(len(calls), 2)

        connect_args = calls[0].kwargs
        self.assertEqual(calls[0].args, ("connect",))
        self.assertEqual(connect_args["mode"], "connected")
        self.assertEqual(connect_args["prev_mode"], "disconnected")
        self.assertEqual(connect_args["station_id"], "usc")
        self.assertEqual(connect_args["rx_zmq_addr"], "tcp://127.0.0.1:0")
        self.assertEqual(connect_args["tx_zmq_addr"], "tcp://127.0.0.1:0")

        disconnect_args = calls[1].kwargs
        self.assertEqual(calls[1].args, ("disconnect",))
        self.assertEqual(disconnect_args["mode"], "disconnected")
        self.assertEqual(disconnect_args["prev_mode"], "connected")
        self.assertEqual(disconnect_args["station_id"], "usc")

    def test_idempotent_engage_does_not_duplicate_log_event(self) -> None:
        runtime = _FakeRuntime()
        log = MagicMock()
        runtime.rx = MagicMock(log=log)
        service = TrackingService(runtime, sink_factory=lambda **_: MagicMock())

        service.engage()
        service.engage()  # second engage is a no-op
        service.disengage()
        service.disengage()  # second disengage is a no-op

        actions = [c.args[0] for c in log.write_tracking_event.call_args_list]
        self.assertEqual(actions, ["connect", "disconnect"])

    def test_log_write_failure_does_not_break_engage(self) -> None:
        runtime = _FakeRuntime()
        log = MagicMock()
        log.write_tracking_event.side_effect = RuntimeError("disk full")
        runtime.rx = MagicMock(log=log)
        service = TrackingService(runtime, sink_factory=lambda **_: MagicMock())

        # Must not raise — engage/disengage are best-effort about logging.
        self.assertEqual(service.engage(), "connected")
        self.assertEqual(service.disengage(), "disconnected")


if __name__ == "__main__":
    unittest.main()
