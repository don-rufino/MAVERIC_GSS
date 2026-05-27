import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

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


if __name__ == "__main__":
    unittest.main()
