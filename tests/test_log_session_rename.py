"""Two-phase rename: prepare → commit (or rollback) atomically rewrites
session_id in already-emitted records.

The previous behavior left old records carrying the pre-rename session_id,
breaking the CLAUDE.md invariant that session_id == file stem. The orchestrator
also needs a rollback path that restores BOTH filename and content.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from mav_gss_lib.logging.session import SessionLog


def _write_one(log: SessionLog) -> str:
    sid = log.session_id
    log.write_jsonl({
        "event_id": "e1", "event_kind": "radio",
        "session_id": sid, "ts_ms": 1, "ts_iso": "x",
        "seq": 0, "v": "test", "mission_id": "maveric",
        "operator": "irfan", "station": "GS-1", "radio": {},
    })
    return sid


class TestSessionRenameForward(unittest.TestCase):
    def test_prepare_then_commit_rewrites_session_id(self):
        with tempfile.TemporaryDirectory() as d:
            log = SessionLog(d, zmq_addr="tcp://127.0.0.1:0",
                             station="GS-1", operator="irfan")
            original_sid = _write_one(log)
            prepared = log.prepare_rename("test_tag")
            log.commit_rename(prepared)
            new_sid = log.session_id
            new_path = log.jsonl_path
            self.assertNotEqual(original_sid, new_sid)
            log.close()
            # Old file is gone.
            self.assertFalse(os.path.exists(prepared["old_jsonl_path"]))
            # All lines on disk carry the NEW session_id.
            with open(new_path) as f:
                lines = [l for l in f if l.strip()]
            self.assertGreater(len(lines), 0, "no records on disk")
            for line in lines:
                rec = json.loads(line)
                self.assertEqual(rec["session_id"], new_sid)


class TestSessionRenameRollback(unittest.TestCase):
    def test_prepare_then_rollback_leaves_old_state_intact(self):
        with tempfile.TemporaryDirectory() as d:
            log = SessionLog(d, zmq_addr="tcp://127.0.0.1:0",
                             station="GS-1", operator="irfan")
            original_sid = _write_one(log)
            original_path = log.jsonl_path
            prepared = log.prepare_rename("test_tag")
            log.rollback_rename(prepared)
            # No state change: filename + session_id unchanged.
            self.assertEqual(log.jsonl_path, original_path)
            self.assertEqual(log.session_id, original_sid)
            # Sidecar new file is gone.
            self.assertFalse(os.path.exists(prepared["new_jsonl_path"]))
            # We can keep writing — the writer is still alive.
            log.write_jsonl({
                "event_id": "e2", "event_kind": "radio",
                "session_id": original_sid, "ts_ms": 2, "ts_iso": "x",
                "seq": 0, "v": "test", "mission_id": "maveric",
                "operator": "irfan", "station": "GS-1", "radio": {},
            })
            log.close()
            with open(original_path) as f:
                lines = [json.loads(l) for l in f if l.strip()]
            self.assertEqual(len(lines), 2)
            for rec in lines:
                self.assertEqual(rec["session_id"], original_sid)


class TestRollbackRespectsPostCommitRecovery(unittest.TestCase):
    """If commit_rename's recovery branch removed the old file before
    failing — leaving new_jsonl as the canonical log on disk — the
    orchestrator's rollback_rename must NOT delete new_jsonl. Doing so
    would silently destroy operator session data.
    """

    def test_rollback_skips_delete_when_log_already_swapped_to_new(self):
        with tempfile.TemporaryDirectory() as d:
            log = SessionLog(d, zmq_addr="tcp://127.0.0.1:0",
                             station="GS-1", operator="irfan")
            original_sid = _write_one(log)
            prepared = log.prepare_rename("test_tag")
            # Simulate commit_rename's recovery-branch end state: old is
            # gone, sidecar is canonical, log handle + session_id swapped.
            os.remove(prepared["old_jsonl_path"])
            log.jsonl_path = prepared["new_jsonl_path"]
            log.session_id = prepared["new_session_id"]
            # Rollback should detect the swapped state and refuse to delete.
            log.rollback_rename(prepared)
            self.assertTrue(os.path.exists(prepared["new_jsonl_path"]),
                            "rollback must not delete the live log file")
            log.close()


if __name__ == "__main__":
    unittest.main()
