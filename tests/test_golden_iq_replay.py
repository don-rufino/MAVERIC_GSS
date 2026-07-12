"""Golden real-IQ replay through the exact production decode paths.

Local-only operational tests, NOT a CI gate (the synthetic loopback suites
are the gate): they replay trimmed slices of real recorded passes and
assert the frames decoded live decode again. Fixtures live in
tests/fixtures/iq/ (gitignored — machine-local, like beacon_samples/):

    roads2_first_frame_200k.cf32   ROADS 2 first orbital ASM+Golay decode
                                   (2026-07-11, 4k8 Mode 5) through
                                   gr_satellites_flowgraph + ROADS_DECODER.yml
    astrocast_beacon{1,2,3}_200k.cf32
                                   the three Astrocast FX.25 beacons decoded
                                   on the 2026-07-11 pass, through
                                   MAV_ASTROCAST --iqfile (full live banks)

Each .cf32 has a .json sidecar: sample_rate, source capture + window,
and expected_frames_hex (what the replay must reproduce). Tests skip when
a fixture is absent; with the fixture present and MAVERIC_FULL_GR=1, any
missing frame FAILS. No SharjahSat golden yet — no recorded pass has
decoded; add one after the next successful pass.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

import numpy as np

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

from test_decode_loopback import GNURADIO, _run_decoder, _requires_full_gr  # noqa: E402
from test_astrocast_loopback import replay_iq_through_banks  # noqa: E402

FIXTURES = TESTS_DIR / "fixtures" / "iq"


def _fixture(name: str) -> tuple[Path, dict]:
    cf32 = FIXTURES / f"{name}.cf32"
    if not cf32.is_file():
        raise unittest.SkipTest(
            f"local golden fixture {cf32} not present on this machine")
    meta = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return cf32, meta


class GoldenIqReplayTests(unittest.TestCase):
    """Recorded passes must keep decoding through the production chains."""

    maxDiff = None

    def setUp(self) -> None:
        _requires_full_gr()

    def test_roads2_first_frame_still_decodes(self):
        cf32, meta = _fixture("roads2_first_frame_200k")
        record = np.fromfile(cf32, dtype=np.complex64)
        expected = {bytes.fromhex(h) for h in meta["expected_frames_hex"]}
        pdus = _run_decoder(GNURADIO / "ROADS_DECODER.yml", "--syncword_threshold 6",
                            record, wait_s=120)
        missing = expected - set(pdus)
        self.assertFalse(
            missing,
            f"golden ROADS 2 frame(s) no longer decode: "
            f"{[m[:16].hex() for m in missing]} (got {len(pdus)} PDUs)")

    def test_astrocast_pass_beacons_still_decode(self):
        for i, name in enumerate(
                ("astrocast_beacon1_200k", "astrocast_beacon2_200k",
                 "astrocast_beacon3_200k")):
            with self.subTest(fixture=name):
                cf32, meta = _fixture(name)
                expected = {bytes.fromhex(h) for h in meta["expected_frames_hex"]}
                frames = replay_iq_through_banks(cf32, zmq_port=52088 + i)
                missing = expected - set(frames)
                self.assertFalse(
                    missing,
                    f"{name}: golden beacon(s) no longer decode "
                    f"(got {len(frames)} PDUs)")


if __name__ == "__main__":
    unittest.main()
