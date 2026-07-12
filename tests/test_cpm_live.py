"""Fast contract tests for the live coherent-CPM GNU Radio wrapper."""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import yaml


CODE_DIR = Path(__file__).resolve().parent.parent
GNURADIO_DIR = CODE_DIR / "gnuradio"

try:
    import pmt
    from gnuradio import gr as _gr  # noqa: F401
except (ImportError, OSError) as exc:  # pragma: no cover - GS dependency gate
    pmt = None
    cpm_live = None
    _GR_IMPORT_ERROR = exc
else:
    sys.path.insert(0, str(GNURADIO_DIR))
    import cpm_live

    _GR_IMPORT_ERROR = None


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class _IdleDetector:
    def __init__(self) -> None:
        self.feeds: list[tuple[int, int]] = []
        self.resets: list[int] = []

    def feed(self, samples, *, sample_start: int):
        self.feeds.append((int(sample_start), len(samples)))
        return []

    def reset(self, sample_start: int = 0) -> None:
        self.resets.append(int(sample_start))


@unittest.skipIf(
    _GR_IMPORT_ERROR is not None,
    f"GNU Radio/pmt unavailable: {_GR_IMPORT_ERROR}",
)
class CpmLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self._tmp.name) / "TEST_DECODER.yml"
        self.profile.write_text(
            """\
transmitters:
  primary:
    frequency: 437000000
    modulation: FSK
    baudrate: 4800
    deviation: 1200
    framing: AX100 ASM+Golay
""",
            encoding="utf-8",
        )
        self._sinks = []
        self._release_events: list[threading.Event] = []

    def tearDown(self) -> None:
        for event in self._release_events:
            event.set()
        for sink in reversed(self._sinks):
            sink.stop()
        self._tmp.cleanup()

    def _sink(self, **kwargs):
        sink = cpm_live.CpmDetectorSink(str(self.profile), **kwargs)
        sink._health = lambda *args, **kw: None
        self._sinks.append(sink)
        return sink

    @staticmethod
    def _pdu(payload: bytes):
        return pmt.cons(
            pmt.PMT_NIL,
            pmt.init_u8vector(len(payload), list(payload)),
        )

    @staticmethod
    def _result(payload: bytes, *, sample_start: int = 1000,
                sample_end: int = 2000):
        return SimpleNamespace(
            payload=payload,
            sample_start=sample_start,
            sample_end=sample_end,
            baud=4800.0,
            deviation=1200.0,
            transmitter="primary",
            cfo_hz=-425.25,
            sync_score=18.5,
        )

    def _capture(self, sink):
        published = []
        sink.message_port_pub = (
            lambda port, message: published.append((port, message)))
        return published

    def test_load_mode5_hypotheses_filters_deduplicates_and_keeps_order(self):
        profile = Path(self._tmp.name) / "matrix.yml"
        profile.write_text(
            """\
transmitters:
  first:
    modulation: fsk
    baudrate: 4800
    deviation: 1200
    framing: AX100 ASM+Golay
  duplicate_waveform:
    modulation: FSK
    baudrate: 4800.0
    deviation: 1200.0
    framing: AX100 ASM+Golay
  wrong_framing:
    modulation: FSK
    baudrate: 9600
    deviation: 3200
    framing: AX.25 G3RUH
  wrong_modulation:
    modulation: BPSK
    baudrate: 9600
    deviation: 3200
    framing: AX100 ASM+Golay
  invalid:
    modulation: FSK
    baudrate: 0
    deviation: 3200
    framing: AX100 ASM+Golay
  second:
    modulation: FSK
    baudrate: 9600
    deviation: 3200
    framing: AX100 ASM+Golay
""",
            encoding="utf-8",
        )

        hypotheses = cpm_live.load_mode5_hypotheses(profile)

        self.assertEqual(
            hypotheses,
            (
                cpm_live.LiveHypothesis(4800.0, 1200.0, "first"),
                cpm_live.LiveHypothesis(9600.0, 3200.0, "second"),
            ),
        )

    def test_start_stop_are_idempotent_and_restartable(self):
        made: list[_IdleDetector] = []
        made_event = threading.Event()

        def factory(_hypotheses):
            detector = _IdleDetector()
            made.append(detector)
            made_event.set()
            return detector

        sink = self._sink(detector_factory=factory)

        self.assertTrue(sink.start())
        first_thread = sink._thread
        self.assertTrue(sink.start())
        self.assertIs(sink._thread, first_thread)
        self.assertTrue(made_event.wait(1.0))
        self.assertEqual(len(made), 1)

        self.assertTrue(sink.stop())
        self.assertTrue(sink.stop())
        self.assertIsNone(sink._thread)

        made_event.clear()
        self.assertTrue(sink.start())
        self.assertTrue(made_event.wait(1.0))
        self.assertEqual(len(made), 2)
        self.assertTrue(sink.stop())

    def test_stop_cancels_active_worker_and_joins(self):
        entered = threading.Event()

        class CooperativeDetector(_IdleDetector):
            def feed(self, samples, *, sample_start: int):
                entered.set()
                while not sink._cancel.wait(0.01):
                    pass
                raise RuntimeError("cancelled by test")

        sink = self._sink(
            detector_factory=lambda _hypotheses: CooperativeDetector())
        sink.nitems_read = lambda _port: 0
        sink.get_tags_in_range = lambda *args, **kwargs: []
        sink.start()
        sink.work([np.ones(4, dtype=np.complex64)], [])
        self.assertTrue(entered.wait(1.0))

        started = time.monotonic()
        self.assertTrue(sink.stop())

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIsNone(sink._thread)
        self.assertFalse(sink._stop_timed_out)

    def test_work_stays_nonblocking_and_drop_forces_detector_reset(self):
        entered = threading.Event()
        release = threading.Event()
        reset_seen = threading.Event()
        self._release_events.append(release)

        class BlockingDetector(_IdleDetector):
            def feed(self, samples, *, sample_start: int):
                self.feeds.append((int(sample_start), len(samples)))
                if len(self.feeds) == 1:
                    entered.set()
                    release.wait(2.0)
                return []

            def reset(self, sample_start: int = 0) -> None:
                super().reset(sample_start)
                reset_seen.set()

        detector = BlockingDetector()
        sink = self._sink(
            detector_factory=lambda _hypotheses: detector,
            queue_limit_samples=8,
        )
        cursor = [0]
        sink.nitems_read = lambda _port: cursor[0]
        sink.get_tags_in_range = lambda *args, **kwargs: []
        sink.start()

        first = np.arange(4, dtype=np.float32).astype(np.complex64)
        self.assertEqual(sink.work([first], []), 4)
        self.assertTrue(entered.wait(1.0))

        started = time.monotonic()
        for value in (4, 8, 12):
            cursor[0] = value
            self.assertEqual(sink.work([first], []), 4)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0, "work() waited for the blocked detector")
        with sink._condition:
            self.assertLessEqual(sink._queued_samples, 8)
            self.assertEqual(
                [chunk.sample_start for chunk in sink._chunks], [8, 12])
        self.assertEqual(sink._counters["chunks_dropped"], 1)
        self.assertEqual(sink._counters["samples_dropped"], 4)

        release.set()
        self.assertTrue(reset_seen.wait(1.0))
        self.assertIn(8, detector.resets)
        self.assertTrue(_wait_until(
            lambda: sink._counters["discontinuity_resets"] == 1))

    def test_detector_factory_failure_keeps_production_pass_through(self):
        def fail_factory(_hypotheses):
            raise RuntimeError("detector unavailable")

        sink = self._sink(detector_factory=fail_factory)
        published = self._capture(sink)
        sink.start()
        self.assertTrue(_wait_until(
            lambda: sink._counters["detector_errors"] == 1))

        payload = b"production still works"
        sink._handle_production(self._pdu(payload))

        self.assertEqual(len(published), 1)
        self.assertEqual(
            bytes(pmt.u8vector_elements(pmt.cdr(published[0][1]))),
            payload,
        )
        self.assertEqual(sink._counters["production_forwarded"], 1)

    def test_real_validated_nushsat_waveform_starts_worker(self):
        self.profile.write_text(
            """\
transmitters:
  nushsat_1k2:
    modulation: FSK
    baudrate: 1200
    deviation: 575
    framing: AX100 ASM+Golay
""",
            encoding="utf-8",
        )
        made = threading.Event()

        def factory(_hypotheses):
            made.set()
            return _IdleDetector()

        sink = self._sink(detector_factory=factory)
        sink.start()

        self.assertTrue(made.wait(1.0))
        self.assertEqual(
            sink._hypotheses,
            (cpm_live.LiveHypothesis(1199.7, 575.0, "nushsat_1k2"),),
        )
        self.assertEqual(sink._skipped_hypotheses, ())
        self.assertEqual(sink._cfo_span_hz, 650.0)

    def test_unvalidated_mode5_waveform_is_inert_and_fail_open(self):
        self.profile.write_text(
            """\
transmitters:
  unvalidated_2k4:
    modulation: FSK
    baudrate: 2400
    deviation: 800
    framing: AX100 ASM+Golay
""",
            encoding="utf-8",
        )
        factory_called = False

        def factory(_hypotheses):
            nonlocal factory_called
            factory_called = True
            return _IdleDetector()

        sink = self._sink(detector_factory=factory)
        published = self._capture(sink)
        sink.start()
        sink._handle_production(self._pdu(b"verified production frame"))

        self.assertFalse(factory_called)
        self.assertIsNone(sink._thread)
        self.assertEqual(len(sink._hypotheses), 0)
        self.assertEqual(len(sink._skipped_hypotheses), 1)
        self.assertEqual(len(published), 1)

    def test_production_result_wins_cross_decoder_duplicate(self):
        sink = self._sink(detector_factory=lambda _h: _IdleDetector())
        published = self._capture(sink)
        sink.start()
        payload = b"same physical frame"
        sink._latest_stream_sample = 100_000

        sink._handle_production(self._pdu(payload))
        sink._emit_results([
            self._result(payload, sample_start=90_000, sample_end=100_100),
        ])

        self.assertEqual(len(published), 1)
        self.assertEqual(sink._counters["production_forwarded"], 1)
        self.assertEqual(sink._counters["cpm_suppressed"], 1)
        self.assertEqual(sink._counters["cpm_forwarded"], 0)

    def test_cpm_first_race_still_emits_one_pdu(self):
        sink = self._sink(detector_factory=lambda _h: _IdleDetector())
        published = self._capture(sink)
        sink.start()
        payload = b"same physical frame, CPM first"
        sink._latest_stream_sample = 100_000

        sink._emit_results([
            self._result(payload, sample_start=90_000, sample_end=100_000),
        ])
        sink._handle_production(self._pdu(payload))

        self.assertEqual(len(published), 1)
        self.assertEqual(sink._counters["cpm_forwarded"], 1)
        self.assertEqual(sink._counters["production_forwarded"], 0)
        self.assertEqual(sink._counters["production_suppressed"], 1)

    def test_two_production_branches_plus_cpm_emit_once(self):
        sink = self._sink(detector_factory=lambda _h: _IdleDetector())
        published = self._capture(sink)
        sink.start()
        payload = b"same frame from two stock hypotheses"
        sink._latest_stream_sample = 100_000

        sink._handle_production(self._pdu(payload))
        sink._handle_production(self._pdu(payload))
        sink._emit_results([
            self._result(payload, sample_start=90_000, sample_end=100_000),
        ])

        self.assertEqual(len(published), 1)
        self.assertEqual(sink._counters["production_forwarded"], 1)
        self.assertEqual(sink._counters["production_peer_suppressed"], 1)
        self.assertEqual(sink._counters["cpm_suppressed"], 1)
        self.assertEqual(len(sink._production), 0)

    def test_identical_later_beacon_is_not_symmetric_false_match(self):
        sink = self._sink(detector_factory=lambda _h: _IdleDetector())
        published = self._capture(sink)
        sink.start()
        payload = b"identical but separate beacon"
        sink._latest_stream_sample = 100_000
        sink._handle_production(self._pdu(payload))

        # A later CPM result must not match an older production cursor merely
        # because the two positions are close in absolute terms.
        sink._emit_results([
            self._result(payload, sample_start=490_000, sample_end=500_000),
        ])

        self.assertEqual(len(published), 2)
        self.assertEqual(sink._counters["cpm_suppressed"], 0)
        self.assertEqual(sink._counters["cpm_forwarded"], 1)

    def test_later_identical_payload_is_forwarded_as_a_new_beacon(self):
        sink = self._sink(detector_factory=lambda _h: _IdleDetector())
        published = self._capture(sink)
        sink.start()
        payload = b"repeating beacon"
        sink._latest_stream_sample = 100_000

        sink._handle_production(self._pdu(payload))
        sink._emit_results([
            self._result(payload, sample_start=99_000, sample_end=100_000),
        ])
        later_end = 100_000 + int(
            cpm_live.PRODUCTION_MATCH_SECONDS * sink._samp_rate) + 10_000
        sink._emit_results([
            self._result(
                payload,
                sample_start=later_end - 1000,
                sample_end=later_end,
            ),
        ])

        self.assertEqual(len(published), 2)
        self.assertEqual(sink._counters["cpm_suppressed"], 1)
        self.assertEqual(sink._counters["cpm_forwarded"], 1)
        self.assertEqual(
            bytes(pmt.u8vector_elements(pmt.cdr(published[-1][1]))), payload)

    def test_cpm_pdu_contains_required_metadata_and_payload(self):
        sink = self._sink(detector_factory=lambda _h: _IdleDetector())
        published = self._capture(sink)
        sink.start()
        payload = b"corrected RS payload"
        result = self._result(payload, sample_start=1234, sample_end=5678)

        sink._emit_results([result])

        self.assertEqual(len(published), 1)
        message = published[0][1]
        self.assertEqual(
            pmt.to_python(pmt.car(message)),
            {
                "transmitter": "primary",
                "decoder": "coherent_cpm",
                "baud": 4800.0,
                "deviation": 1200.0,
                "cfo_hz": -425.25,
                "sync_score": 18.5,
                "sample_start": 1234,
                "sample_end": 5678,
            },
        )
        self.assertEqual(
            bytes(pmt.u8vector_elements(pmt.cdr(message))), payload)

    def test_stop_prevents_delayed_cpm_publication(self):
        sink = self._sink(detector_factory=lambda _h: _IdleDetector())
        published = self._capture(sink)
        sink.start()
        sink.stop()

        sink._emit_results([self._result(b"too late")])
        sink._handle_production(self._pdu(b"production too late"))

        self.assertEqual(published, [])
        self.assertEqual(sink._counters["cpm_forwarded"], 0)
        self.assertEqual(sink._counters["production_forwarded"], 0)


class CpmFlowgraphGuards(unittest.TestCase):
    """Keep the hand-edited Python and GRC flowgraphs equivalent."""

    def test_post_fir_wiring_and_production_arbiter_match(self):
        py = (GNURADIO_DIR / "MAV_DUO.py").read_text(encoding="utf-8")
        self.assertIn("from cpm_live import CpmDetectorSink", py)
        self.assertIn(
            "self.cpm_detector = CpmDetectorSink(\n"
            "            decoder_yml=decoder_yml,\n"
            "            samp_rate=int(samp_rate/rx_decim),",
            py,
        )
        expected_py = (
            "self.connect((self.fir_filter_xxx_1, 0), (self.cpm_detector, 0))",
            "self.msg_connect((self.satellites_satellite_decoder_0, 'out'), "
            "(self.cpm_detector, 'production'))",
            "self.msg_connect((self.cpm_detector, 'out'), "
            "(self.satellites_hexdump_sink_0, 'in'))",
            "self.msg_connect((self.cpm_detector, 'out'), "
            "(self.zeromq_pub_msg_sink_0, 'in'))",
        )
        for connection in expected_py:
            self.assertIn(connection, py)
        self.assertNotIn(
            "self.msg_connect((self.satellites_satellite_decoder_0, 'out'), "
            "(self.zeromq_pub_msg_sink_0, 'in'))",
            py,
        )
        for preserved in (
            "self.connect((self.fir_filter_xxx_1, 0), (self.iq_recorder, 0))",
            "self.connect((self.fir_filter_xxx_1, 0), "
            "(self.satellites_satellite_decoder_0, 0))",
            "self.connect((self.fir_filter_xxx_1, 0), (self.waterfall_logger, 0))",
        ):
            self.assertIn(preserved, py)

        grc = yaml.safe_load(
            (GNURADIO_DIR / "MAV_DUO.grc").read_text(encoding="utf-8"))
        blocks = {block["name"]: block for block in grc["blocks"]}
        block = blocks["epy_block_cpm"]
        source = block["parameters"]["_source_code"]
        compile(source, "epy_block_cpm", "exec")
        self.assertIn("from cpm_live import CpmDetectorSink", source)
        self.assertIn("class blk(CpmDetectorSink)", source)
        self.assertEqual(block["parameters"]["decoder_yml"], "decoder_yml")
        self.assertEqual(
            block["parameters"]["samp_rate"], "int(samp_rate/rx_decim)")

        connections = {tuple(item) for item in grc["connections"]}
        for connection in (
            ("fir_filter_xxx_1", "0", "epy_block_cpm", "0"),
            ("satellites_satellite_decoder_0", "out",
             "epy_block_cpm", "production"),
            ("epy_block_cpm", "out", "satellites_hexdump_sink_0", "in"),
            ("epy_block_cpm", "out", "zeromq_pub_msg_sink_0", "in"),
        ):
            self.assertIn(connection, connections)
        self.assertNotIn(
            ("satellites_satellite_decoder_0", "out",
             "zeromq_pub_msg_sink_0", "in"),
            connections,
        )
        for preserved in (
            ("fir_filter_xxx_1", "0", "epy_block_iq", "0"),
            ("fir_filter_xxx_1", "0", "epy_block_waterfall", "0"),
            ("fir_filter_xxx_1", "0",
             "satellites_satellite_decoder_0", "0"),
        ):
            self.assertIn(preserved, connections)


if __name__ == "__main__":
    unittest.main()
