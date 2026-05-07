"""Operations-focused web runtime and config workflow tests for MAVERIC GSS."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mav_gss_lib.config import get_generated_commands_dir
from mav_gss_lib.server.api.queue_io import (
    export_queue,
    import_file,
    list_import_files,
    parse_import_file,
    preview_import,
)
from mav_gss_lib.server.tx.queue import make_checkpoint, make_delay, make_note, validate_mission_cmd
from mav_gss_lib.server.state import create_runtime


def _request_for(runtime, *, token=True):
    """Build the minimal request shape expected by the API helpers."""
    headers = {}
    if token:
        headers["x-gss-token"] = runtime.session_token
    app = SimpleNamespace(state=SimpleNamespace(runtime=runtime))
    return SimpleNamespace(app=app, headers=headers)


def _make_mission_item(cmd_id="com_ping", args="", dest="LPPM", runtime=None):
    """Build a validated mission_cmd queue item for testing."""
    payload = {
        "cmd_id": cmd_id,
        "args": args if isinstance(args, dict) else {},
        "packet": {"dest": dest},
    }
    return validate_mission_cmd(payload, runtime=runtime)


class TestWebRuntimeWorkflows(unittest.TestCase):
    """Covers config-path and import/export workflows exposed to operators."""

    def setUp(self):
        self.runtime = create_runtime()
        self.tmp = tempfile.TemporaryDirectory()
        self.generated_dir = Path(self.tmp.name) / "imports"
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        general = self.runtime.platform_cfg.setdefault("general", {})
        general["generated_commands_dir"] = str(self.generated_dir)
        general["log_dir"] = self.tmp.name
        self.runtime.tx.queue.clear()

        async def _noop(_msg=None):
            # API helpers await this during import/export flows.
            return None

        self.runtime.tx.send_queue_update = _noop

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_import_file_produces_mission_cmd_items(self):
        payload = """
        // comment
        {"type": "mission_cmd", "guard": true, "payload": {"cmd_id": "com_ping", "args": {}, "packet": {"dest": "EPS"}}} // trailing
        {"type": "checkpoint", "text": "Confirm EPS bus is stable"}
        {"type": "delay", "delay_ms": 250}
        """.strip()
        path = self.generated_dir / "sample.jsonl"
        path.write_text(payload + "\n")
        items, skipped = parse_import_file(path, runtime=self.runtime)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0]["type"], "note")
        self.assertEqual(items[0]["text"], "comment")
        self.assertEqual(items[1]["type"], "mission_cmd")
        self.assertTrue(items[1]["guard"])
        self.assertEqual(items[1]["cmd_id"], "com_ping")
        self.assertEqual(items[2]["type"], "checkpoint")
        self.assertEqual(items[2]["text"], "Confirm EPS bus is stable")
        self.assertEqual(items[3]["type"], "delay")

    def test_list_import_files_uses_configured_directory(self):
        (self.generated_dir / "a.jsonl").write_text("{}\n")
        (self.generated_dir / "b.jsonl").write_text("{}\n")
        result = asyncio.run(list_import_files(_request_for(self.runtime)))
        names = [item["name"] for item in result]
        self.assertEqual(set(names), {"a.jsonl", "b.jsonl"})

    def test_preview_returns_mission_facts(self):
        path = self.generated_dir / "queue.jsonl"
        path.write_text('{"type": "mission_cmd", "payload": {"cmd_id": "com_ping", "args": {}, "packet": {"dest": "EPS"}}}\n')
        preview = asyncio.run(preview_import("queue.jsonl", _request_for(self.runtime)))
        self.assertEqual(preview["skipped"], 0)
        self.assertEqual(len(preview["items"]), 1)
        item = preview["items"][0]
        self.assertEqual(item["type"], "mission_cmd")
        self.assertEqual(item["cmd_id"], "com_ping")
        self.assertIn("mission", item)
        # cmd_id is canonical at the queue item top level (item["cmd_id"]);
        # not duplicated under mission.facts.header (post-Task-6).
        self.assertNotIn("cmd_id", item["mission"]["facts"]["header"])
        self.assertEqual(item["mission"]["facts"]["header"]["dest"], "EPS")

    def test_preview_returns_checkpoint_items(self):
        path = self.generated_dir / "checkpoint.jsonl"
        path.write_text('{"type": "checkpoint", "text": "Confirm pass constraints"}\n')
        preview = asyncio.run(preview_import("checkpoint.jsonl", _request_for(self.runtime)))
        self.assertEqual(preview["skipped"], 0)
        self.assertEqual(preview["items"], [
            {"type": "checkpoint", "text": "Confirm pass constraints"},
        ])

    def test_import_produces_mission_cmd_queue_items(self):
        path = self.generated_dir / "queue.jsonl"
        path.write_text('{"type": "mission_cmd", "payload": {"cmd_id": "com_ping", "args": {}, "packet": {"dest": "EPS"}}}\n')
        result = asyncio.run(import_file("queue.jsonl", _request_for(self.runtime)))
        self.assertEqual(result["loaded"], 1)
        item = self.runtime.tx.queue[0]
        self.assertEqual(item["type"], "mission_cmd")
        self.assertEqual(item["cmd_id"], "com_ping")

    def test_import_preserves_delay_and_mission_command(self):
        path = self.generated_dir / "command_and_delay.jsonl"
        path.write_text('{"type": "mission_cmd", "payload": {"cmd_id": "com_ping", "args": {}, "packet": {"dest": "EPS"}}}\n{"type": "delay", "delay_ms": 500}\n')
        result = asyncio.run(import_file("command_and_delay.jsonl", _request_for(self.runtime)))
        self.assertEqual(result["loaded"], 2)
        self.assertEqual([item["type"] for item in self.runtime.tx.queue], ["mission_cmd", "delay"])

    def test_export_queue_writes_to_configured_directory(self):
        self.runtime.tx.queue.extend(
            [
                _make_mission_item("com_ping", "", runtime=self.runtime),
                make_delay(500),
            ]
        )
        result = asyncio.run(export_queue({"name": "ops smoke"}, _request_for(self.runtime)))
        self.assertTrue(result["ok"])
        export_path = self.generated_dir / "ops_smoke.jsonl"
        self.assertTrue(export_path.exists())
        contents = export_path.read_text()
        self.assertIn('"type": "mission_cmd"', contents)
        self.assertIn('"type": "delay"', contents)

    def test_export_then_import_round_trips_notes_delays_and_commands(self):
        self.runtime.tx.queue.extend(
            [
                make_note("ops note"),
                make_checkpoint("operator checkpoint"),
                _make_mission_item("com_ping", "", runtime=self.runtime),
                make_delay(500),
            ]
        )
        exported = asyncio.run(export_queue({"name": "roundtrip"}, _request_for(self.runtime)))
        self.assertTrue(exported["ok"])

        self.runtime.tx.queue.clear()
        imported = asyncio.run(import_file("roundtrip.jsonl", _request_for(self.runtime)))

        self.assertEqual(imported["loaded"], 4)
        self.assertEqual(imported["skipped"], 0)
        self.assertEqual([item["type"] for item in self.runtime.tx.queue], ["note", "checkpoint", "mission_cmd", "delay"])
        self.assertEqual(self.runtime.tx.queue[0]["text"], "ops note")
        self.assertEqual(self.runtime.tx.queue[1]["text"], "operator checkpoint")
        self.assertEqual(self.runtime.tx.queue[2]["cmd_id"], "com_ping")
        self.assertEqual(self.runtime.tx.queue[3]["delay_ms"], 500)

    def test_export_queue_requires_session_token(self):
        self.runtime.tx.queue.append(
            _make_mission_item("com_ping", "", runtime=self.runtime)
        )
        response = asyncio.run(export_queue({"name": "blocked"}, _request_for(self.runtime, token=False)))
        self.assertEqual(response.status_code, 403)


    def test_log_pagination_respects_offset_and_limit(self):
        """Paginated log endpoint returns bounded results with has_more."""
        import json as _json

        # Redirect log_dir to isolated temp directory
        log_base = Path(self.tmp.name) / "logs"
        self.runtime.platform_cfg["general"]["log_dir"] = str(log_base)
        log_dir = log_base / "json"
        log_dir.mkdir(parents=True, exist_ok=True)

        # Write 5 unified-schema rx_packet entries
        session_id = "session_20260408_120000"
        log_file = log_dir / f"{session_id}.jsonl"
        entries = []
        for i in range(5):
            entry = {
                "event_id": f"e{i}", "event_kind": "rx_packet",
                "session_id": session_id,
                "ts_ms": 1712577600000 + i * 1000,
                "ts_iso": f"2026-04-08T12:00:{i:02d}.000+00:00",
                "seq": i + 1, "v": "5.7.0", "mission_id": "maveric",
                "operator": "", "station": "",
                "frame_type": "HDLC", "transport_meta": "",
                "raw_hex": f"{i:02x}" * 10, "size": 20,
                "duplicate": False, "uplink_echo": False, "unknown": False,
                "warnings": [], "mission": {},
            }
            entries.append(_json.dumps(entry))
        log_file.write_text("\n".join(entries) + "\n")

        from mav_gss_lib.server.api.logs import api_log_entries

        req = _request_for(self.runtime)
        result = asyncio.run(api_log_entries(
            session_id=session_id,
            request=req,
            label=None,
            time_from=None,
            time_to=None,
            event_kind="rx_packet,tx_command",
            offset=1,
            limit=2,
        ))
        self.assertIsInstance(result, dict)
        self.assertIn("entries", result)
        self.assertIn("has_more", result)
        self.assertEqual(len(result["entries"]), 2)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["offset"], 1)
        self.assertEqual(result["limit"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
