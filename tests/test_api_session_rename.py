"""Smoke + failure coverage for PATCH /api/session.

Two-phase rename happens inside this endpoint: prepare_rename per log,
then commit_rename per log. We exercise:
  * success — endpoint returns 200, records on disk reflect the new
    session_id, files renamed.
  * preflight failure — target filename already exists → 409, no log
    is renamed, session_id stays at its pre-call value.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from mav_gss_lib.server.app import create_app


class TestApiSessionRename(unittest.TestCase):
    def _build_app(self, tmp: str):
        """Mirror tests/test_api_tx_clear_sent.py:_build_app — redirect
        log_dir before lifespan so SessionLog opens files in tmp, not cwd."""
        app = create_app()
        app.state.runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
        token = app.state.runtime.session_token
        return app, token

    def test_rename_success_rewrites_session_id_in_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, token = self._build_app(tmp)
            with TestClient(app) as client:
                runtime = app.state.runtime
                rx_log = runtime.rx.log
                self.assertIsNotNone(rx_log)
                original_sid = rx_log.session_id
                original_path = rx_log.jsonl_path
                rx_log.write_jsonl({
                    "event_id": "e1", "event_kind": "radio",
                    "session_id": original_sid, "ts_ms": 1, "ts_iso": "x",
                    "seq": 0, "v": "test", "mission_id": "maveric",
                    "operator": "irfan", "station": "GS-1", "radio": {},
                })
                resp = client.patch(
                    "/api/session",
                    json={"session_tag": "test_t"},
                    headers={"x-gss-token": token},
                )
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertNotEqual(rx_log.jsonl_path, original_path)
                self.assertNotEqual(rx_log.session_id, original_sid)
                self.assertIn("test_t", rx_log.session_id)
                with open(rx_log.jsonl_path) as f:
                    lines = [l for l in f if l.strip()]
                self.assertGreater(len(lines), 0, "no records on disk")
                for line in lines:
                    rec = json.loads(line)
                    self.assertEqual(rec["session_id"], rx_log.session_id)

    def test_rename_preflight_failure_leaves_logs_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, token = self._build_app(tmp)
            with TestClient(app) as client:
                runtime = app.state.runtime
                rx_log = runtime.rx.log
                original_sid = rx_log.session_id
                original_path = rx_log.jsonl_path
                base, ext = os.path.splitext(original_path)
                blocker_path = f"{base}_blocked{ext}"
                Path(blocker_path).touch()
                resp = client.patch(
                    "/api/session",
                    json={"session_tag": "blocked"},
                    headers={"x-gss-token": token},
                )
                self.assertEqual(resp.status_code, 409, resp.text)
                self.assertEqual(rx_log.jsonl_path, original_path)
                self.assertEqual(rx_log.session_id, original_sid)
                self.assertTrue(os.path.exists(original_path))

    def test_rename_requires_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _token = self._build_app(tmp)
            with TestClient(app) as client:
                resp = client.patch("/api/session", json={"session_tag": "x"})
                self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
