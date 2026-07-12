"""Correctness guards for the coherent CPM detector prototype (tranche 5b).

Gated behind MAVERIC_FULL_GR=1 alongside the loopback suites (pure
numpy/libfec — no GNU Radio — but several seconds of DSP per test).
Once enabled, failures FAIL.

Measured verdict (2026-07-11, paired seeds, impaired channel: random
carrier phase, ±10 ppm clock, ±500 Hz CFO, 32 trials/point, seed
20260711; final per-survivor-processing detector):

    ROADS 4k8/1200:   production p50 7.78 dB  ->  CPM p50 3.25 dB  (+4.5)
    MAVERIC 9k6/3200: production p50 6.57 dB  ->  CPM p50 2.00 dB  (+4.6)
    REAL IQ (ROADS 2 golden burst + noise): production +1.0 dB margin,
    CPM +5.0 dB  ->  +4.0 dB confirmed on the on-orbit waveform.

Deterministic (repeat points identical), zero false payloads from 10 s of
pure noise, and the p50s sit where coherent-CPM theory + RS(16-error)
predicts. These tests pin the correctness floor that made those numbers
credible — the curves themselves are re-measured with the fer_harness
CLI, not asserted here.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import numpy as np

TESTS_DIR = Path(__file__).resolve().parent
CODE_DIR = TESTS_DIR.parent
GNURADIO_DIR = CODE_DIR / "gnuradio"
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(GNURADIO_DIR))

from test_decode_loopback import FS, _gfsk_iq, _requires_full_gr  # noqa: E402


class CpmDetectorTests(unittest.TestCase):

    def setUp(self) -> None:
        _requires_full_gr()

    def test_model_matches_production_synthesis(self):
        # Branch tables must reconstruct the very waveform _gfsk_iq
        # synthesizes (the wire format), to within the fractional-sps
        # discretization jitter of the FS synthesis itself.
        from cpm_detector import GRID_SPS, CpmModel, reconstruct_grid_waveform
        rng = np.random.default_rng(7)
        for baud, dev, bound in ((4800, 1200, 0.15), (9600, 3200, 0.30)):
            model = CpmModel.build(baud, dev)
            bits = rng.integers(0, 2, 64)
            direct = _gfsk_iq(np.packbits(bits).tobytes(), baud, dev)
            step = FS / (baud * GRID_SPS)
            pos = np.arange(len(bits) * GRID_SPS) * step
            base = np.arange(len(direct))
            grid = (np.interp(pos, base, direct.real)
                    + 1j * np.interp(pos, base, direct.imag))
            recon = reconstruct_grid_waveform(model, bits)
            a = grid[4 * GRID_SPS:(len(bits) - 6) * GRID_SPS]
            b = recon[2 * GRID_SPS:][:len(a)]
            rot = (a * np.conj(b))
            err = np.angle(rot * np.exp(-1j * np.angle(rot.sum())))
            self.assertLess(float(np.max(np.abs(err))), bound,
                            f"{baud}/{dev}: model diverged from synthesis")

    def test_blind_decode_clean_bursts_both_indices(self):
        from mav_gss_lib.platform.framing.asm_golay import build_asm_golay_frame
        from cpm_detector import detect_and_decode
        rng = np.random.default_rng(11)
        for baud, dev in ((4800, 1200), (9600, 3200)):
            payload = bytes([0xC1]) + rng.bytes(39)
            burst = _gfsk_iq(build_asm_golay_frame(payload), baud, dev)
            gap = np.zeros(int(0.1 * FS), dtype=complex)
            record = np.concatenate([gap, burst * np.exp(1j * 1.234), gap])
            noise = 0.02 * (rng.standard_normal(record.size)
                            + 1j * rng.standard_normal(record.size)) / np.sqrt(2)
            out = detect_and_decode(record + noise, baud, dev, max_candidates=4)
            self.assertIn(payload, out, f"{baud}/{dev}: clean burst not decoded")

    def test_low_baud_acquisition_finds_each_high_duty_sync(self):
        """Long 1k2/2k4 correlation shelves must not poison CFAR.

        Two closely spaced sync references deliberately occupy most of the
        record.  The old global median/MAD threshold treated their broad
        sidelobes as noise and placed the threshold above both true peaks.
        Requiring both candidates also prevents a one-candidate fallback
        from hiding a threshold regression.
        """
        from cpm_detector import ACQ_DECIM, FS, _sync_reference, acquire

        rng = np.random.default_rng(20260712)
        gap = np.zeros(int(0.04 * FS), dtype=np.complex128)
        for baud, dev in ((1200, 575), (2400, 600), (2400, 800)):
            with self.subTest(baud=baud, deviation=dev):
                reference = _sync_reference(baud, dev)
                record = np.concatenate((
                    gap,
                    reference,
                    gap,
                    reference * np.exp(0.7j),
                    gap,
                ))
                record *= np.exp(
                    2j * np.pi * -425.0 * np.arange(record.size) / FS)
                record += 0.02 * (
                    rng.standard_normal(record.size)
                    + 1j * rng.standard_normal(record.size)
                ) / np.sqrt(2.0)

                candidates = acquire(
                    record, baud, dev, max_candidates=6,
                    cfo_span_hz=650.0)
                starts = [start for start, _ in candidates]
                expected = (len(gap), 2 * len(gap) + len(reference))
                for target in expected:
                    self.assertTrue(
                        any(abs(start - target) <= 2 * ACQ_DECIM
                            for start in starts),
                        f"{baud}/{dev}: no candidate near {target}; "
                        f"got {candidates}",
                    )

    def test_clean_nushsat_profiles_decode_at_measured_residual_cfo(self):
        from mav_gss_lib.platform.framing.asm_golay import (
            ASM, PREAMBLE, RS_PARITY, build_asm_golay_frame,
        )
        from cpm_detector import FS, detect_and_decode, gfsk_iq

        rng = np.random.default_rng(575)
        for baud, dev in ((1200, 575), (2400, 600), (2400, 800)):
            with self.subTest(baud=baud, deviation=dev):
                payload = bytes([0xD1]) + rng.bytes(39)
                wire = build_asm_golay_frame(payload)
                variable_len = (
                    len(PREAMBLE) + len(ASM) + 3
                    + len(payload) + RS_PARITY
                )
                burst = gfsk_iq(wire[:variable_len], baud, dev)
                t = np.arange(burst.size) / FS
                burst *= np.exp(1j * (0.9 + 2 * np.pi * -425.0 * t))
                gap = np.zeros(int(0.1 * FS), dtype=np.complex128)
                record = np.concatenate((gap, burst, gap))
                record += 0.02 * (
                    rng.standard_normal(record.size)
                    + 1j * rng.standard_normal(record.size)
                ) / np.sqrt(2.0)

                decoded = detect_and_decode(
                    record, baud, dev, max_candidates=4,
                    cfo_span_hz=650.0)
                self.assertIn(payload, decoded)

    def test_wide_cfo_acquisition_reaches_locating_edges(self):
        from cpm_detector import ACQ_DECIM, FS, _sync_reference, acquire

        rng = np.random.default_rng(10_000)
        cases = (
            (1200, 575, -9_750.0),
            (2400, 600, 8_400.0),
            (9600, 3200, 9_750.0),
        )
        gap = np.zeros(int(0.1 * FS), dtype=np.complex128)
        for baud, dev, cfo_hz in cases:
            with self.subTest(baud=baud, deviation=dev, cfo_hz=cfo_hz):
                reference = _sync_reference(baud, dev)
                record = np.concatenate((gap, reference, gap))
                record *= np.exp(
                    2j * np.pi * cfo_hz * np.arange(record.size) / FS)
                record += 0.02 * (
                    rng.standard_normal(record.size)
                    + 1j * rng.standard_normal(record.size)
                ) / np.sqrt(2.0)

                candidates = acquire(
                    record, baud, dev, max_candidates=4,
                    cfo_span_hz=10_000.0)
                self.assertTrue(
                    any(abs(start - len(gap)) <= 2 * ACQ_DECIM
                        and abs(coarse_cfo - cfo_hz) <= 100.0
                        for start, coarse_cfo in candidates),
                    f"{baud}/{dev} at {cfo_hz:+.0f} Hz: {candidates}",
                )

    def test_impaired_channel_decodes_at_high_snr(self):
        # Full benchmark impairments — random carrier phase, ±10 ppm clock,
        # ±500 Hz CFO — must not cost decodes at 20 dB Eb/N0.
        from fer_harness import make_channel_set
        from cpm_detector import run_point_mode5_cpm
        ch = make_channel_set(8, 20260711, random_phase=True,
                              clock_ppm_span=10.0)
        for baud, dev in ((4800, 1200), (9600, 3200)):
            pt = run_point_mode5_cpm(baud, dev, 20.0, ch)
            self.assertEqual((pt.decoded, pt.trials), (8, 8),
                             f"{baud}/{dev}: impaired high-SNR decode failed")

    def test_pure_noise_yields_no_payloads(self):
        from cpm_detector import detect_and_decode
        rng = np.random.default_rng(99)
        noise = 3.0 * (rng.standard_normal(1_000_000)
                       + 1j * rng.standard_normal(1_000_000)) / np.sqrt(2)
        for baud, dev in ((4800, 1200), (9600, 3200)):
            self.assertEqual(detect_and_decode(noise, baud, dev,
                                               max_candidates=32), [],
                             f"{baud}/{dev}: false payload from pure noise")

    def test_low_baud_noise_yields_no_payloads(self):
        from cpm_detector import FS, detect_and_decode

        rng = np.random.default_rng(575575)
        noise = 3.0 * (
            rng.standard_normal(FS) + 1j * rng.standard_normal(FS)
        ) / np.sqrt(2.0)
        for baud, dev in ((1200, 575), (2400, 600), (2400, 800)):
            with self.subTest(baud=baud, deviation=dev):
                self.assertEqual(
                    detect_and_decode(noise, baud, dev,
                                      max_candidates=8,
                                      cfo_span_hz=650.0),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
