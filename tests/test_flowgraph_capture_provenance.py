"""Focused tranche-4 guards for live flowgraph capture provenance."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np
import yaml


ROOT = Path(__file__).resolve().parent.parent
GNURADIO_DIR = ROOT / "gnuradio"
sys.path.insert(0, str(GNURADIO_DIR))

try:
    import pmt
    import MAV_ASTROCAST
    import MAV_DUO
except Exception as exc:  # default test env does not necessarily ship GNU Radio
    pmt = None
    MAV_ASTROCAST = None
    MAV_DUO = None
    FLOWGRAPH_IMPORT_ERROR = str(exc)
else:
    FLOWGRAPH_IMPORT_ERROR = ""


class FlowgraphCaptureSourceGuards(unittest.TestCase):
    def test_grc_mirrors_provenance_and_tx_health_wiring(self):
        grc = yaml.safe_load(
            (GNURADIO_DIR / "MAV_DUO.grc").read_text(encoding="utf-8"))
        blocks = {block["name"]: block for block in grc["blocks"]}
        connections = {tuple(connection) for connection in grc["connections"]}

        for name in ("epy_block_iq", "epy_block_iq_raw",
                     "epy_block_tx_health"):
            source = blocks[name]["parameters"]["_source_code"]
            compile(source, name, "exec")

        self.assertEqual(
            blocks["uhd_usrp_source_0"]["parameters"]["sync"], "pc_clock")
        self.assertEqual(
            blocks["uhd_usrp_sink_0"]["parameters"]["sync"], "none")
        self.assertEqual(
            blocks["blocks_var_to_msg_rx_gain"]["parameters"]["target"],
            "rx_gain")
        self.assertIn(
            ("blocks_var_to_msg_rx_gain", "msgout", "epy_block_iq", "command"),
            connections)
        self.assertIn(
            ("blocks_var_to_msg_rx_gain", "msgout", "epy_block_iq_raw", "command"),
            connections)
        self.assertIn(
            ("uhd_usrp_sink_0", "async_msgs", "epy_block_tx_health", "async"),
            connections)

        post = blocks["epy_block_iq"]["parameters"]["_source_code"]
        raw = blocks["epy_block_iq_raw"]["parameters"]["_source_code"]
        tx = blocks["epy_block_tx_health"]["parameters"]["_source_code"]
        self.assertIn("GSS_IQ_MAX_BYTES", post)
        self.assertIn("GSS_IQ_RAW_MAX_BYTES", raw)
        self.assertIn("usrp_rx_time", post)
        self.assertIn("doppler_command", post)
        self.assertIn('== "rx_gain"', post)
        self.assertIn("TX_HEALTH", tx)
        self.assertIn("underflow_in_packet", tx)
        self.assertNotIn("msg_to_async_metadata_t", tx)


@unittest.skipIf(MAV_ASTROCAST is None, FLOWGRAPH_IMPORT_ERROR)
class IqRecorderProvenanceTests(unittest.TestCase):
    def _environment(self, directory: str) -> dict[str, str]:
        return {
            "GSS_IQ_DIR": directory,
            "GSS_IQ_RECORD": "1",
            "GSS_IQ_RAW_RECORD": "1",
            "GSS_IQ_MAX_BYTES": "800",
            "GSS_IQ_RAW_MAX_BYTES": "1600",
            "GSS_MISSION": "astrocast",
            "GSS_RX_FREQ_HZ": "437150000",
            "GSS_RX_GAIN": "40",
        }

    def test_cap_env_and_same_second_names_are_collision_safe(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, self._environment(directory), clear=False), \
                mock.patch.object(
                    MAV_ASTROCAST.time, "strftime",
                    return_value="20260711T192712Z"):
            first = MAV_ASTROCAST._IqRecorder(samp_rate=200_000.0)
            second = MAV_ASTROCAST._IqRecorder(samp_rate=200_000.0)
            raw = MAV_ASTROCAST._IqRecorder(
                samp_rate=1_000_000.0, prefix="iqraw",
                gate_env="GSS_IQ_RAW_RECORD", max_bytes=50_000_000_000)
            try:
                self.assertEqual(first._max_bytes, 800)
                self.assertEqual(raw._max_bytes, 1600)
                self.assertNotEqual(first._data_path, second._data_path)
                self.assertTrue(first._data_path.endswith(".sigmf-data"))
                self.assertTrue(second._data_path.endswith("_001.sigmf-data"))
                self.assertEqual(os.path.getsize(first._data_path), 0)
                self.assertEqual(os.path.getsize(second._data_path), 0)
            finally:
                first.stop()
                second.stop()
                raw.stop()

    def test_rx_time_tune_and_gain_are_fractional_and_sample_indexed(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, self._environment(directory), clear=False):
            recorder = MAV_ASTROCAST._IqRecorder(samp_rate=1_000.0)
            try:
                tag_time = pmt.make_tuple(
                    pmt.from_uint64(1_720_000_000), pmt.from_double(0.375125))
                with recorder._lock:
                    recorder._record_rx_time_tags_locked(
                        [SimpleNamespace(offset=5, value=tag_time)], nread=0)

                recorder._written = 10 * 8
                tune = pmt.make_dict()
                tune = pmt.dict_add(
                    tune, pmt.intern("lo_freq"), pmt.from_double(437_400_000.0))
                tune = pmt.dict_add(
                    tune, pmt.intern("dsp_freq"), pmt.from_double(247_000.0))
                recorder._handle_tune_command(tune)
                recorder._handle_tune_command(
                    pmt.cons(pmt.intern("rx_gain"), pmt.from_double(55.0)))

                with open(recorder._meta_path, encoding="utf-8") as handle:
                    meta = json.load(handle)
                initial = meta["captures"][0]
                expected_zero = 1_720_000_000.375125 - 0.005
                actual_zero = datetime.fromisoformat(
                    initial["core:datetime"].replace("Z", "+00:00")).timestamp()
                self.assertAlmostEqual(actual_zero, expected_zero, places=6)
                self.assertEqual(initial["maveric:datetime_source"], "usrp_rx_time")

                tune_capture = meta["captures"][-1]
                self.assertEqual(tune_capture["core:sample_start"], 10)
                self.assertEqual(tune_capture["core:frequency"], 437_153_000.0)
                self.assertEqual(tune_capture["maveric:event"], "doppler_command")
                gain = meta["annotations"][-1]
                self.assertEqual(gain["core:sample_start"], 10)
                self.assertEqual(gain["maveric:rx_gain_db"], 55.0)

                recorder._written = 20 * 8
                discontinuity = pmt.make_tuple(
                    pmt.from_uint64(1_720_000_001), pmt.from_double(0.1255))
                with recorder._lock:
                    recorder._record_rx_time_tags_locked(
                        [SimpleNamespace(offset=22, value=discontinuity)], nread=20)
                with open(recorder._meta_path, encoding="utf-8") as handle:
                    meta = json.load(handle)
                boundary = meta["captures"][-1]
                self.assertEqual(boundary["core:sample_start"], 22)
                self.assertEqual(boundary["maveric:event"], "rx_time_discontinuity")
                self.assertIn(".125500", boundary["core:datetime"])
            finally:
                # _written is synthetic; stop still exercises final atomic meta write.
                recorder.stop()

    def test_byte_cap_is_exact_and_excludes_tags_beyond_eof(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                os.environ, self._environment(directory), clear=False):
            recorder = MAV_ASTROCAST._IqRecorder(samp_rate=1_000.0)
            tag_ranges = []
            recorder.nitems_read = lambda _port: 0

            def fake_tags(_port, start, end, _key):
                tag_ranges.append((start, end))
                value = pmt.make_tuple(
                    pmt.from_uint64(1_720_000_000), pmt.from_double(0.5))
                return [SimpleNamespace(offset=end - 1, value=value)]

            recorder.get_tags_in_range = fake_tags
            data_path = recorder._data_path
            try:
                consumed = recorder.work(
                    [np.zeros(200, dtype=np.complex64)], [])
                self.assertEqual(consumed, 200)
                self.assertEqual(recorder._written, 800)
                self.assertEqual(os.path.getsize(data_path), 800)
                self.assertEqual(tag_ranges, [(0, 100)])
                with open(recorder._meta_path, encoding="utf-8") as handle:
                    meta = json.load(handle)
                self.assertLessEqual(
                    max(capture["core:sample_start"]
                        for capture in meta["captures"]),
                    99)
            finally:
                recorder.stop()


@unittest.skipIf(MAV_DUO is None, FLOWGRAPH_IMPORT_ERROR)
class TxAsyncMetadataTests(unittest.TestCase):
    def test_modern_uhd_pmt_events_are_counted_without_legacy_converter(self):
        monitor = MAV_DUO._TxAsyncMonitor(report_every_s=60.0)
        events = pmt.make_tuple(
            pmt.intern("underflow"),
            pmt.intern("underflow_in_packet"),
            pmt.intern("seq_error_in_burst"),
            pmt.intern("time_error"),
        )
        body = pmt.dict_add(
            pmt.make_dict(), pmt.intern("event_code"), events)
        message = pmt.cons(pmt.intern("uhd_async_msg"), body)

        monitor._handle(message)

        self.assertEqual(monitor._underflows, 2)
        self.assertEqual(monitor._sequence_errors, 1)
        self.assertEqual(monitor._time_errors, 1)
        self.assertEqual(monitor._last_event_code, 0x2 | 0x10 | 0x20 | 0x8)


if __name__ == "__main__":
    unittest.main()
