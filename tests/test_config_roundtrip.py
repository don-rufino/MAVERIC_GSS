"""Round-trip tests for mav_gss_lib.config load/save (native split shape)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mav_gss_lib import config as cfg_mod


class TestConfigRoundTrip(unittest.TestCase):
    def test_load_missing_file_returns_split_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            missing = os.path.join(td, "does-not-exist.yml")
            platform_cfg, mission_id, mission_cfg = cfg_mod.load_split_config(missing)
            self.assertEqual(platform_cfg["tx"]["zmq_addr"], "tcp://127.0.0.1:52002")
            self.assertIs(platform_cfg["tx"]["verifiers_enabled"], True)
            self.assertEqual(platform_cfg["rx"]["zmq_addr"], "tcp://127.0.0.1:52001")
            self.assertEqual(mission_id, "maveric")
            self.assertEqual(mission_cfg, {})

    def test_verifiers_enabled_false_round_trips(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "gss.yml")
            initial = {
                "platform": {"tx": {"verifiers_enabled": False}},
                "mission": {"id": "maveric", "config": {}},
            }
            cfg_mod.save_operator_config(initial, path)

            platform_cfg, mission_id, mission_cfg = cfg_mod.load_split_config(path)
            self.assertIs(platform_cfg["tx"]["verifiers_enabled"], False)

            native = cfg_mod.split_to_persistable(platform_cfg, mission_id, mission_cfg)
            self.assertIs(native["platform"]["tx"]["verifiers_enabled"], False)

    def test_user_value_overrides_default(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "gss.yml")
            initial = {
                "platform": {
                    "tx": {"zmq_addr": "tcp://127.0.0.1:59999"},
                    "rx": {"tx_blackout_ms": 250},
                },
                "mission": {"id": "maveric", "config": {}},
            }
            cfg_mod.save_operator_config(initial, path)

            platform_cfg, mission_id, _ = cfg_mod.load_split_config(path)
            self.assertEqual(platform_cfg["tx"]["zmq_addr"], "tcp://127.0.0.1:59999")
            self.assertEqual(platform_cfg["rx"]["tx_blackout_ms"], 250)
            self.assertEqual(mission_id, "maveric")

    def test_round_trip_preserves_native_split_shape(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "gss.yml")
            value = {
                "platform": {
                    "tx": {"zmq_addr": "tcp://x:1", "delay_ms": 100},
                    "rx": {"zmq_addr": "tcp://y:2", "tx_blackout_ms": 5},
                    "general": {"log_dir": "logs-test"},
                },
                "mission": {
                    "id": "echo_v2",
                    "config": {"csp": {"source": 6}},
                },
            }
            cfg_mod.save_operator_config(value, path)
            platform_cfg, mission_id, mission_cfg = cfg_mod.load_split_config(path)
            self.assertEqual(platform_cfg["tx"]["zmq_addr"], "tcp://x:1")
            self.assertEqual(platform_cfg["tx"]["delay_ms"], 100)
            self.assertEqual(platform_cfg["rx"]["zmq_addr"], "tcp://y:2")
            self.assertEqual(platform_cfg["rx"]["tx_blackout_ms"], 5)
            self.assertEqual(platform_cfg["general"]["log_dir"], "logs-test")
            self.assertEqual(mission_id, "echo_v2")
            self.assertEqual(mission_cfg["csp"]["source"], 6)

    def test_save_is_atomic_replace(self):
        """Partial write scenario — temp file shouldn't leak into real path."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "gss.yml")
            good = {"platform": {}, "mission": {"id": "maveric", "config": {}}}
            cfg_mod.save_operator_config(good, path)
            entries = os.listdir(td)
            self.assertIn("gss.yml", entries)
            for name in entries:
                self.assertFalse(name.endswith(".tmp"), f"leftover temp file: {name}")

    def test_partial_platform_overrides_deep_merge_with_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "gss.yml")
            partial = {
                "platform": {"tx": {"delay_ms": 250}},
                "mission": {"id": "maveric", "config": {}},
            }
            cfg_mod.save_operator_config(partial, path)
            platform_cfg, _, _ = cfg_mod.load_split_config(path)
            self.assertEqual(platform_cfg["tx"]["delay_ms"], 250)
            self.assertEqual(platform_cfg["tx"]["zmq_addr"], "tcp://127.0.0.1:52002")
            self.assertEqual(platform_cfg["rx"]["zmq_addr"], "tcp://127.0.0.1:52001")

    def test_legacy_radio_stop_timeout_migrates_for_waterfall_render(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "gss.yml")
            value = {
                "platform": {"radio": {"stop_timeout_s": 8.0}},
                "mission": {"id": "astrocast", "config": {}},
            }
            cfg_mod.save_operator_config(value, path)

            platform_cfg, _, _ = cfg_mod.load_split_config(path)

            self.assertEqual(platform_cfg["radio"]["stop_timeout_s"], 30.0)

    def test_flat_file_on_disk_is_rejected(self):
        """Operator gss.yml files must use the native split shape."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "gss.yml")
            flat = {
                "tx": {"zmq_addr": "tcp://x:1", "delay_ms": 250},
                "general": {"mission": "maveric", "log_dir": "native-logs"},
                "csp": {"priority": 3, "source": 6},
            }
            cfg_mod.save_operator_config(flat, path)
            with self.assertRaises(ValueError):
                cfg_mod.load_split_config(path)

    def test_non_mapping_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "gss.yml")
            with open(path, "w") as handle:
                handle.write("- not\n- a\n- mapping\n")

            with self.assertRaises(ValueError):
                cfg_mod.load_split_config(path)

    def test_mixed_native_and_flat_file_is_rejected(self):
        """Old top-level fragments must not be silently ignored."""
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "gss.yml")
            mixed = {
                "platform": {"tx": {"delay_ms": 250}},
                "mission": {"id": "maveric", "config": {}},
                "csp": {"priority": 3, "source": 6},
            }
            cfg_mod.save_operator_config(mixed, path)
            with self.assertRaises(ValueError):
                cfg_mod.load_split_config(path)


if __name__ == "__main__":
    unittest.main()
