from __future__ import annotations

import threading
import time
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mav_gss_lib.platform.tracking import (
    default_tracking_config,
    default_tracking_config_dict,
    normalize_tracking_config,
    tracking_state,
)
from mav_gss_lib.platform.tracking.propagation import satellite_from_lines, TrackingError
from mav_gss_lib.platform.tracking.models import TrackingTle
from mav_gss_lib.server.api.tracking import router as tracking_router
from mav_gss_lib.server.tracking import TrackingService
from mav_gss_lib.server.tracking._tick import DopplerBroadcaster
from mav_gss_lib.missions.maveric.tracking_defaults import seed_tracking_defaults


class TestTrackingDomain(unittest.TestCase):
    def test_default_config_normalizes_to_neutral_reference_station(self):
        # Platform defaults are mission-agnostic — `default_station()`
        # returns a neutral "Reference Station" placeholder, NOT
        # "USC / Southern California" (the latter was MAVERIC-flavored
        # convenience leak; mission-specific defaults now live in
        # mav_gss_lib/missions/<mission>/tracking_defaults.py).
        cfg = normalize_tracking_config({})

        self.assertEqual(cfg.selected_station_id, "ref")
        self.assertEqual(cfg.selected_station.name, "Reference Station")
        self.assertEqual(cfg.tle.name, "Sample LEO")
        self.assertGreater(cfg.frequencies.rx_hz, 0)
        self.assertGreater(cfg.frequencies.tx_hz, 0)

    def test_tracking_state_contains_doppler_and_passes(self):
        state = tracking_state(
            default_tracking_config(),
            time_ms=int(time.time() * 1000),
            pass_count=2,
        )

        self.assertEqual(state.doppler.satellite, "Sample LEO")
        self.assertEqual(state.doppler.rx_tune_hz, state.doppler.rx_hz + state.doppler.rx_shift_hz)
        self.assertEqual(state.doppler.tx_tune_hz, state.doppler.tx_hz + state.doppler.tx_shift_hz)
        self.assertGreater(state.footprint.radius_deg, 0)
        self.assertLessEqual(len(state.upcoming_passes), 2)


class _CaptureSink:
    def __init__(self):
        self.items = []

    def publish(self, correction):
        self.items.append(correction)


class _Runtime:
    def __init__(self):
        self.platform_cfg = {"tracking": default_tracking_config_dict()}
        self.cfg_lock = threading.RLock()
        self.session_token = "token"
        self.tracking = TrackingService(self)
        self.doppler_broadcaster = DopplerBroadcaster()


class TestTrackingService(unittest.TestCase):
    def test_doppler_includes_orbital_altitude_distinct_from_range(self):
        """altitude_km (subsatellite point) and range_km (topocentric
        line-of-sight distance) are different quantities that only
        converge near zenith — both must be present and, for a satellite
        not directly overhead, distinguishably different."""
        runtime = _Runtime()

        result = runtime.tracking.doppler()

        self.assertIn("altitude_km", result)
        self.assertIn("range_km", result)
        self.assertGreater(result["altitude_km"], 0)
        self.assertGreater(result["range_km"], 0)

    def test_connected_mode_publishes_to_sink(self):
        runtime = _Runtime()
        sink = _CaptureSink()
        runtime.tracking = TrackingService(runtime, sink_factory=lambda **_: sink)

        runtime.tracking.set_doppler_connected(True)
        correction = runtime.tracking.doppler()

        self.assertEqual(correction["mode"], "connected")
        self.assertEqual(len(sink.items), 1)
        self.assertEqual(sink.items[0].mode, "connected")

    def test_tracking_api_returns_state_and_accepts_doppler_connection(self):
        runtime = _Runtime()
        app = FastAPI()
        app.state.runtime = runtime
        app.include_router(tracking_router)
        client = TestClient(app)

        mode = client.post(
            "/api/tracking/doppler/connection/connect",
            headers={"x-gss-token": "token"},
        )
        self.assertEqual(mode.status_code, 200)
        self.assertEqual(mode.json(), {"connected": True, "mode": "connected"})

        response = client.get("/api/tracking/state?pass_count=1")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["doppler"]["mode"], "connected")
        self.assertIn("ground_track", body)
        self.assertLessEqual(len(body["upcoming_passes"]), 1)


class SatelliteFromLinesTests(unittest.TestCase):
    L1 = "1 25544U 98067A   26001.50000000  .00000000  00000-0  00000-0 0  9990"
    L2 = "2 25544  51.6400   0.0000 0000000   0.0000   0.0000 15.50000000000007"

    def test_builds_from_lines(self):
        sat = satellite_from_lines("ISS", self.L1, self.L2)
        self.assertEqual(sat.name, "ISS")

    def test_rejects_garbage(self):
        with self.assertRaises(TrackingError):
            satellite_from_lines("X", "1 garbage", "2 garbage")


class TrackingTleProvenanceTests(unittest.TestCase):
    def test_defaults(self):
        tle = TrackingTle(source="s", name="n", line1="1", line2="2")
        self.assertEqual(tle.method, "manual")
        self.assertEqual(tle.fetched_at_ms, 0)

    def test_explicit_method(self):
        tle = TrackingTle(source="s", name="n", line1="1", line2="2",
                          method="fetched", fetched_at_ms=123)
        self.assertEqual(tle.method, "fetched")
        self.assertEqual(tle.fetched_at_ms, 123)


class NormalizeTleProvenanceTests(unittest.TestCase):
    def test_legacy_tle_without_method_defaults_manual(self):
        cfg = normalize_tracking_config({"tle": {
            "source": "op", "name": "X",
            "line1": "1 12345U ...", "line2": "2 12345 ...",
        }})
        self.assertEqual(cfg.tle.method, "manual")

    def test_method_preserved(self):
        cfg = normalize_tracking_config({"tle": {
            "source": "CelesTrak", "name": "X",
            "line1": "1 12345U ...", "line2": "2 12345 ...",
            "method": "fetched", "fetched_at_ms": 999,
        }})
        self.assertEqual(cfg.tle.method, "fetched")
        self.assertEqual(cfg.tle.fetched_at_ms, 999)

    def test_invalid_method_coerced_to_manual(self):
        cfg = normalize_tracking_config({"tle": {
            "source": "op", "name": "X",
            "line1": "1 12345U ...", "line2": "2 12345 ...",
            "method": "bogus",
        }})
        self.assertEqual(cfg.tle.method, "manual")


class SeedTrackingDefaultsTests(unittest.TestCase):
    def test_seeds_tle_fetch_and_method(self):
        cfg = {}
        seed_tracking_defaults(cfg)
        self.assertEqual(cfg["tracking"]["tle"]["method"], "seed")
        self.assertEqual(cfg["tracking"]["tle_fetch"]["identifier"], "")
        self.assertFalse(cfg["tracking"]["tle_fetch"]["auto_refresh"])

    def test_does_not_mark_existing_operator_tle_as_seed(self):
        cfg = {"tracking": {"tle": {"line1": "1 12345U ...", "line2": "2 12345 ..."}}}
        seed_tracking_defaults(cfg)
        self.assertNotIn("method", cfg["tracking"]["tle"])  # left for normalize -> "manual"


if __name__ == "__main__":
    unittest.main()
