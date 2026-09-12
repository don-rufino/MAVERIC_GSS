import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from mav_gss_lib.platform.tracking import TrackingError
from mav_gss_lib.server.radio.service import RadioService
from mav_gss_lib.server.tracking.service import TrackingService


def _runtime() -> SimpleNamespace:
    runtime = SimpleNamespace()
    runtime.platform_cfg = {
        "radio": {"enabled": True, "autostart": False,
                  "script": "gnuradio/MAV_DUO.py", "log_lines": 100},
        "tracking": {
            "selected_station_id": "usc",
            "stations": [{"id": "usc", "name": "USC", "lat_deg": 34.02,
                          "lon_deg": -118.28, "alt_m": 70.0,
                          "min_elevation_deg": 5.0}],
            "tle": {"source": "test", "name": "MAVERIC",
                    "line1": "1 99999U 26001A   26182.53800926  .00000000  00000-0  15000-3 0  9999",
                    "line2": "2 99999  97.8250 154.7171 0058009 348.1000 351.9980 14.91466332000019"},
            "frequencies": {"rx_hz": 437_575_000.0, "tx_hz": 437_575_000.0},
            "display": {"day_night_map": True},
            "control": {"rx_zmq_addr": "tcp://127.0.0.1:0",
                        "tx_zmq_addr": "tcp://127.0.0.1:0",
                        "tick_period_s": 1.0},
        },
    }
    runtime.cfg_lock = threading.Lock()
    runtime.rx = SimpleNamespace(log=None)
    runtime.tx = SimpleNamespace(log=None)
    runtime.mission_id = "maveric"
    return runtime


class RadioDopplerLifecycleTests(unittest.TestCase):
    def test_radio_exit_disengages_doppler(self) -> None:
        runtime = _runtime()
        sink = MagicMock()
        runtime.tracking = TrackingService(runtime, sink_factory=lambda **_: sink)
        runtime.radio = RadioService(runtime)
        runtime.radio.add_exit_callback(runtime.tracking.disengage)

        runtime.tracking.engage()
        self.assertEqual(runtime.tracking.doppler_mode, "connected")

        # Simulate process exit
        fake_proc = SimpleNamespace(poll=lambda: 0, wait=lambda: 0)
        runtime.radio.proc = fake_proc
        runtime.radio.started_at = 0.0
        runtime.radio._waiter(fake_proc)

        self.assertEqual(runtime.tracking.doppler_mode, "disconnected")
        sink.close.assert_called_once()


class StaticModeTests(unittest.TestCase):
    def test_static_mode_parks_at_nominal_without_tle_math(self) -> None:
        runtime = _runtime()
        sink = MagicMock()
        runtime.tracking = TrackingService(runtime, sink_factory=lambda **_: sink)

        mode = runtime.tracking.set_static_mode(True)
        self.assertEqual(mode, "static")
        self.assertEqual(runtime.tracking.doppler_mode, "static")

        result = runtime.tracking.doppler()
        self.assertEqual(result["mode"], "static")
        self.assertEqual(result["tx_hz"], 437_575_000.0)
        self.assertEqual(result["tx_tune_hz"], 437_575_000.0)
        self.assertEqual(result["rx_shift_hz"], 0.0)
        # Static mode never touches the sink — nothing should have tuned the radio.
        sink.publish.assert_not_called()

    def test_engage_refuses_while_static(self) -> None:
        runtime = _runtime()
        runtime.tracking = TrackingService(runtime, sink_factory=lambda **_: MagicMock())
        runtime.tracking.set_static_mode(True)

        with self.assertRaises(TrackingError):
            runtime.tracking.engage()
        self.assertEqual(runtime.tracking.doppler_mode, "static")

    def test_static_mode_refuses_while_engaged(self) -> None:
        runtime = _runtime()
        runtime.tracking = TrackingService(runtime, sink_factory=lambda **_: MagicMock())
        runtime.tracking.engage()

        with self.assertRaises(TrackingError):
            runtime.tracking.set_static_mode(True)
        self.assertEqual(runtime.tracking.doppler_mode, "connected")

    def test_static_mode_off_returns_to_disconnected(self) -> None:
        runtime = _runtime()
        runtime.tracking = TrackingService(runtime, sink_factory=lambda **_: MagicMock())
        runtime.tracking.set_static_mode(True)

        mode = runtime.tracking.set_static_mode(False)
        self.assertEqual(mode, "disconnected")
        self.assertEqual(runtime.tracking.doppler_mode, "disconnected")

    def test_static_mode_is_idempotent(self) -> None:
        runtime = _runtime()
        runtime.tracking = TrackingService(runtime, sink_factory=lambda **_: MagicMock())

        self.assertEqual(runtime.tracking.set_static_mode(True), "static")
        self.assertEqual(runtime.tracking.set_static_mode(True), "static")
        self.assertEqual(runtime.tracking.set_static_mode(False), "disconnected")
        self.assertEqual(runtime.tracking.set_static_mode(False), "disconnected")


def _with_offset_sweep_control(runtime: SimpleNamespace, **overrides) -> None:
    control = runtime.platform_cfg["tracking"]["control"]
    control.update({
        "offset_sweep_base_hz": 0.0,
        "offset_sweep_step_hz": 50.0,
        "offset_sweep_max_deviation_hz": 150.0,
        **overrides,
    })


class OffsetSweepTests(unittest.TestCase):
    def test_enable_refuses_for_non_maveric_mission(self) -> None:
        runtime = _runtime()
        runtime.mission_id = "astrocast"
        _with_offset_sweep_control(runtime)
        runtime.tracking = TrackingService(runtime, sink_factory=lambda **_: MagicMock())

        with self.assertRaises(TrackingError):
            runtime.tracking.set_offset_sweep_enabled(True)
        self.assertFalse(runtime.tracking.offset_sweep_enabled)

    def test_enable_succeeds_for_maveric_and_reads_config(self) -> None:
        runtime = _runtime()
        _with_offset_sweep_control(runtime)
        runtime.tracking = TrackingService(runtime, sink_factory=lambda **_: MagicMock())

        self.assertTrue(runtime.tracking.set_offset_sweep_enabled(True))
        self.assertTrue(runtime.tracking.offset_sweep_enabled)

    def test_disable_is_idempotent(self) -> None:
        runtime = _runtime()
        _with_offset_sweep_control(runtime)
        runtime.tracking = TrackingService(runtime, sink_factory=lambda **_: MagicMock())

        self.assertFalse(runtime.tracking.set_offset_sweep_enabled(False))
        self.assertFalse(runtime.tracking.set_offset_sweep_enabled(False))

    def test_status_reports_offset_sweep_enabled(self) -> None:
        runtime = _runtime()
        _with_offset_sweep_control(runtime)
        runtime.tracking = TrackingService(runtime, sink_factory=lambda **_: MagicMock())

        self.assertFalse(runtime.tracking.status()["offset_sweep_enabled"])
        runtime.tracking.set_offset_sweep_enabled(True)
        self.assertTrue(runtime.tracking.status()["offset_sweep_enabled"])

    def test_doppler_applies_offset_to_published_correction_and_result(self) -> None:
        runtime = _runtime()
        _with_offset_sweep_control(runtime)
        sink = MagicMock()
        runtime.tracking = TrackingService(runtime, sink_factory=lambda **_: sink)
        runtime.tracking.set_offset_sweep_enabled(True)
        runtime.tracking.engage()
        sink.reset_mock()

        result = runtime.tracking.doppler()
        self.assertEqual(result["tx_offset_step_hz"], 0.0)  # sequence starts at base=0.0

        published_correction = sink.publish.call_args[0][0]
        self.assertEqual(published_correction.tx_tune_hz, result["tx_tune_hz"])

        offset_hz = runtime.tracking.advance_offset_sweep()
        self.assertEqual(offset_hz, 50.0)
        # advance_offset_sweep() must have already republished with the new
        # offset — a caller sending a command right after this must not race
        # a stale tune sitting in the flowgraph for up to a full tick period.
        # assertAlmostEqual, not assertEqual: each doppler() call re-samples
        # the satellite's range-rate at the current wall-clock instant, so
        # two calls a fraction of a millisecond apart carry a tiny natural
        # Doppler-shift drift on top of the exact +50.0 Hz offset step.
        second_published = sink.publish.call_args[0][0]
        self.assertAlmostEqual(
            second_published.tx_tune_hz - published_correction.tx_tune_hz, 50.0, delta=1.0,
        )

    def test_static_mode_never_reports_nonzero_offset_step(self) -> None:
        runtime = _runtime()
        _with_offset_sweep_control(runtime, offset_sweep_base_hz=50.0)
        runtime.tracking = TrackingService(runtime, sink_factory=lambda **_: MagicMock())
        runtime.tracking.set_offset_sweep_enabled(True)
        runtime.tracking.set_static_mode(True)

        result = runtime.tracking.doppler()
        self.assertEqual(result["mode"], "static")
        self.assertEqual(result["tx_offset_step_hz"], 0.0)

    def test_advance_offset_sweep_is_noop_when_disabled(self) -> None:
        runtime = _runtime()
        _with_offset_sweep_control(runtime)
        runtime.tracking = TrackingService(runtime, sink_factory=lambda **_: MagicMock())

        self.assertEqual(runtime.tracking.advance_offset_sweep(), 0.0)


if __name__ == "__main__":
    unittest.main()
