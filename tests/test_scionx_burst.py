"""SCION-X burst decoder (gnuradio/scionx_burst.py).

Synthetic bursts are built from the published ground-test frame only (the
beacon field map is team-supplied and not in the repository). Replays of
real captures run where the local files exist.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "gnuradio"))

try:
    import scionx_burst as sb
except ImportError as exc:                       # scipy lives in radioconda
    sb = None
    _IMPORT_ERROR = str(exc)

FS = 200_000.0
FIELDS = ROOT / "mav_gss_lib" / "missions" / "scionx" / "beacon_fields.json"
N6RFM = ROOT / "scripts" / "scionx_phase0" / "data" / "scionX_50000SPS_437500000Hz_2026_09_28_T15-04-31Z_snip.iq"
GS_0923 = ROOT / "logs" / "iq" / "iq_maveric_20260923T063435Z.sigmf-data"
GS_1013 = ROOT / "logs" / "iq" / "iq_scionx_20261013T062944Z.sigmf-data"


def _stuff(bits, skip_at=None):
    out, ones = [], 0
    for i, b in enumerate(bits):
        out.append(int(b))
        ones = ones + 1 if b else 0
        if ones == 5:
            if i != skip_at:
                out.append(0)
            ones = 0
    return np.array(out, dtype=np.uint8)


def _frame_bits(*, drop_bytes=0, skip_stuffing=False, drop_zero_at_byte=None):
    frame = sb.REFERENCE + sb.crc16_x25(sb.REFERENCE).to_bytes(2, "little")
    bits = sb.bytes_to_bits(frame)
    if drop_zero_at_byte is not None:            # the satellite's bug: a 0 lost from a long zero run
        i = drop_zero_at_byte * 8
        assert not bits[i:i + sb.SLIP_MIN_RUN].any(), "not inside a long zero run"
        bits = np.delete(bits, i)
    skip = None
    if skip_stuffing:                            # the satellite's bug: first eligible site left unstuffed
        ones = 0
        for i, b in enumerate(bits):
            ones = ones + 1 if b else 0
            if ones == 5:
                if i + 1 < len(bits) and bits[i + 1] == 0:
                    skip = i
                    break
                ones = 0
    body = _stuff(bits, skip)
    tail = np.tile(sb.FLAG, 10)
    if drop_bytes:                               # late key-up
        return np.concatenate([body[drop_bytes * 8:], tail]), frame, skip
    return np.concatenate([np.tile(sb.FLAG, 12), body, tail]), frame, skip


def _gfsk(bits, dev=2400.0, bt=0.5):
    sps_ = FS / sb.BAUD
    n = int(len(bits) * sps_)
    sym = 2.0 * bits[np.minimum((np.arange(n) / sps_).astype(int), len(bits) - 1)] - 1
    t = np.arange(-2 * sps_, 2 * sps_ + 1) / sps_
    sigma = np.sqrt(np.log(2)) / (2 * np.pi * bt)
    g = np.exp(-t ** 2 / (2 * sigma ** 2))
    g /= g.sum()
    f = np.convolve(sym, g, "same") * dev
    return np.exp(2j * np.pi * np.cumsum(f) / FS)


def _capture(bits, *, snr_db=16.0, doppler=7300.0, gap_s=0.6, seed=1):
    rng = np.random.default_rng(seed)
    gap = np.zeros(int(gap_s * FS), dtype=complex)
    x = np.concatenate([gap, _gfsk(bits), gap])
    x = x * np.exp(2j * np.pi * doppler * np.arange(len(x)) / FS)
    noise_p = 10 ** (-snr_db / 10) * FS / 9600          # SNR in a 9.6 kHz bandwidth
    x = x + np.sqrt(noise_p / 2) * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
    return x.astype(np.complex64)


def _decode_only_burst(x, scorer=None):
    bursts = sb.find_bursts(x, FS)
    assert len(bursts) == 1, bursts
    a, b, snr = bursts[0]
    return sb.decode_burst(x, FS, a, b, snr, scorer)


@unittest.skipIf(sb is None, "scipy not available (run under radioconda)")
class SyntheticBurstTests(unittest.TestCase):
    def test_complete_frame_passes_crc(self):
        bits, frame, _ = _frame_bits()
        r = _decode_only_burst(_capture(bits))
        self.assertEqual(r.status, "crc_pass")
        self.assertEqual(r.frame, frame)
        self.assertEqual(r.frame[:16], sb.HEADER)
        self.assertAlmostEqual(r.bit_rate, 9600, delta=5)
        self.assertEqual(r.restored_zeros_at, ())

    def test_dropped_zero_in_long_run_complete_frame_passes_crc(self):
        bits, frame, _ = _frame_bits(drop_zero_at_byte=230)
        r = _decode_only_burst(_capture(bits))
        self.assertEqual(r.status, "crc_pass")
        self.assertEqual(r.frame, frame)
        self.assertEqual(len(r.restored_zeros_at), 1)
        self.assertTrue(205 <= r.restored_zeros_at[0] <= 230, r.restored_zeros_at)

    def test_late_key_up_recovers_the_received_tail(self):
        bits, frame, _ = _frame_bits(drop_bytes=48)
        r = _decode_only_burst(_capture(bits))
        self.assertEqual(r.status, "unverified")
        self.assertEqual(r.crc, "unverifiable")
        self.assertEqual(r.start_byte, 48)
        self.assertEqual(r.frame, frame[48:])

    def test_skipped_stuffing_bit_complete_frame_passes_crc(self):
        bits, frame, skip = _frame_bits(skip_stuffing=True)
        self.assertIsNotNone(skip)
        r = _decode_only_burst(_capture(bits))
        self.assertEqual(r.status, "crc_pass")
        self.assertEqual(r.frame, frame)
        self.assertTrue(r.keep)                   # the unstuffed 0 was kept as data

    def test_clock_holds_through_sparse_late_key_up(self):
        # key-up 100 bytes in: mostly zero runs, few transitions — the case
        # a seeded clock fit used to miscount (Phase 0 synthetic failure).
        # A clock slip corrupts dozens of bytes; a stray demod bit error on
        # an unverifiable frame costs at most one.
        for seed in (1, 2, 3, 4):
            bits, frame, _ = _frame_bits(drop_bytes=100)
            r = _decode_only_burst(_capture(bits, seed=seed))
            self.assertEqual(r.status, "unverified", seed)
            self.assertEqual(r.start_byte, 100, seed)
            wrong = sum(1 for i, v in enumerate(r.frame) if v != frame[100 + i])
            self.assertLessEqual(wrong, 1, seed)
            self.assertAlmostEqual(r.bit_rate, 9600, delta=3, msg=seed)

    def test_noise_only_capture_has_no_bursts(self):
        rng = np.random.default_rng(5)
        x = (rng.standard_normal(int(FS)) + 1j * rng.standard_normal(int(FS))).astype(np.complex64)
        self.assertEqual(sb.find_bursts(x, FS), [])

    def test_meta_is_json_ready(self):
        import json
        bits, _, _ = _frame_bits(drop_bytes=48)
        r = _decode_only_burst(_capture(bits))
        meta = json.loads(json.dumps(r.meta()))
        self.assertEqual(meta["status"], "unverified")
        self.assertEqual(meta["start_byte"], 48)


@unittest.skipIf(sb is None, "scipy not available (run under radioconda)")
class BurstCollectorTests(unittest.TestCase):
    def test_live_stream_yields_the_same_decode_as_the_capture(self):
        bits, frame, _ = _frame_bits(drop_bytes=48)
        x = _capture(bits, gap_s=2.0)
        col = sb.BurstCollector(FS)
        segments = []
        for i in range(0, len(x), 4096):
            segments += col.push(x[i:i + 4096])
        self.assertEqual(len(segments), 1)
        seg, seg_start, burst_end = segments[0]
        results = sb.decode_segment(seg, FS)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][2].frame, frame[48:])
        burst_s = len(bits) / sb.BAUD
        self.assertAlmostEqual(burst_end / FS, 2.0 + burst_s, delta=0.05)

    def test_endless_carrier_is_not_a_burst(self):
        rng = np.random.default_rng(3)
        n = int(8 * FS)
        x = 0.003 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
        x[int(1.5 * FS):] += 0.1 * np.exp(2j * np.pi * 2000 * np.arange(n - int(1.5 * FS)) / FS)
        col = sb.BurstCollector(FS)
        got = []
        for i in range(0, n, 8192):
            got += col.push(x[i:i + 8192].astype(np.complex64))
        self.assertEqual(got, [])


@unittest.skipIf(sb is None, "scipy not available (run under radioconda)")
class FieldScorerTests(unittest.TestCase):
    def test_rail_nominals_come_from_names(self):
        self.assertEqual(sb.rail_nominal_v("EPS_RAILA3V3 Voltage"), 3.3)
        self.assertEqual(sb.rail_nominal_v("EPS_RAILB7V Voltage"), 7.0)
        self.assertEqual(sb.rail_nominal_v("EPS_RAILC3.3V Voltage"), 3.3)
        self.assertIsNone(sb.rail_nominal_v("EPS_PANEL0 Voltage"))
        self.assertIsNone(sb.rail_nominal_v("EPS_Pack Voltage"))

    def test_scorer_flags_impossible_values(self):
        fields = [sb.BeaconField("EPS_RAILB7V Voltage", "uint16_t", 2, 0),
                  sb.BeaconField("EPS_PANEL0 Temperature", "int8_t", 1, 2)]
        scorer = sb.FieldScorer(fields)
        good = {16: 0x38, 17: 0x1B, 18: 7}               # 6.968 V, 7 degC
        self.assertEqual(scorer(good), (0, 0))
        bad = {16: 0x39, 17: 0x36, 18: 0x9B}             # low bits set, 13.9 V, -101 degC
        ina, implausible = scorer(bad)
        self.assertEqual(ina, 1)
        self.assertEqual(implausible, 2)

    def test_missing_field_map_means_no_scorer(self):
        self.assertIsNone(sb.FieldScorer.from_json(ROOT / "no" / "such.json"))


def _field(predicate):
    """Frame byte and size of the first local field-map entry matching
    predicate(name); the layout itself is not in the repository."""
    for f in sb.load_beacon_fields(FIELDS):
        if predicate(f.name):
            return sb.INFO_OFFSET + f.offset, f.size
    raise LookupError("field not in map")


def _value(r, predicate):
    fb, size = _field(predicate)
    i = fb - r.start_byte
    return int.from_bytes(r.frame[i:i + size], "little")


def _gauge_v(n):
    return "(Gauge)" in n and "Voltage" in n


def _soc(n):
    return "SOC" in n


def _rail(nominal):
    return lambda n: n.startswith("EPS_") and "Gauge" not in n and sb.rail_nominal_v(n) == nominal


@unittest.skipIf(sb is None or not N6RFM.is_file(), "local N6RFM capture not present")
class N6rfmReplayTests(unittest.TestCase):
    def test_reproduces_n6rfm_burst(self):
        x = np.fromfile(N6RFM, dtype=np.complex64)
        (a, b, snr), = sb.find_bursts(x, 50_000.0)
        r = sb.decode_burst(x, 50_000.0, a, b, snr, sb.FieldScorer.from_json(FIELDS))
        self.assertEqual(r.start_byte, 47)
        if not FIELDS.is_file():
            return
        # N6RFM report: gauge 8.230 V / 90.98 %, 3.3 V rail 3.296 V, 7 V rail 6.968 V
        self.assertEqual(_value(r, _gauge_v), 8230)
        self.assertEqual(_value(r, _soc), 9098)
        self.assertEqual(_value(r, _rail(3.3)) >> 3, 412)
        self.assertEqual(_value(r, _rail(7.0)) >> 3, 871)


@unittest.skipIf(sb is None or not GS_0923.is_file(), "local 2026-09-23 SCION-X capture not present")
class GroundStationReplayTests(unittest.TestCase):
    def test_strong_beacons_decode_from_byte_101(self):
        x = np.fromfile(GS_0923, dtype=np.complex64)
        bursts = sb.find_bursts(x, FS)
        scorer = sb.FieldScorer.from_json(FIELDS)
        first, second = [sb.decode_burst(x, FS, a, b, snr, scorer)
                         for a, b, snr in bursts if snr > 10]
        for r in (first, second):
            self.assertEqual(r.status, "unverified")
            self.assertEqual(r.start_byte, 101)
        if scorer is None:
            return
        # 06:38:58: clean; the 3.3 V rail reads 3.296 V like N6RFM's
        self.assertEqual(_value(first, _rail(3.3)) >> 3, 412)
        self.assertFalse(first.ambiguous)
        # 06:42:29: one 0 dropped from the run before the FCS; put back it
        # reads like 06:38:58. Still a tie on plausibility with the shorter
        # run before byte 219 (an unpowered rail), so it stays flagged.
        self.assertEqual(second.restored_zeros_at, (219,))
        self.assertEqual(_value(second, _rail(3.3)) >> 3, 412)
        self.assertTrue(second.ambiguous)


@unittest.skipIf(sb is None or not GS_1013.is_file(), "local 2026-10-13 SCION-X capture not present")
class GroundStation1013ReplayTests(unittest.TestCase):
    def test_both_beacons_read_nominal_rails(self):
        # the two bursts at 213.4 s and 433.5 s (the whole pass holds no
        # others); slices keep the suite's memory to one full capture
        capture = np.memmap(GS_1013, dtype=np.complex64, mode="r")
        scorer = sb.FieldScorer.from_json(FIELDS)
        decoded = []
        for t in (213.4, 433.5):
            x = np.asarray(capture[int((t - 1) * FS):int((t + 2) * FS)])
            (a, b, snr), = sb.find_bursts(x, FS)
            decoded.append(sb.decode_burst(x, FS, a, b, snr, scorer))
        first, second = decoded
        self.assertEqual((first.start_byte, second.start_byte), (118, 119))
        if scorer is None:
            return
        # 06:33:19 lost one 0 before the FCS (418 bit periods where the
        # frame has 419); 06:37:01 is clean
        self.assertEqual(first.restored_zeros_at, (219,))
        self.assertEqual(second.restored_zeros_at, ())
        for r in (first, second):
            self.assertEqual(_value(r, _rail(3.3)) >> 3, 412)
            self.assertIn(_value(r, _rail(7.0)) >> 3, (870, 871))


if __name__ == "__main__":
    unittest.main()
