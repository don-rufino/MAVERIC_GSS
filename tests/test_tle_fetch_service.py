"""TleFetchService: preview (no persist), refresh+persist, precedence, status."""
from __future__ import annotations

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mav_gss_lib.platform.tracking.fetch import FetchResult  # noqa: E402
from mav_gss_lib.server.tracking.fetch_service import TleFetchService  # noqa: E402

ISS_L1 = "1 25544U 98067A   26001.50000000  .00000000  00000-0  00000-0 0  9990"
ISS_L2 = "2 25544  51.6400   0.0000 0000000   0.0000   0.0000 15.50000000000007"


class _FakeRuntime:
    def __init__(self, tracking_cfg):
        self.platform_cfg = {"tracking": tracking_cfg}
        self.mission_id = "maveric"
        self.mission_cfg = {}
        self.mission = None
        self.cfg_lock = threading.Lock()
        self.config_save_disabled = True   # never touch real gss.yml in tests
        self.saved = []


def _good_result():
    return FetchResult(ok=True, via="celestrak", name="MAVERIC",
                       line1=ISS_L1, line2=ISS_L2, tle_epoch_ms=1767225600000)


class TleFetchServiceTests(unittest.TestCase):
    def _svc(self, cfg, result):
        rt = _FakeRuntime(cfg)
        svc = TleFetchService(rt, fetch_fn=lambda settings, now_ms: result)
        return rt, svc

    def test_preview_does_not_persist(self):
        cfg = {"tle": {"line1": "x", "line2": "y", "method": "seed"},
               "tle_fetch": {"identifier": "25544"}}
        rt, svc = self._svc(cfg, _good_result())
        out = svc.fetch_preview()
        self.assertTrue(out["ok"])
        self.assertEqual(out["line1"], ISS_L1)
        self.assertEqual(rt.platform_cfg["tracking"]["tle"]["line1"], "x")

    def test_refresh_persists_and_marks_fetched(self):
        cfg = {"tle": {"line1": "x", "line2": "y", "method": "seed"},
               "tle_fetch": {"identifier": "25544"}}
        rt, svc = self._svc(cfg, _good_result())
        out = svc.refresh_and_persist()
        self.assertTrue(out["ok"])
        tle = rt.platform_cfg["tracking"]["tle"]
        self.assertEqual(tle["line1"], ISS_L1)
        self.assertEqual(tle["method"], "fetched")
        self.assertGreater(tle["fetched_at_ms"], 0)

    def test_refresh_skips_manual(self):
        cfg = {"tle": {"line1": "hand", "line2": "edit", "method": "manual"},
               "tle_fetch": {"identifier": "25544"}}
        rt, svc = self._svc(cfg, _good_result())
        out = svc.refresh_and_persist()
        self.assertFalse(out["ok"])
        self.assertIn("manual", out["detail"].lower())
        self.assertEqual(rt.platform_cfg["tracking"]["tle"]["line1"], "hand")

    def test_bad_fetch_leaves_config(self):
        cfg = {"tle": {"line1": "x", "line2": "y", "method": "seed"},
               "tle_fetch": {"identifier": "25544"}}
        rt, svc = self._svc(cfg, FetchResult(ok=False, detail="no usable TLE"))
        out = svc.refresh_and_persist()
        self.assertFalse(out["ok"])
        self.assertEqual(rt.platform_cfg["tracking"]["tle"]["line1"], "x")
