"""PlatformConfigSpec governs which platform update keys are accepted.

`apply_platform_config_update` applies an incoming platform bucket through
the spec, writing only the declared editable sections and general keys.
Anything else (runtime-derived `version`/`build_sha`, install-time
`stations`, stray mission keys) is silently dropped.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mav_gss_lib.platform.config import (  # noqa: E402
    DEFAULT_PLATFORM_CONFIG_SPEC,
    PlatformConfigSpec,
    apply_platform_config_update,
)


class TestDefaultPlatformSpec(unittest.TestCase):
    def test_tx_and_rx_updates_merge_in(self):
        platform = {"tx": {"zmq_addr": "old"}, "rx": {"zmq_addr": "rx_old"}}
        apply_platform_config_update(platform, {
            "tx": {"delay_ms": 100, "frequency": "437.6 MHz"},
            "rx": {"frequency": "437.7 MHz", "tx_blackout_ms": 250},
        })
        self.assertEqual(platform["tx"]["delay_ms"], 100)
        self.assertEqual(platform["tx"]["frequency"], "437.6 MHz")
        self.assertEqual(platform["tx"]["zmq_addr"], "old")  # deep-merged
        self.assertEqual(platform["rx"]["frequency"], "437.7 MHz")
        self.assertEqual(platform["rx"]["tx_blackout_ms"], 250)

    def test_retired_tx_keys_are_dropped(self):
        platform = {"tx": {"delay_ms": 10}}
        apply_platform_config_update(platform, {
            "tx": {"uplink_mode": "ASM+Golay", "delay_ms": 20},
        })

        self.assertEqual(platform["tx"]["delay_ms"], 20)
        self.assertNotIn("uplink_mode", platform["tx"])

    def test_non_editable_top_level_sections_are_dropped(self):
        """csp/nodes belong on mission_cfg — never platform."""
        platform = {"tx": {}}
        apply_platform_config_update(platform, {
            "csp": {"priority": 2},
            "nodes": {"1": "X"},
            "tx": {"delay_ms": 1},
        })
        self.assertEqual(platform["tx"]["delay_ms"], 1)
        self.assertNotIn("csp", platform)
        self.assertNotIn("nodes", platform)

    def test_general_whitelist_drops_runtime_derived_and_identity_keys(self):
        platform = {"general": {"log_dir": "logs", "version": "5.0.0"}}
        apply_platform_config_update(platform, {"general": {
            "log_dir": "new_logs",
            "generated_commands_dir": "cmds",
            "mission": "maveric",
            "version": "SENTINEL",
            "build_sha": "abc",
            "mission_name": "LEAK",
        }})
        self.assertEqual(platform["general"], {
            "log_dir": "new_logs",
            "generated_commands_dir": "cmds",
            "version": "5.0.0",
        })

    def test_stations_is_install_time_and_dropped(self):
        platform = {"tx": {}, "stations": {"pad-0": "PAD"}}
        apply_platform_config_update(platform, {
            "stations": {"pad-0": "UPDATED", "pad-1": "NEW"},
            "tx": {"delay_ms": 1},
        })
        # Existing stations dict is untouched; updates to it are refused.
        self.assertEqual(platform["stations"], {"pad-0": "PAD"})

    def test_radio_section_is_operator_editable(self):
        platform = {"radio": {
            "script": "gnuradio/MAV_DUO.py",
            "decoder_yml": "gnuradio/decoders/MAVERIC_DECODER.yml",
        }}
        apply_platform_config_update(platform, {
            "radio": {
                "enabled": True,
                "autostart": False,
                "script": "gnuradio/ALT.py",
                "log_lines": 500,
                "decoder_yml": "gnuradio/decoders/ROADS_DECODER.yml",
            },
        })
        self.assertEqual(platform["radio"], {
            "script": "gnuradio/ALT.py",
            "decoder_yml": "gnuradio/decoders/MAVERIC_DECODER.yml",
            "enabled": True,
            "autostart": False,
            "log_lines": 500,
        })


class TestCustomSpec(unittest.TestCase):
    def test_extending_the_spec_adds_platform_sections_without_route_edits(self):
        """New platform sections land by extending the spec, not editing routes."""
        extended = PlatformConfigSpec(
            editable_sections=DEFAULT_PLATFORM_CONFIG_SPEC.editable_sections | {"radio"},
            editable_general_keys=DEFAULT_PLATFORM_CONFIG_SPEC.editable_general_keys,
        )
        platform = {"tx": {}}
        apply_platform_config_update(platform, {
            "radio": {"bandwidth_hz": 12000},
            "csp": {"source": 6},
        }, extended)
        self.assertEqual(platform["radio"], {"bandwidth_hz": 12000})
        self.assertNotIn("csp", platform)


if __name__ == "__main__":
    unittest.main()
