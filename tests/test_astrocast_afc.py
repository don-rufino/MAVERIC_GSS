"""Focused tests for Astrocast 1k2 carrier acquisition and filter-bank logic."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gnuradio"))

from astrocast_1k2_afc import AveragedFftPower, estimate_fsk_center


SAMPLE_RATE = 200_000
FFT_SIZE = 8192
AVERAGES = 6


def _synthetic_fsk(center_hz: float, *, snr_db: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    count = FFT_SIZE * AVERAGES
    samples_per_symbol = SAMPLE_RATE / 1200.0
    bits = rng.choice((-1.0, 1.0), size=int(count / samples_per_symbol) + 2)
    symbols = bits[(np.arange(count) / samples_per_symbol).astype(int)]
    frequency = center_hz + 1200.0 * symbols
    phase = np.cumsum(2.0 * np.pi * frequency / SAMPLE_RATE)
    signal = np.exp(1j * phase)
    noise_scale = 10.0 ** (-snr_db / 20.0)
    noise = (
        rng.normal(size=count) + 1j * rng.normal(size=count)
    ) * (noise_scale / np.sqrt(2.0))
    return (signal + noise).astype(np.complex64)


@pytest.mark.parametrize(
    ("center_hz", "seed"),
    [(-15_000.0, 1), (-8_000.0, 2), (0.0, 3), (14_000.0, 4)],
)
def test_estimator_centers_weak_1k2_fsk(center_hz, seed):
    samples = _synthetic_fsk(center_hz, snr_db=-8.0, seed=seed)
    averager = AveragedFftPower(fft_size=FFT_SIZE, averages=AVERAGES)
    snapshots = []
    # Exercise the same partial-buffer behavior used by a GNU Radio scheduler.
    for chunk in np.array_split(samples, 17):
        snapshots.extend(averager.feed(chunk))

    assert len(snapshots) == 1
    estimate = estimate_fsk_center(snapshots[0], SAMPLE_RATE)
    assert estimate is not None
    assert estimate.center_hz == pytest.approx(center_hz, abs=100.0)
    assert estimate.tone_snr_db >= 6.0


def test_estimator_rejects_noise_without_a_tone_pair():
    rng = np.random.default_rng(99)
    count = FFT_SIZE * AVERAGES
    noise = (rng.normal(size=count) + 1j * rng.normal(size=count)).astype(np.complex64)
    snapshots = AveragedFftPower(FFT_SIZE, AVERAGES).feed(noise)

    assert len(snapshots) == 1
    assert estimate_fsk_center(snapshots[0], SAMPLE_RATE) is None


def test_strong_single_carrier_does_not_hide_weaker_fsk_pair():
    samples = _synthetic_fsk(-8_000.0, snr_db=-3.0, seed=10)
    phase = 2.0 * np.pi * 10_000.0 * np.arange(samples.size) / SAMPLE_RATE
    samples += (20.0 * np.exp(1j * phase)).astype(np.complex64)
    power = AveragedFftPower(FFT_SIZE, AVERAGES).feed(samples)[0]

    estimate = estimate_fsk_center(power, SAMPLE_RATE)
    assert estimate is not None
    assert estimate.center_hz == pytest.approx(-8_000.0, abs=100.0)


def test_estimator_honors_the_acquisition_window():
    samples = _synthetic_fsk(-8_000.0, snr_db=0.0, seed=8)
    power = AveragedFftPower(FFT_SIZE, AVERAGES).feed(samples)[0]

    estimate = estimate_fsk_center(
        power,
        SAMPLE_RATE,
        search_center_hz=-7_500.0,
        search_half_width_hz=2_000.0,
    )
    assert estimate is not None
    assert estimate.center_hz == pytest.approx(-8_000.0, abs=100.0)


def test_afc_requires_confirmation_and_reacquires_after_large_jump():
    pytest.importorskip("gnuradio")
    import MAV_ASTROCAST as flowgraph

    class Channelizer:
        def __init__(self):
            self.updates = []

        def set_center_freq(self, value):
            self.updates.append(float(value))

    def power_at(center_hz, seed):
        samples = _synthetic_fsk(center_hz, snr_db=-6.0, seed=seed)
        return AveragedFftPower(FFT_SIZE, AVERAGES).feed(samples)[0]

    channelizer = Channelizer()
    afc = flowgraph._BeaconAfcSink(channelizer, SAMPLE_RATE, 0.0, 20_000.0)
    first = power_at(-8_000.0, 21)
    for _ in range(flowgraph.AFC_CONFIRMATIONS - 1):
        afc._apply_estimate(first)
    assert not afc.locked
    assert channelizer.updates == []

    afc._apply_estimate(first)
    assert afc.locked
    assert afc.correction_hz == pytest.approx(-8_000.0, abs=100.0)

    # Models engaging Doppler after the initial lock: the residual carrier
    # jumps outside the narrow tracking window and must use broad reacquisition.
    moved = power_at(2_000.0, 22)
    for _ in range(flowgraph.AFC_CONFIRMATIONS):
        afc._apply_estimate(moved)
    assert afc.correction_hz == pytest.approx(2_000.0, abs=100.0)
    assert len(channelizer.updates) == 2


def test_fixed_bins_cover_full_offset_range_without_waiting_for_afc():
    pytest.importorskip("gnuradio")
    import MAV_ASTROCAST as flowgraph

    assert flowgraph.BEACON_BIN_CENTERS_HZ == (
        -18_000.0, -12_000.0, -6_000.0, 0.0,
        6_000.0, 12_000.0, 18_000.0,
    )
    transition_edge = (
        flowgraph.BEACON_CHANNEL_CUTOFF_HZ
        + flowgraph.BEACON_CHANNEL_TRANSITION_HZ / 2.0
    )
    for carrier_hz in np.linspace(-21_000.0, 21_000.0, 169):
        residual = min(
            abs(carrier_hz - center)
            for center in flowgraph.BEACON_BIN_CENTERS_HZ
        )
        assert residual <= 3_000.0
        assert residual + flowgraph.BEACON_DEVIATION_HZ <= transition_edge


def test_parallel_decoder_dedup_window_is_short():
    pytest.importorskip("gnuradio")
    import MAV_ASTROCAST as flowgraph

    dedup = flowgraph._PduDeduplicator(ttl_s=0.5)
    payload = bytes.fromhex("01020304")
    assert dedup._accept_payload(payload, now=10.0)
    assert not dedup._accept_payload(payload, now=10.49)
    # A genuinely repeated frame completes well after this window.
    assert dedup._accept_payload(payload, now=10.5)
