"""Per-beacon signal measurement: flowgraph metadata -> server -> log / WS.

MAV_DUO's _SignalMeter attaches `gss_signal` (UTF-8 JSON in a u8vector) to
each decoded frame. The server turns it into meta["signal"], writes it on the
rx_packet log record, and sends it as a top-level `signal` on the live
packet summary (not duplicated inside transport_meta).
"""

from __future__ import annotations

import dataclasses
import json
import unittest

import numpy as np

from mav_gss_lib.platform import MissionSpec
from mav_gss_lib.platform.contract.packets import PacketEnvelope, PacketFlags
from mav_gss_lib.platform.log_records import rx_packet_record
from mav_gss_lib.server.rx.events import packet_summary
from mav_gss_lib.transport import _decode_signal

SIGNAL = {"v": 1, "status": "ok", "peak_dbfs": -75.3, "floor_dbfs": -107.7,
          "snr_peak_db": 32.4, "edges_3db_hz": [-3027, 2051],
          "spectrum": {"f0_hz": -11914.06, "df_hz": 97.6562, "db10": [-1077, -1075]}}


def _packet(meta: dict) -> PacketEnvelope:
    return PacketEnvelope(
        seq=7, received_at_ms=1790000000000, frame_type="AX.25",
        raw=b"\x01\x02", payload=b"\x02", transport_meta=meta, warnings=[],
        mission_payload={}, mission={"id": "maveric"}, flags=PacketFlags(),
    )


class SignalMetaTests(unittest.TestCase):
    def test_u8vector_json_becomes_signal_dict(self):
        blob = np.frombuffer(json.dumps(SIGNAL).encode(), dtype=np.uint8)  # what pmt.to_python yields
        meta = {"transmitter": "9k6 FSK", "gss_signal": blob}
        _decode_signal(meta)
        self.assertEqual(meta, {"transmitter": "9k6 FSK", "signal": SIGNAL})

    def test_malformed_blob_is_dropped_not_raised(self):
        meta = {"gss_signal": np.frombuffer(b"{not json", dtype=np.uint8)}
        _decode_signal(meta)
        self.assertEqual(meta, {})

    def test_log_record_carries_signal_only_when_present(self):
        spec = MissionSpec(id="maveric", name="MAVERIC", packets=None, config=None)
        with_sig = rx_packet_record(spec, _packet({"transmitter": "x", "signal": SIGNAL}), "1",
                                    session_id="s")
        self.assertEqual(with_sig["signal"], SIGNAL)
        self.assertEqual(with_sig["transport_meta"], "x")
        json.dumps(with_sig)                                     # JSONL-safe
        without = rx_packet_record(spec, _packet({"transmitter": "x"}), "1", session_id="s")
        self.assertNotIn("signal", without)

    def test_summary_lifts_signal_out_of_transport_meta(self):
        pkt = _packet({"transmitter": "x", "signal": SIGNAL})
        summary = packet_summary(pkt, "evt")
        self.assertEqual(summary["signal"], SIGNAL)
        self.assertEqual(summary["transport_meta"], {"transmitter": "x"})
        self.assertIn("signal", pkt.transport_meta)              # envelope untouched
        plain = packet_summary(dataclasses.replace(pkt, transport_meta={}), "evt")
        self.assertNotIn("signal", plain)


if __name__ == "__main__":
    unittest.main()
