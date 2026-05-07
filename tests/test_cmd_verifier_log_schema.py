"""cmd_verifier log event shape and envelope compatibility.

SessionLog construction:
  SessionLog(log_dir: str, zmq_addr: str, version="", mission_name=...,
        *, mission_id="", station="", operator="", host="")
  Session opens automatically when the first write fires (via _BaseLog's
  lazy-open path); callers do NOT need an explicit `log.open()`.
"""
import json
import tempfile
import unittest
from pathlib import Path

from mav_gss_lib.logging import SessionLog


class CmdVerifierWrites(unittest.TestCase):
    def test_write_appends_a_cmd_verifier_event_to_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = SessionLog(
                tmp,                              # log_dir (positional)
                "tcp://127.0.0.1:0",              # zmq_addr (positional, unused here)
                version="test",
                mission_name="maveric",
                mission_id="maveric",
                station="GS-0",
                operator="op",
            )
            log.write_cmd_verifier({
                "cmd_event_id": "c1", "instance_id": "i1",
                "stage": "received", "verifier_id": "uppm_ack",
                "outcome": "passed", "elapsed_ms": 445,
                "match_event_id": "e9", "seq": 1,
            })
            log.close()
            files = sorted(Path(tmp).glob("json/*.jsonl"))
            self.assertEqual(len(files), 1)
            lines = files[0].read_text().strip().splitlines()
            events = [json.loads(l) for l in lines]
            verifier_events = [e for e in events if e.get("event_kind") == "cmd_verifier"]
            self.assertEqual(len(verifier_events), 1)
            ev = verifier_events[0]
            for k in ("event_id", "session_id", "ts_ms", "ts_iso", "seq", "v",
                      "mission_id", "operator", "station"):
                self.assertIn(k, ev)
            self.assertEqual(ev["cmd_event_id"], "c1")
            self.assertEqual(ev["stage"], "received")


if __name__ == "__main__":
    unittest.main()
