"""UiSpec parser tests + mission.yml round-trip."""
import unittest
from pathlib import Path
from textwrap import dedent

from mav_gss_lib.platform.spec import parse_ui_section, parse_yaml_for_tooling


class UiSpecParserTests(unittest.TestCase):
    def test_returns_none_when_absent(self):
        self.assertIsNone(parse_ui_section(None))

    def test_empty_rx_columns(self):
        spec = parse_ui_section({})
        self.assertIsNotNone(spec)
        self.assertEqual(spec.rx_columns, ())

    def test_parses_rx_columns(self):
        spec = parse_ui_section({
            "rx_columns": [
                {"id": "src", "label": "src", "width": 52, "path": "header.src"},
                {"id": "echo", "label": "echo", "toggle": "showEcho", "path": "header.echo"},
                {
                    "id": "ptype",
                    "label": "type",
                    "badge": True,
                    "path": "header.ptype",
                    "value_icons": {"CMD": "command", "TLM": "telemetry"},
                    "default_icon": "unknown",
                },
                {"id": "cmd", "label": "cmd", "flex": True, "kind": "cmd_id"},
            ],
        })
        self.assertEqual(len(spec.rx_columns), 4)

        src = spec.rx_columns[0]
        self.assertEqual(src.id, "src")
        self.assertEqual(src.label, "src")
        self.assertEqual(src.path, "header.src")
        self.assertEqual(src.width, 52)
        self.assertFalse(src.badge)

        echo = spec.rx_columns[1]
        self.assertEqual(echo.toggle, "showEcho")

        ptype = spec.rx_columns[2]
        self.assertTrue(ptype.badge)
        self.assertEqual(ptype.value_icons, (("CMD", "command"), ("TLM", "telemetry")))
        self.assertEqual(ptype.default_icon, "unknown")

        cmd = spec.rx_columns[3]
        self.assertTrue(cmd.flex)
        self.assertEqual(cmd.kind, "cmd_id")

    def test_to_json_strips_defaults(self):
        spec = parse_ui_section({
            "rx_columns": [
                {"id": "src", "label": "src", "path": "header.src", "width": 52},
            ],
        })
        self.assertEqual(
            spec.rx_columns[0].to_json(),
            {"id": "src", "label": "src", "path": "header.src", "width": 52},
        )

    def test_to_json_includes_icon_tokens(self):
        spec = parse_ui_section({
            "rx_columns": [
                {
                    "id": "ptype",
                    "label": "type",
                    "path": "header.ptype",
                    "badge": True,
                    "value_icons": {"CMD": "command", "RES": "response"},
                    "default_icon": "unknown",
                },
            ],
        })
        self.assertEqual(
            spec.rx_columns[0].to_json(),
            {
                "id": "ptype",
                "label": "type",
                "path": "header.ptype",
                "badge": True,
                "value_icons": {"CMD": "command", "RES": "response"},
                "default_icon": "unknown",
            },
        )

    def test_rejects_missing_id(self):
        with self.assertRaises(ValueError):
            parse_ui_section({"rx_columns": [{"label": "x", "path": "y"}]})

    def test_rejects_missing_path(self):
        with self.assertRaises(ValueError):
            parse_ui_section({"rx_columns": [{"id": "x", "label": "x"}]})

    def test_rejects_duplicate_ids(self):
        with self.assertRaises(ValueError):
            parse_ui_section({
                "rx_columns": [
                    {"id": "x", "label": "x", "path": "a"},
                    {"id": "x", "label": "y", "path": "b"},
                ],
            })

    def test_rejects_invalid_align(self):
        with self.assertRaises(ValueError):
            parse_ui_section({
                "rx_columns": [
                    {"id": "x", "label": "x", "path": "a", "align": "center"},
                ],
            })

    def test_rejects_invalid_icon_tokens(self):
        with self.assertRaises(ValueError):
            parse_ui_section({
                "rx_columns": [
                    {
                        "id": "x",
                        "label": "x",
                        "path": "a",
                        "badge": True,
                        "value_icons": {"CMD": "send"},
                    },
                ],
            })

        with self.assertRaises(ValueError):
            parse_ui_section({
                "rx_columns": [
                    {
                        "id": "x",
                        "label": "x",
                        "path": "a",
                        "badge": True,
                        "default_icon": "satellite",
                    },
                ],
            })

    def test_rejects_non_list_rx_columns(self):
        with self.assertRaises(ValueError):
            parse_ui_section({"rx_columns": "not a list"})

    def test_rejects_non_integer_width(self):
        """Spec contract pin: width must be a positive integer (pixels).
        Catches the prior Tailwind-class-string format ('w-[52px]') and
        any other non-numeric value — these would otherwise silently
        produce no-op className strings at render time."""
        for bad in ("w-[52px]", "52", 0, -10, True, 52.5):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    parse_ui_section({
                        "rx_columns": [
                            {"id": "x", "label": "x", "path": "a", "width": bad},
                        ],
                    })

    def test_parses_truncate_flag(self):
        spec = parse_ui_section({
            "rx_columns": [
                {"id": "args", "label": "args", "path": "header.args",
                 "width": 200, "truncate": True},
            ],
        })
        self.assertTrue(spec.rx_columns[0].truncate)
        self.assertEqual(
            spec.rx_columns[0].to_json(),
            {"id": "args", "label": "args", "path": "header.args",
             "width": 200, "truncate": True},
        )

    def test_truncate_defaults_false(self):
        spec = parse_ui_section({
            "rx_columns": [
                {"id": "src", "label": "src", "path": "header.src"},
            ],
        })
        self.assertFalse(spec.rx_columns[0].truncate)
        self.assertNotIn("truncate", spec.rx_columns[0].to_json())


class MissionDocumentUiTests(unittest.TestCase):
    def test_mission_yaml_round_trip(self):
        p = Path("/tmp/test_mission_ui.yml")
        p.write_text(dedent("""
            schema_version: 1
            id: testmission
            name: "Test"
            header: {version: "0", date: "2026-04-26"}
            ui:
              rx_columns:
                - id: src
                  label: src
                  width: 52
                  path: header.src
                - id: ptype
                  label: type
                  width: 52
                  badge: true
                  path: header.ptype
                  value_icons:
                    CMD: command
                    TLM: telemetry
                  default_icon: unknown
        """).strip())
        try:
            mission = parse_yaml_for_tooling(p)
            self.assertIsNotNone(mission.ui)
            self.assertEqual(len(mission.ui.rx_columns), 2)
            self.assertEqual(mission.ui.rx_columns[0].id, "src")
            self.assertTrue(mission.ui.rx_columns[1].badge)
            self.assertEqual(mission.ui.rx_columns[1].default_icon, "unknown")
        finally:
            p.unlink(missing_ok=True)

    def test_missing_ui_block_yields_none(self):
        p = Path("/tmp/test_mission_no_ui.yml")
        p.write_text(dedent("""
            schema_version: 1
            id: testmission
            name: "Test"
            header: {version: "0", date: "2026-04-26"}
        """).strip())
        try:
            mission = parse_yaml_for_tooling(p)
            self.assertIsNone(mission.ui)
        finally:
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
