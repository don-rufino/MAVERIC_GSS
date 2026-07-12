"""Coarse FER regression gate for the production Mode 5 decode chains.

Gated behind MAVERIC_FER=1 — its own tier above MAVERIC_FULL_GR because a
run costs minutes, not seconds. Once enabled, failures FAIL.

Each gate sweeps 3 Eb/N0 points x 32 trials through fer_harness (frozen
channel set, seed 20260711) and interpolates the 50%-decode threshold.
The harness is fully deterministic (measured run-to-run p50 delta =
0.000 dB at 48 trials/point), so the stored baselines reproduce exactly
until the DSP chain actually changes. The +/-0.5 dB tolerance therefore
only absorbs deliberate small chain edits — after a real improvement,
re-measure and update the constants (fer_harness CLI, same config).

Baselines measured 2026-07-11 on this machine (EOF-correct harness):

    ROADS_DECODER.yml   4k8 dev 1200:
        48-trial curve p50 = 8.14 dB Eb/N0 (Carson CNR 6.38 dB) — matches
        the ~6 dB sustained-CNR threshold measured on the real ROADS 2
        waveform; p90 = 8.94 dB.
    MAVERIC_DECODER.yml 9k6 dev 3200:
        48-trial curve p50 = 6.75 dB Eb/N0 (Carson CNR 4.53 dB),
        p90 = 7.52 dB. ~1.4 dB better than the 4k8 h=0.5 chain per bit,
        consistent with discriminator gain scaling ~h^2 net of the wider
        Carson bandwidth.

Detector prototypes (impulse blanker, coherent CPM) are judged by
FER-curve separation on fer_harness at matched seeds, not by this gate.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

from fer_harness import interp_threshold, sweep_mode5  # noqa: E402

GATE_TRIALS = 32
TOLERANCE_DB = 0.5

# Gate-config baselines (3 points x 32 trials, seed 20260711) — measured
# with the exact sweep each test below runs, so a healthy chain reproduces
# them to the millibel.
ROADS_GATE_POINTS = [7.5, 8.25, 9.0]
ROADS_GATE_P50_DB = 8.20      # 2/32, 17/32, 28/32
MAVERIC_GATE_POINTS = [6.0, 6.75, 7.5]
MAVERIC_GATE_P50_DB = 6.625   # 1/32, 19/32, 31/32


def _requires_fer() -> None:
    if os.environ.get("MAVERIC_FER") != "1":
        raise unittest.SkipTest("set MAVERIC_FER=1 to run the FER regression gate")


class FerBaselineTests(unittest.TestCase):

    def setUp(self) -> None:
        _requires_fer()

    def _gate(self, db: str, baud: float, dev: float,
              points: list[float], baseline_db: float) -> None:
        curve = sweep_mode5(db, "--syncword_threshold 6", baud, dev,
                            points, GATE_TRIALS, log=lambda *_: None)
        p50 = interp_threshold(curve, 0.5)
        detail = ", ".join(f"{pt.ebn0_db:g} dB: {pt.decoded}/{pt.trials}"
                           for pt in curve)
        self.assertIsNotNone(
            p50,
            f"{db} {baud:g}/{dev:g}: 50% threshold no longer bracketed by "
            f"{points} — the chain moved by more than the sweep span "
            f"({detail})")
        self.assertLessEqual(
            p50, baseline_db + TOLERANCE_DB,
            f"{db} {baud:g}/{dev:g}: sensitivity REGRESSED — p50 {p50:.2f} dB "
            f"vs baseline {baseline_db:.2f} dB ({detail})")
        self.assertGreaterEqual(
            p50, baseline_db - TOLERANCE_DB,
            f"{db} {baud:g}/{dev:g}: p50 {p50:.2f} dB is better than baseline "
            f"{baseline_db:.2f} dB by more than the tolerance — if the chain "
            f"deliberately improved, re-measure and update the baseline "
            f"({detail})")

    def test_roads_4k8_h05_chain_holds_baseline(self):
        self._gate("ROADS_DECODER.yml", 4800, 1200,
                   ROADS_GATE_POINTS, ROADS_GATE_P50_DB)

    def test_maveric_9k6_auto_default_chain_holds_baseline(self):
        self._gate("MAVERIC_DECODER.yml", 9600, 3200,
                   MAVERIC_GATE_POINTS, MAVERIC_GATE_P50_DB)


if __name__ == "__main__":
    unittest.main()
