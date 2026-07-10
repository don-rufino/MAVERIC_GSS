"""Carrier estimator for the Astrocast 0.1 1k2 two-FSK beacon.

The gr-satellites FSK demodulator deliberately applies a narrow Carson-
bandwidth filter.  That is desirable once the beacon is centred, but it is
not an acquisition filter.  This module finds the midpoint of the two FSK
tones in the wider 200 ksps channel so MAV_ASTROCAST can translate the
beacon to baseband before handing it to gr-satellites.

The module is GNU Radio independent so the estimator can be exercised with
ordinary NumPy tests.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class FskCenterEstimate:
    """One accepted two-tone carrier estimate."""

    center_hz: float
    tone_snr_db: float
    tone_balance_db: float


class AveragedFftPower:
    """Accumulate non-overlapping FFTs into short averaged PSD snapshots."""

    def __init__(self, fft_size: int = 8192, averages: int = 6) -> None:
        if fft_size < 256 or fft_size & (fft_size - 1):
            raise ValueError("fft_size must be a power of two >= 256")
        if averages < 1:
            raise ValueError("averages must be >= 1")
        self.fft_size = int(fft_size)
        self.averages = int(averages)
        self._window = np.hanning(self.fft_size).astype(np.float32)
        self._buffer = np.empty(self.fft_size, dtype=np.complex64)
        self._buffer_used = 0
        self._power_sum = np.zeros(self.fft_size, dtype=np.float64)
        self._power_count = 0

    def feed(self, samples: np.ndarray) -> list[np.ndarray]:
        """Consume complex samples and return each newly completed PSD average."""
        values = np.asarray(samples, dtype=np.complex64).reshape(-1)
        snapshots: list[np.ndarray] = []
        offset = 0
        while offset < values.size:
            count = min(self.fft_size - self._buffer_used, values.size - offset)
            end = self._buffer_used + count
            self._buffer[self._buffer_used:end] = values[offset:offset + count]
            self._buffer_used = end
            offset += count
            if self._buffer_used != self.fft_size:
                continue

            spectrum = np.fft.fftshift(np.fft.fft(self._buffer * self._window))
            self._power_sum += spectrum.real * spectrum.real + spectrum.imag * spectrum.imag
            self._power_count += 1
            self._buffer_used = 0
            if self._power_count == self.averages:
                snapshots.append(self._power_sum / self._power_count)
                self._power_sum.fill(0.0)
                self._power_count = 0
        return snapshots


def estimate_fsk_center(
    power: np.ndarray,
    sample_rate: float,
    *,
    search_center_hz: float = 0.0,
    search_half_width_hz: float = 20_000.0,
    tone_offset_hz: float = 1_200.0,
    tone_window_hz: float = 250.0,
    refine_window_hz: float = 500.0,
    min_tone_snr_db: float = 6.0,
    max_tone_balance_db: float = 12.0,
) -> FskCenterEstimate | None:
    """Estimate the midpoint of a two-FSK signal from an FFT-shifted PSD.

    Candidate centres are scored by the weaker of the two expected tone
    bands.  Requiring both tones rejects ordinary single-carrier interferers
    and the B210 DC spur.  The winning tone locations are then refined by a
    power-weighted centroid.
    """
    psd = np.asarray(power, dtype=np.float64).reshape(-1)
    if psd.size < 256 or not np.isfinite(sample_rate) or sample_rate <= 0:
        return None
    if not np.all(np.isfinite(psd)):
        psd = np.nan_to_num(psd, nan=0.0, posinf=0.0, neginf=0.0)
    psd = np.maximum(psd, 0.0)

    bin_hz = float(sample_rate) / psd.size
    frequencies = np.fft.fftshift(np.fft.fftfreq(psd.size, 1.0 / float(sample_rate)))
    tone_bins = max(1, int(round(abs(tone_offset_hz) / bin_hz)))
    window_bins = max(1, int(round(abs(tone_window_hz) / bin_hz)))
    kernel = np.ones(2 * window_bins + 1, dtype=np.float64)
    kernel /= kernel.size
    band_power = np.convolve(psd, kernel, mode="same")

    candidates = np.arange(tone_bins, psd.size - tone_bins)
    low = float(search_center_hz) - abs(float(search_half_width_hz))
    high = float(search_center_hz) + abs(float(search_half_width_hz))
    candidates = candidates[
        (frequencies[candidates] >= low) & (frequencies[candidates] <= high)
    ]
    if candidates.size == 0:
        return None

    left = band_power[candidates - tone_bins]
    right = band_power[candidates + tone_bins]
    weak = np.minimum(left, right)
    strong = np.maximum(left, right)
    noise_floor = float(np.median(band_power[candidates]))
    if noise_floor <= 0.0:
        return None
    candidate_snr_db = 10.0 * np.log10(
        np.maximum(weak, np.finfo(float).tiny) / noise_floor
    )
    candidate_balance_db = 10.0 * np.log10(
        np.maximum(strong, np.finfo(float).tiny)
        / np.maximum(weak, np.finfo(float).tiny)
    )
    valid = (
        (candidate_snr_db >= min_tone_snr_db)
        & (candidate_balance_db <= max_tone_balance_db)
    )
    if not np.any(valid):
        return None
    valid_candidates = candidates[valid]
    # Rank only already-balanced pairs by their weaker tone. This prevents a
    # very strong lone carrier from hiding a weaker but valid FSK pair.
    winner = int(valid_candidates[int(np.argmax(weak[valid]))])
    left_power = float(band_power[winner - tone_bins])
    right_power = float(band_power[winner + tone_bins])

    weak_tone = min(left_power, right_power)
    strong_tone = max(left_power, right_power)
    tone_snr_db = 10.0 * np.log10(max(weak_tone, np.finfo(float).tiny) / noise_floor)
    tone_balance_db = 10.0 * np.log10(
        max(strong_tone, np.finfo(float).tiny)
        / max(weak_tone, np.finfo(float).tiny)
    )
    if tone_snr_db < min_tone_snr_db or tone_balance_db > max_tone_balance_db:
        return None

    coarse_center = float(frequencies[winner])
    baseline = float(np.median(psd[candidates]))
    refined_tones: list[float] = []
    for expected in (coarse_center - tone_offset_hz, coarse_center + tone_offset_hz):
        mask = np.abs(frequencies - expected) <= abs(refine_window_hz)
        weights = np.maximum(psd[mask] - baseline, 0.0)
        total = float(np.sum(weights))
        if total <= 0.0:
            refined_tones.append(float(expected))
        else:
            refined_tones.append(float(np.sum(frequencies[mask] * weights) / total))

    return FskCenterEstimate(
        center_hz=0.5 * (refined_tones[0] + refined_tones[1]),
        tone_snr_db=float(tone_snr_db),
        tone_balance_db=float(tone_balance_db),
    )


__all__ = ["AveragedFftPower", "FskCenterEstimate", "estimate_fsk_center"]
