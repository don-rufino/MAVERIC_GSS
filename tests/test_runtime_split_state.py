"""Split-config state is primary on WebRuntime.

Covers:
- load_split_config() splits disk operator config into platform/mission buckets.
- WebRuntime exposes platform_cfg / mission_id / mission_cfg as live state.
- Typed accessors on WebRuntime read from split state directly.
- Guardrail: no code in mav_gss_lib/ drills into a derived `runtime.cfg` dict;
  the flat projection has been retired, so every read goes through the split
  state or a typed accessor.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import mav_gss_lib.config as cfg_module  # noqa: E402


class TestLoadSplitConfig(unittest.TestCase):
    def _write(self, tmp_dir: str, payload: dict) -> str:
        path = os.path.join(tmp_dir, "gss.yml")
        with open(path, "w") as handle:
            yaml.safe_dump(payload, handle)
        return path

    def test_native_file_round_trips_without_flattening(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {
                "platform": {
                    "general": {"log_dir": "ops_logs"},
                    "tx": {"zmq_addr": "tcp://127.0.0.1:60000"},
                    "stations": {"h": "Pad"},
                },
                "mission": {"id": "maveric", "config": {"csp": {"source": 6}}},
            })
            platform, mission_id, mission = cfg_module.load_split_config(path)
            self.assertEqual(mission_id, "maveric")
            self.assertEqual(platform["general"]["log_dir"], "ops_logs")
            self.assertEqual(platform["tx"]["zmq_addr"], "tcp://127.0.0.1:60000")
            self.assertEqual(platform["stations"], {"h": "Pad"})
            self.assertEqual(mission["csp"]["source"], 6)

    def test_flat_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {
                "general": {"mission": "maveric", "log_dir": "old_logs"},
                "tx": {"zmq_addr": "tcp://127.0.0.1:60001"},
                "csp": {"source": 6},
            })
            with self.assertRaises(ValueError):
                cfg_module.load_split_config(path)


class TestSplitToPersistable(unittest.TestCase):
    def test_strips_runtime_derived_from_platform_general(self):
        platform = {
            "general": {
                "log_dir": "logs",
                "generated_commands_dir": "cmds",
                "version": "5.0.0",
                "build_sha": "abcd",
                "mission": "maveric",
            },
            "tx": {"zmq_addr": "tcp://a"},
        }
        mission = {"mission_name": "MAVERIC", "csp": {"source": 6}}
        native = cfg_module.split_to_persistable(platform, "maveric", mission)
        self.assertEqual(native["mission"]["id"], "maveric")
        self.assertEqual(native["platform"]["general"], {
            "log_dir": "logs",
            "generated_commands_dir": "cmds",
        })
        self.assertNotIn("version", native["platform"]["general"])
        self.assertNotIn("mission", native["platform"]["general"])
        self.assertEqual(native["mission"]["config"]["csp"], {"source": 6})

    def test_strips_retired_tx_uplink_mode(self):
        platform = {"tx": {"delay_ms": 100, "uplink_mode": "ASM+Golay"}}
        native = cfg_module.split_to_persistable(platform, "maveric", {})

        self.assertEqual(native["platform"]["tx"], {"delay_ms": 100})

    def test_strips_runtime_derived_radio_decoder_profile(self):
        platform = {"radio": {
            "enabled": True,
            "decoder_yml": "gnuradio/decoders/MAVERIC_DECODER.yml",
        }}
        native = cfg_module.split_to_persistable(platform, "maveric", {})

        self.assertEqual(native["platform"]["radio"], {"enabled": True})


class TestWebRuntimePrimarySplitState(unittest.TestCase):
    def test_runtime_exposes_split_state_without_mission_leak(self):
        from mav_gss_lib.server.state import create_runtime
        rt = create_runtime()
        self.assertIsInstance(rt.platform_cfg, dict)
        self.assertIsInstance(rt.mission_cfg, dict)
        # mission_id is a WebRuntime field, not a platform_cfg.general key.
        # Writing it into platform_cfg would be a mission → platform leak.
        self.assertNotIn("mission", rt.platform_cfg.get("general", {}))
        self.assertIsNotNone(rt.mission_id)
        # No flat projection survives.
        self.assertFalse(hasattr(rt, "cfg"))
        self.assertFalse(hasattr(rt, "rebuild_flat_cfg"))

    def test_typed_accessors_read_from_split_state(self):
        from mav_gss_lib.server.state import create_runtime
        rt = create_runtime()
        self.assertEqual(rt.log_dir, rt.platform_cfg["general"].get("log_dir", "logs"))
        self.assertEqual(rt.version, rt.platform_cfg["general"].get("version", ""))
        self.assertEqual(rt.build_sha, rt.platform_cfg["general"].get("build_sha", ""))
        self.assertEqual(rt.rx_frequency, rt.platform_cfg["rx"].get("frequency", ""))
        self.assertEqual(rt.tx_frequency, rt.platform_cfg["tx"].get("frequency", ""))
        self.assertEqual(rt.tx_delay_ms, int(rt.platform_cfg["tx"].get("delay_ms", 500)))
        self.assertEqual(
            rt.tx_verifiers_enabled,
            bool(rt.platform_cfg["tx"].get("verifiers_enabled", True)),
        )
        self.assertEqual(
            rt.tx_blackout_ms, int(rt.platform_cfg["rx"].get("tx_blackout_ms", 0) or 0),
        )
        from mav_gss_lib.constants import DEFAULT_MISSION_NAME
        self.assertEqual(
            rt.mission_name,
            rt.mission_cfg.get("mission_name") or DEFAULT_MISSION_NAME,
        )


class TestBackendHasNoFlatCfgReads(unittest.TestCase):
    """Guardrail — the flat `runtime.cfg` projection is retired.

    Any `.cfg` access on a `WebRuntime` object (named `runtime`, `rt`, or
    reached through `get_runtime(request)`, `app.state.runtime`, etc.) is a
    regression, because `WebRuntime` no longer defines `cfg`. All config
    access goes through the split state or typed accessors.

    Scans both the library package and top-level scripts (`mav_web.py`,
    `mav_rx.py`, `mav_tx.py`) — the earlier scope missed `mav_web.py` and a
    broken `runtime.cfg.get(...)` call survived into the shipped entrypoint.
    """

    def test_no_runtime_cfg_references(self):
        import pathlib
        import re
        repo_root = pathlib.Path(__file__).resolve().parent.parent
        patterns = [
            # Direct runtime.cfg attribute reads under any receiver name we
            # use in this codebase, plus the sneaky `get_runtime(...).cfg`
            # pattern that slipped through earlier guardrails.
            re.compile(r"\bruntime\.cfg\b"),
            re.compile(r"\bself\.runtime\.cfg\b"),
            re.compile(r"\bget_runtime\([^)]*\)\.cfg\b"),
            re.compile(r"\bapp\.state\.runtime\.cfg\b"),
        ]
        # Walk the library package plus any top-level entrypoint scripts
        # (anything .py directly under the repo root). Exclude tests (which
        # reference these patterns in docstrings and guardrail regexes).
        scan_paths: list[pathlib.Path] = []
        scan_paths.extend((repo_root / "mav_gss_lib").rglob("*.py"))
        for entry in repo_root.iterdir():
            if entry.is_file() and entry.suffix == ".py":
                scan_paths.append(entry)

        offenders: list[str] = []
        for py in scan_paths:
            text = py.read_text()
            for lineno, line in enumerate(text.splitlines(), start=1):
                for pattern in patterns:
                    if pattern.search(line):
                        offenders.append(f"{py.relative_to(repo_root)}:{lineno}: {line.strip()}")
                        break
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_webruntime_class_has_no_cfg_attribute(self):
        """Constructing a WebRuntime must not silently materialize a `cfg`
        attribute. A regression here indicates someone re-added the flat
        projection without updating the primary-state contract."""
        from mav_gss_lib.server.state import create_runtime
        rt = create_runtime()
        self.assertFalse(
            hasattr(rt, "cfg"),
            "WebRuntime.cfg is retired — use platform_cfg / mission_cfg or typed accessors",
        )


if __name__ == "__main__":
    unittest.main()
