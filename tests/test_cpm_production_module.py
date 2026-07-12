"""Focused guards for the production CPM detector and streaming core."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "gnuradio" / "cpm_detector.py"
SPEC = importlib.util.spec_from_file_location("production_cpm_detector", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
CPM = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CPM
SPEC.loader.exec_module(CPM)

from mav_gss_lib.platform.framing.asm_golay import (  # noqa: E402
    ASM, PREAMBLE, build_asm_golay_frame)


@unittest.skipUnless(CPM._libfec is not None, "libfec is required")
class CpmProductionModuleTests(unittest.TestCase):

    def test_cancellation_interrupts_cfo_search_and_viterbi(self):
        class CancelAfter:
            def __init__(self, calls):
                self.limit = calls
                self.calls = 0

            def __call__(self):
                self.calls += 1
                return self.calls >= self.limit

        rng = np.random.default_rng(99)
        record = (rng.standard_normal(300_000)
                  + 1j * rng.standard_normal(300_000)).astype(np.complex64)
        cfo_cancel = CancelAfter(3)
        with self.assertRaises(CPM.CpmCancelled):
            CPM.acquire(record, 1200, 575, max_candidates=8,
                        cfo_span_hz=650, cancelled=cfo_cancel)
        self.assertGreaterEqual(cfo_cancel.calls, 3)

        model = CPM.CpmModel.build(4800, 1200)
        head = np.unpackbits(np.frombuffer(PREAMBLE + ASM, np.uint8))[-56:]
        grid = np.ones((len(head) + 132) * CPM.GRID_SPS,
                       dtype=np.complex128)
        viterbi_cancel = CancelAfter(14)
        with self.assertRaises(CPM.CpmCancelled):
            CPM.viterbi_demod(model, grid, 128, head,
                               cancelled=viterbi_cancel)
        self.assertGreaterEqual(viterbi_cancel.calls, 14)

    def test_stream_stored_and_override_cancellation(self):
        hypothesis = CPM.Hypothesis(9600, 3200, "MAVERIC")
        detector = CPM.BurstStreamDetector(
            [hypothesis], cfo_span_hz=650,
            cancelled=lambda: True)
        with self.assertRaises(CPM.CpmCancelled):
            detector.feed(np.zeros(1_000, np.complex64))
        self.assertEqual(detector.buffered_samples, 0)

        detector = CPM.BurstStreamDetector(
            [hypothesis], cfo_span_hz=650,
            cancelled=lambda: False)
        detector.feed(np.zeros(1_000, np.complex64))
        with self.assertRaises(CPM.CpmCancelled):
            detector.finish(cancelled=lambda: True)
        self.assertEqual(detector.buffered_samples, 1_000)

    def test_imports_when_gnuradio_is_process_cwd(self):
        proc = subprocess.run(
            [sys.executable, "-c", "import cpm_detector; print(cpm_detector.FS)"],
            cwd=ROOT / "gnuradio", capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "200000")

    def test_wide_cfo_acquisition_and_noise_gate(self):
        baud, deviation, cfo = 1200.0, 575.0, 8_700.0
        self.assertIn(0.0, CPM._fine_cfo_bins([0.0], 650.0))
        self.assertIn(cfo, CPM._fine_cfo_bins([cfo], 10_000.0))
        reference = CPM.gfsk_iq(PREAMBLE + ASM, baud, deviation)
        gap = np.zeros(30_000, dtype=np.complex64)
        record = np.concatenate((gap, reference, gap))
        record *= np.exp(2j * np.pi * cfo * np.arange(len(record)) / CPM.FS)
        found = CPM.acquire(record, baud, deviation, max_candidates=1,
                            cfo_span_hz=10_000)
        self.assertEqual(len(found), 1)
        self.assertLessEqual(abs(found[0][0] - len(gap)), 2 * CPM.ACQ_DECIM)
        self.assertLessEqual(abs(found[0][1] - cfo), CPM.FINE_CFO_STEP_HZ)

        rng = np.random.default_rng(4)
        noise = (rng.standard_normal(300_000)
                 + 1j * rng.standard_normal(300_000)).astype(np.complex64)
        dec = noise[::CPM.ACQ_DECIM]
        ref_dec = reference[::CPM.ACQ_DECIM]
        centres = CPM._wide_cfo_centres(
            dec, ref_dec, len(dec) - len(ref_dec) + 1, 10_000)
        self.assertEqual(centres, [])

    def test_clean_nushsat_1k2_streaming_finish(self):
        payload = b"NUSHSAT-CPM-STREAM".ljust(40, b"\x69")
        burst = CPM.gfsk_iq(build_asm_golay_frame(payload), 1200, 575)
        prefix = np.zeros(100_000, dtype=np.complex64)
        suffix = np.zeros(100_000, dtype=np.complex64)
        record = np.concatenate((prefix, burst, suffix)).astype(np.complex64)
        base = 2_000_000
        detector = CPM.BurstStreamDetector(
            [CPM.Hypothesis(1200, 575, "NUSHSAT-1")],
            cfo_span_hz=650,
            max_candidates=2,
        )
        emitted = []
        cursor = 0
        sizes = (1_000, 7_777, 31_337, 4_096, 65_000)
        turn = 0
        while cursor < len(record):
            size = min(sizes[turn % len(sizes)], len(record) - cursor)
            emitted.extend(detector.feed(
                record[cursor:cursor + size],
                sample_start=(base if cursor == 0 else None),
            ))
            cursor += size
            turn += 1
        self.assertEqual(emitted, [])  # shorter than the full rolling window
        emitted = detector.finish()
        self.assertEqual(len(emitted), 1)
        result = emitted[0]
        self.assertEqual(result.payload, payload)
        self.assertEqual((result.baud, result.deviation, result.transmitter),
                         (1200.0, 575.0, "NUSHSAT-1"))
        self.assertLessEqual(
            abs(result.sample_start - (base + len(prefix))),
            2 * CPM.ACQ_DECIM,
        )
        self.assertGreater(result.sample_end, result.sample_start)
        self.assertEqual(detector.buffered_samples, 0)

    def test_model_mismatch_phase_loop_is_isolated_from_proven_modes(self):
        roads = CPM.CpmModel.build(4800, 1200)
        maveric = CPM.CpmModel.build(9600, 3200)
        nushsat = CPM.CpmModel.build(1200, 575)

        self.assertEqual(
            CPM._phase_loop_alphas(roads),
            (CPM.SLOW_PHASE_LOOP_ALPHA,),
        )
        self.assertEqual(
            CPM._phase_loop_alphas(maveric),
            (CPM.SLOW_PHASE_LOOP_ALPHA,),
        )
        self.assertEqual(
            CPM._phase_loop_alphas(nushsat),
            (CPM.MODEL_MISMATCH_PHASE_LOOP_ALPHA,
             CPM.SLOW_PHASE_LOOP_ALPHA),
        )

    def test_overlap_dedup_and_discontinuity_reset(self):
        hypothesis = CPM.Hypothesis(9600, 3200, "MAVERIC")
        detector = CPM.BurstStreamDetector(
            [hypothesis], cfo_span_hz=650, max_candidates=1)
        physical_start = 50_000

        def fake_detect(record, selected, **kwargs):
            return [CPM.BurstResult(
                payload=b"same-frame",
                sample_start=physical_start,
                sample_end=physical_start + 100,
                baud=selected.baud,
                deviation=selected.deviation,
                transmitter=selected.transmitter,
                cfo_hz=12.0,
                sync_score=9.0,
            )]

        total = detector.window_samples + detector.hop_samples
        with mock.patch.object(CPM, "detect_bursts", side_effect=fake_detect):
            emitted = detector.feed(np.zeros(total, np.complex64))
            emitted.extend(detector.finish())
        self.assertEqual(len(emitted), 1)

        detector.feed(np.zeros(20, np.complex64), sample_start=900_000)
        self.assertEqual(detector.next_sample_start, 900_020)
        self.assertEqual(detector.buffered_samples, 20)
        detector.reset(next_sample_start=1_000_000)
        self.assertEqual(detector.next_sample_start, 1_000_000)
        self.assertEqual(detector.buffered_samples, 0)

    def test_real_nushsat_decoded_burst(self):
        root = Path(os.environ.get(
            "MAVERIC_NUSHSAT_AUDIT_DIR", "/private/tmp/nushsat_jul12_audit"))
        iq_path = root / "iq_nushsat1_20260712T065241Z.sigmf-data"
        log_path = root / "session_20260711_235208_nushsat1_d23ll-barnhart_irfan.jsonl"
        if not iq_path.is_file() or not log_path.is_file():
            self.skipTest("local NUSHSat IQ audit files are absent")
        rows = [json.loads(line) for line in log_path.read_text().splitlines()]
        expected = bytes.fromhex(next(
            row["inner_hex"] for row in rows
            if row.get("event_kind") == "rx_packet" and row.get("seq") == 5))
        first = int(235.2 * CPM.FS)
        last = int(238.4 * CPM.FS)
        iq = np.memmap(iq_path, dtype="<c8", mode="r")[first:last]
        detector = CPM.BurstStreamDetector(
            [CPM.Hypothesis(1199.7, 575, "NUSHSAT-1")],
            max_candidates=8,
            cfo_span_hz=650,
        )
        results = []
        chunk_samples = 32_768
        for offset in range(0, len(iq), chunk_samples):
            results.extend(detector.feed(
                iq[offset:offset + chunk_samples],
                sample_start=(first if offset == 0 else None),
            ))
        results.extend(detector.finish())
        self.assertIn(expected, {result.payload for result in results})


if __name__ == "__main__":
    unittest.main()
