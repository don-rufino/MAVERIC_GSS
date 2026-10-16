"""Unit tests for gnuradio/signal_meter.py — the GR-free per-beacon meter.

Synthetic bursts with a known Eb/N0 and noise density check the absolute
numbers; uplink windows check that transmit rows never reach the floor or
the burst average.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gnuradio"))

from signal_meter import SignalMeter

FS = 100_000.0
BAUD = 9600.0
DEV = 2400.0


def _fsk(n_bits: int, rng: np.random.Generator) -> np.ndarray:
    sps = FS / BAUD
    bits = rng.integers(0, 2, n_bits)
    sym = 2.0 * bits[np.minimum((np.arange(int(n_bits * sps)) / sps).astype(int), n_bits - 1)] - 1
    return np.exp(2j * np.pi * np.cumsum(sym * DEV) / FS)


def _scene(ebn0_db: float, *, quiet_s: float = 3.0, burst_bits: int = 3000,
           tx_level: float = 0.0, tx_span: tuple[float, float] | None = None,
           seed: int = 1):
    """Noise everywhere, one FSK burst after `quiet_s`; optional strong
    wideband uplink leak inside tx_span (seconds from start)."""
    rng = np.random.default_rng(seed)
    sigma2 = 1e-6                                   # noise power per sample
    burst = _fsk(burst_bits, rng)
    amp = np.sqrt(10 ** (ebn0_db / 10) * BAUD * sigma2 / FS)
    n = int((quiet_s + 0.5) * FS) + burst.size
    x = np.sqrt(sigma2 / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    start = int(quiet_s * FS)
    x[start:start + burst.size] += amp * burst
    if tx_span:
        a, b = int(tx_span[0] * FS), int(tx_span[1] * FS)
        # uplink leak is wideband (GFSK + switch transients), not a tone
        x[a:b] += tx_level * (rng.standard_normal(b - a) + 1j * rng.standard_normal(b - a))
    end_s = (start + burst.size) / FS
    return x.astype(np.complex64), sigma2, end_s


def _run(x: np.ndarray, *, t0: float = 1000.0, pdu_delay_s: float = 0.2,
         end_s: float, air_s: float, tx: tuple[float, float] | None = None,
         nominal_hz: float | None = None, tunes=()):
    meter = SignalMeter(FS, nominal_hz=nominal_hz)
    if tx:
        meter.note_tx(t0 + tx[0], t0 + tx[1])
    for rel_t, hz, source in tunes:
        meter.note_tune(t0 + rel_t, hz, source)
    stop = int((end_s + pdu_delay_s) * FS)
    for i in range(0, stop, 4096):
        j = min(stop, i + 4096)
        meter.push(x[i:j], t0 + j / FS)
    return meter, meter.measure(air_s, BAUD, t0 + stop / FS)


class SignalMeterTests(unittest.TestCase):
    def test_known_ebn0_and_noise_floor(self):
        for ebn0 in (10.0, 20.0):
            x, sigma2, end_s = _scene(ebn0)
            meter, r = _run(x, end_s=end_s, air_s=0.2)
            self.assertEqual(r["status"], "ok")
            self.assertAlmostEqual(r["ebn0_db"], ebn0, delta=0.6, msg=f"ebn0 {ebn0}")
            expected_floor = 10 * np.log10(sigma2 * meter.enbw / FS)
            self.assertAlmostEqual(r["floor_dbfs"], expected_floor, delta=0.5)
            self.assertEqual(r["floor_method"], "pre_burst")
            self.assertAlmostEqual(r["snr_peak_db"], r["peak_dbfs"] - r["floor_dbfs"], delta=0.11)
            self.assertAlmostEqual(r["snr_3db_db"], r["snr_peak_db"] - 3.0, delta=0.11)
            # max-hold of ~K exponential noise samples sits near ln(K) times
            # the mean, so it must land well above the averaged floor
            hold = r["maxhold"]
            k = hold["floor_ffts"]
            self.assertGreater(k, 50)
            expected_rise = 10 * np.log10(np.log(k) + 0.5772)
            self.assertAlmostEqual(hold["floor_dbfs"] - r["floor_dbfs"], expected_rise, delta=1.0)
            self.assertGreaterEqual(hold["peak_dbfs"], r["peak_dbfs"])
            self.assertLess(hold["snr_peak_db"], r["snr_peak_db"])
            self.assertAlmostEqual(hold["snr_3db_db"], hold["snr_peak_db"] - 3.0, delta=0.11)

    def test_whole_burst_and_both_fsk_tones(self):
        x, _, end_s = _scene(20.0, burst_bits=3000)
        _, r = _run(x, end_s=end_s, air_s=0.15)      # PDU understates air time
        self.assertAlmostEqual(r["burst_s"], 3000 / BAUD, delta=0.03)
        lo, hi = r["edges_3db_hz"]
        self.assertLess(lo, -DEV + 400)
        self.assertGreater(hi, DEV - 400)
        self.assertLess(abs(r["center_offset_hz"]), 300)
        self.assertLess(r["tone_3db_hz"], r["bw_3db_hz"])
        spec = r["spectrum"]
        self.assertEqual(len(spec["db10"]), sum(1 for _ in spec["db10"]))
        self.assertLess(spec["f0_hz"], lo)

    def test_bandwidth_below_peak_nests_around_the_3db_edges(self):
        widths = []
        for ebn0 in (30.0, 40.0):
            x, _, end_s = _scene(ebn0)
            _, r = _run(x, end_s=end_s, air_s=0.2)
            l10, l20 = r["bw_levels"]["10"], r["bw_levels"]["20"]
            for level, lv in ((10, l10), (20, l20)):
                self.assertEqual(lv["status"], "ok")
                self.assertFalse(lv["band_limited"])
                self.assertAlmostEqual(lv["line_above_floor_db"], r["snr_peak_db"] - level, delta=0.11)
            lo3, hi3 = r["edges_3db_hz"]
            self.assertLess(l20["edges_hz"][0], l10["edges_hz"][0])
            self.assertLess(l10["edges_hz"][0], lo3)
            self.assertGreater(l10["edges_hz"][1], hi3)
            self.assertGreater(l20["edges_hz"][1], l10["edges_hz"][1])
            self.assertAlmostEqual(l10["bw_hz"], l10["edges_hz"][1] - l10["edges_hz"][0] + r["rbw_hz"], delta=1)
            widths.append((l10["bw_hz"], l20["bw_hz"]))
        # the signal's shape, not its strength, sets the width
        self.assertLessEqual(abs(widths[0][0] - widths[1][0]), 200)
        self.assertLessEqual(abs(widths[0][1] - widths[1][1]), 200)

    def test_deep_dip_between_tones_does_not_cut_the_width(self):
        # two carriers 5 kHz apart, within 3 dB of each other, with noise
        # between them: -10/-20 must span both, like the -3 dB edges do
        rng = np.random.default_rng(5)
        n = int(4 * FS)
        x = np.sqrt(1e-6 / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
        t = np.arange(int(0.4 * FS)) / FS
        a = int(3 * FS)
        x[a:a + t.size] += 3e-3 * (np.exp(-2j * np.pi * 2500 * t) + 0.85 * np.exp(2j * np.pi * 2500 * t))
        _, r = _run(x.astype(np.complex64), end_s=3.4, air_s=0.2)
        lo3, hi3 = r["edges_3db_hz"]
        self.assertLess(lo3, -2000)
        self.assertGreater(hi3, 2000)
        for level in ("10", "20"):
            lo, hi = r["bw_levels"][level]["edges_hz"]
            self.assertLessEqual(lo, lo3)
            self.assertGreaterEqual(hi, hi3)

    def test_level_too_close_to_floor_is_not_measured(self):
        x, _, end_s = _scene(20.0)                  # peak SNR ~23 dB: -20 dB line ~3 dB up
        _, r = _run(x, end_s=end_s, air_s=0.2)
        self.assertEqual(r["bw_levels"]["10"]["status"], "ok")
        l20 = r["bw_levels"]["20"]
        self.assertEqual(l20["status"], "near_floor")
        self.assertLess(l20["line_above_floor_db"], 3.0)
        self.assertNotIn("edges_hz", l20)
        self.assertNotIn("bw_hz", l20)

    def test_tone_beyond_the_skirt_stays_out_of_the_width(self):
        # a CW carrier at +9 kHz, above even the -10 dB line, separated from
        # the burst by noise: the walk stops at the burst's skirt
        x, _, end_s = _scene(30.0)
        _, clean = _run(x, end_s=end_s, air_s=0.2)
        t = np.arange(x.size) / FS
        x = (x + 1e-3 * np.exp(2j * np.pi * 9000 * t)).astype(np.complex64)
        _, r = _run(x, end_s=end_s, air_s=0.2)
        spec = r["spectrum"]
        tone_db = spec["db10"][round((9000 - spec["f0_hz"]) / spec["df_hz"])] / 10
        self.assertGreater(tone_db, r["peak_dbfs"] - 10)
        for level in ("10", "20"):
            self.assertEqual(r["bw_levels"][level]["edges_hz"], clean["bw_levels"][level]["edges_hz"])
            self.assertLess(r["bw_levels"][level]["edges_hz"][1], 8000)

    def test_level_reaching_the_band_edge_is_flagged(self):
        # a wideband burst fills the whole measured band
        rng = np.random.default_rng(3)
        n = int(4 * FS)
        x = np.sqrt(1e-6 / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
        x[int(3 * FS):int(3.4 * FS)] *= 100
        _, r = _run(x.astype(np.complex64), end_s=3.4, air_s=0.2)
        for level in ("10", "20"):
            self.assertEqual(r["bw_levels"][level]["status"], "ok")
            self.assertTrue(r["bw_levels"][level]["band_limited"])

    def test_uplink_before_beacon_never_reaches_floor(self):
        # strong leak 1.5-2.1 s, beacon at 3.0 s: with the recovery guard the
        # whole second before the beacon is TX-tainted, so the floor must come
        # from earlier rows and stay at the true noise level, not the leak's
        x, sigma2, end_s = _scene(15.0, tx_level=0.05, tx_span=(1.5, 2.1))
        meter, r = _run(x, end_s=end_s, air_s=0.2, tx=(1.5, 2.1))
        expected_floor = 10 * np.log10(sigma2 * meter.enbw / FS)
        self.assertAlmostEqual(r["floor_dbfs"], expected_floor, delta=0.5)
        self.assertTrue(r["floor_tx_affected"])
        self.assertEqual(r["floor_method"], "pre_burst_earlier")
        self.assertAlmostEqual(r["ebn0_db"], 15.0, delta=0.6)

    def test_unflagged_uplink_is_mistaken_for_the_beacon(self):
        # same scene without telling the meter about TX: the leak sits in the
        # lookback and outshines the beacon, so it gets measured instead.
        # Guards the test above (TX marking is what makes it pass).
        x, _, end_s = _scene(15.0, tx_level=0.05, tx_span=(1.5, 2.1))
        _, r = _run(x, end_s=end_s, air_s=0.2)
        self.assertGreater(abs(r["ebn0_db"] - 15.0), 3.0)

    def test_frame_decoded_during_uplink_is_not_measured(self):
        x, _, end_s = _scene(15.0)
        _, r = _run(x, end_s=end_s, air_s=0.2, tx=(0.0, end_s + 1.0))
        self.assertEqual(r, {"v": 1, "status": "during_tx"})

    def test_no_history_returns_none(self):
        self.assertIsNone(SignalMeter(FS).measure(0.2, BAUD, 1000.0))

    def test_no_nominal_means_no_tune_block(self):
        x, _, end_s = _scene(20.0)
        _, r = _run(x, end_s=end_s, air_s=0.2)
        self.assertNotIn("tune", r)
        self.assertNotIn("nominal_offset_hz", r)

    def test_tuned_frequency_is_averaged_over_the_burst(self):
        # Doppler holds +8000 Hz until a step to +7940 Hz exactly halfway
        # through the burst, so the burst sees +7970 Hz on average
        nominal = 437_500_000.0
        x, _, end_s = _scene(20.0, burst_bits=3000)
        _, probe = _run(x, end_s=end_s, air_s=0.2)
        t1 = probe["burst_end_ms"] / 1000 - 1000.0
        mid = t1 - probe["burst_s"] / 2
        _, r = _run(x, end_s=end_s, air_s=0.2, nominal_hz=nominal,
                    tunes=[(0.5, nominal + 8000, "doppler"), (mid, nominal + 7940, "doppler")])
        tune = r["tune"]
        self.assertEqual(tune["nominal_hz"], nominal)
        self.assertAlmostEqual(tune["tuned_hz"], nominal + 7970, delta=1.0)
        self.assertEqual(tune["tuned_change_hz"], -60.0)
        self.assertTrue(tune["doppler"])
        self.assertEqual(r["received_center_hz"], round(tune["tuned_hz"] + r["center_offset_hz"], 1))
        self.assertEqual(r["nominal_offset_hz"], round(r["received_center_hz"] - nominal))

    def test_parked_on_nominal_is_not_doppler(self):
        # disengaging parks the radio on nominal through the same command path
        nominal = 437_500_000.0
        x, _, end_s = _scene(20.0)
        _, r = _run(x, end_s=end_s, air_s=0.2, nominal_hz=nominal,
                    tunes=[(0.2, nominal + 5000, "doppler"), (1.0, nominal, "doppler")])
        self.assertEqual(r["tune"]["tuned_hz"], nominal)
        self.assertFalse(r["tune"]["doppler"])
        self.assertEqual(r["nominal_offset_hz"], r["center_offset_hz"])


REPLAY = (Path(__file__).resolve().parent.parent / "logs" / "iq"
          / "iq_maveric_20260923T063435Z.sigmf-data")


@unittest.skipUnless(REPLAY.is_file(), "local SCION-X capture not present (logs/ is gitignored)")
class RecordedPassReplayTests(unittest.TestCase):
    """GS pass 2026-09-23 06:34Z: three SCION-X beacons, values hand-checked
    against the offline prototype and the Phase 0 decoder's burst spans."""

    EXPECTED = [  # burst start/end s, peak, floor, Eb/N0
        (262.780, 263.169, -75.3, -107.7, 19.7),
        (473.091, 473.478, -78.2, -107.6, 16.9),
        (683.369, 683.673, -86.9, -107.9, 8.5),
    ]
    # -10 / -20 dB widths (Hz); None = line too close to the floor. The
    # strongest tone is +2051 Hz with a >10 dB dip to the other tone.
    EXPECTED_BW = [(5273, 6445), (5371, 6055), (5371, None)]

    def test_beacons_reproduce(self):
        fs, disp = 200_000.0, 2                     # display tap at rx_bw 50 kHz
        x = np.memmap(REPLAY, dtype=np.complex64, mode="r")
        for (start, end, peak, floor, ebn0), (bw10, bw20) in zip(self.EXPECTED, self.EXPECTED_BW):
            meter = SignalMeter(fs / disp)
            stop = int((end + 0.15) * fs)
            for i in range(int((start - 6) * fs), stop, 8192):
                j = min(stop, i + 8192)
                meter.push(np.asarray(x[i:j:disp]), j / fs)
            r = meter.measure(274 * 8 / 9600, 9600, stop / fs)
            self.assertAlmostEqual(r["peak_dbfs"], peak, delta=0.5)
            self.assertAlmostEqual(r["floor_dbfs"], floor, delta=0.5)
            self.assertAlmostEqual(r["ebn0_db"], ebn0, delta=0.5)
            self.assertEqual(r["edges_3db_hz"], [-3027, 2051])
            self.assertEqual(r["bw_levels"]["10"]["bw_hz"], bw10)
            if bw20 is None:
                self.assertEqual(r["bw_levels"]["20"]["status"], "near_floor")
            else:
                self.assertEqual(r["bw_levels"]["20"]["bw_hz"], bw20)
            # every beacon is the same 274-byte frame plus flags: 0.389 s on
            # air (the decoder cut the weak third burst short at 0.304 s)
            self.assertAlmostEqual(r["burst_s"], 0.389, delta=0.02)

    def test_offset_from_nominal_with_recorded_doppler(self):
        # the recording's own Doppler commands, replayed as tune events:
        # residual -488 Hz on every beacon while Doppler swings ~18 kHz
        import json
        caps = json.loads(REPLAY.with_suffix(".sigmf-meta").read_text())["captures"]
        tunes = [(c["core:sample_start"] / 200_000.0, c["core:frequency"])
                 for c in caps if c.get("maveric:event") == "doppler_command"]
        expected_offsets = [7771, -5585, -10152]      # Hz from 437.500 MHz
        fs, disp = 200_000.0, 2
        x = np.memmap(REPLAY, dtype=np.complex64, mode="r")
        for (start, end, *_), offset in zip(self.EXPECTED, expected_offsets):
            meter = SignalMeter(fs / disp, nominal_hz=437_500_000.0)
            stop = int((end + 0.15) * fs)
            for t, hz in tunes:                      # live, only the past has arrived
                if t <= stop / fs:
                    meter.note_tune(t, hz, "doppler")
            for i in range(int((start - 6) * fs), stop, 8192):
                j = min(stop, i + 8192)
                meter.push(np.asarray(x[i:j:disp]), j / fs)
            r = meter.measure(274 * 8 / 9600, 9600, stop / fs)
            self.assertEqual(r["center_offset_hz"], -488)
            self.assertTrue(r["tune"]["doppler"])
            self.assertAlmostEqual(r["nominal_offset_hz"], offset, delta=2)


if __name__ == "__main__":
    unittest.main()
