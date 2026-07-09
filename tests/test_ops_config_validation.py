"""Tests for mission config seeding and startup diagnostics.

Verifies:
  1. MAVERIC build(ctx) seeds mission_cfg with operator-overridable defaults
     (csp/imaging) and gap-fills RX/TX frequency defaults onto platform_cfg.
  2. Operator-supplied values in gss.yml win over mission defaults.
  3. Platform _DEFAULTS stay mission-free.
  4. load_mission_spec_from_split + build(ctx) produce a populated MissionSpec.

NodeTable / nodes.py / config_access.py assertions were removed when the
declarative codec took ownership of node/ptype identity (those live under
mission.yml `extensions:` now and are exercised by the gitignored
declarative test suite). Tests skip-gate on mission.yml presence since
the declarative builder requires it.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mav_gss_lib.config import _DEFAULTS, load_split_config
from mav_gss_lib.missions.maveric.mission import MISSION_YML_PATH, _seed
from mav_gss_lib.platform.loader import load_mission_spec_from_split


_MISSION_YML_PRESENT = MISSION_YML_PATH.is_file()


@unittest.skipUnless(_MISSION_YML_PRESENT, "mission.yml not present (gitignored)")
class TestMissionDefaultsSeeding(unittest.TestCase):
    """Verify MAVERIC defaults seeding into split state."""

    def test_seed_fills_missing_keys(self):
        platform_cfg: dict = {"tx": {}, "rx": {}}
        mission_cfg: dict = {}
        _seed(mission_cfg, platform_cfg)

        # csp / imaging gap-filled with MAVERIC's real defaults.
        self.assertIn("csp", mission_cfg)
        self.assertIn("imaging", mission_cfg)
        self.assertEqual(mission_cfg["imaging"]["thumb_prefix"], "tn_")
        # Real uplink routing, not neutral zeros (spacecraft node 8 / port 24).
        self.assertEqual(mission_cfg["csp"]["destination"], 8)
        self.assertEqual(mission_cfg["csp"]["dest_port"], 24)

        # RX/TX defaults gap-fill onto platform_cfg.
        self.assertIn("frequency", platform_cfg["rx"])
        self.assertIn("frequency", platform_cfg["tx"])

    def test_seed_respects_operator_overrides(self):
        """Operator-set values win over mission defaults; one-deep merge on
        csp/imaging fills only the gaps."""
        mission_cfg = {
            "csp": {"dest_port": 24},
            "imaging": {"thumb_prefix": "thumb_"},
        }
        platform_cfg = {"rx": {"frequency": "437.7 MHz"}, "tx": {"frequency": "437.6 MHz"}}
        _seed(mission_cfg, platform_cfg)

        # Operator values preserved.
        self.assertEqual(mission_cfg["csp"]["dest_port"], 24)
        self.assertEqual(mission_cfg["imaging"]["thumb_prefix"], "thumb_")
        self.assertEqual(platform_cfg["rx"]["frequency"], "437.7 MHz")
        self.assertEqual(platform_cfg["tx"]["frequency"], "437.6 MHz")

        # Default fills the gap on the same dict.
        self.assertEqual(mission_cfg["csp"]["priority"], 2)

    def test_build_maveric_from_empty_split_seeds_mission_cfg(self):
        """Loading the MAVERIC spec with an empty mission_cfg still yields
        a populated spec via build(ctx)."""
        mission_cfg: dict = {}
        spec = load_mission_spec_from_split({}, "maveric", mission_cfg)
        self.assertEqual(spec.name, "MAVERIC")
        self.assertIn("csp", mission_cfg)
        self.assertIn("imaging", mission_cfg)


class TestConfigDefaults(unittest.TestCase):
    def test_platform_defaults_have_no_mission_keys(self):
        """Platform _DEFAULTS must not carry mission-owned placeholders."""
        for key in ("nodes", "ptypes", "node_descriptions", "csp"):
            self.assertNotIn(key, _DEFAULTS,
                f"'{key}' should not be in platform _DEFAULTS")

    def test_load_split_config_returns_mission_free_platform_cfg(self):
        """load_split_config returns platform_cfg without mission keys."""
        platform_cfg, _, _ = load_split_config()
        self.assertIn("tx", platform_cfg)
        self.assertIn("rx", platform_cfg)
        self.assertIn("general", platform_cfg)
        for key in ("nodes", "ptypes", "csp"):
            self.assertNotIn(key, platform_cfg)

    @unittest.skipUnless(_MISSION_YML_PRESENT, "mission.yml not present (gitignored)")
    def test_build_populates_mission_cfg_from_real_gss_yml(self):
        """Loading MAVERIC against the real operator split state yields a
        populated mission_cfg."""
        platform_cfg, mission_id, mission_cfg = load_split_config()
        load_mission_spec_from_split(platform_cfg, mission_id, mission_cfg)
        self.assertIn("csp", mission_cfg)
        self.assertIn("imaging", mission_cfg)


if __name__ == "__main__":
    unittest.main()
