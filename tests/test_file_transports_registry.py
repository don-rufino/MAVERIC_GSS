"""Tests for FILE_TRANSPORTS + build-time validation."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from mav_gss_lib.missions.maveric.files.registry import (
    FILE_TRANSPORTS,
    build_file_kind_adapters,
    validate_against_mission,
)


@dataclass(frozen=True)
class _StubMetaCmd:
    name: str


@dataclass(frozen=True)
class _StubMission:
    meta_commands: dict[str, _StubMetaCmd]


def _mission_with(*names: str) -> _StubMission:
    return _StubMission(meta_commands={n: _StubMetaCmd(name=n) for n in names})


class FileTransportsTests(unittest.TestCase):
    def test_registry_lists_four_kinds(self):
        kinds = [c.kind for c in FILE_TRANSPORTS]
        self.assertEqual(kinds, ["image", "aii", "mag", "eps"])

    def test_build_returns_four_adapters_in_order(self):
        adapters = build_file_kind_adapters({"imaging": {"thumb_prefix": "tn_"}})
        self.assertEqual([a.kind for a in adapters], ["image", "aii", "mag", "eps"])
        self.assertEqual(adapters[0].cnt_cmd, "img_cnt_chunks")
        self.assertEqual(adapters[1].cnt_cmd, "aii_cnt_chunks")
        self.assertEqual(adapters[2].cnt_cmd, "mag_cnt_chunks")
        self.assertEqual(adapters[3].cnt_cmd, "eps_cnt_chunks")

    def test_validate_passes_with_full_mission(self):
        mission = _mission_with(
            "img_cnt_chunks", "img_get_chunks", "cam_capture",
            "aii_cnt_chunks", "aii_get_chunks",
            "mag_cnt_chunks", "mag_get_chunks",
            "eps_cnt_chunks", "eps_get_chunks",
        )
        # Should not raise.
        validate_against_mission(mission)

    def test_validate_raises_when_cmd_missing(self):
        mission = _mission_with(
            "img_cnt_chunks", "img_get_chunks", "cam_capture",
            "aii_cnt_chunks", "aii_get_chunks",
            # mag_cnt_chunks intentionally missing
            "mag_get_chunks",
        )
        with self.assertRaises(ValueError) as cm:
            validate_against_mission(mission)
        self.assertIn("mag_cnt_chunks", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
