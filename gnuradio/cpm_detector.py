"""Coherent CPM (GFSK) detector for AX100 Mode 5 bursts.

The tranche-5b sensitivity candidate: replaces the production
limiter-discriminator + hard slicer with preamble-aided block acquisition
and full CPM maximum-likelihood sequence estimation, parameterized by
modulation index (h = 2/3 for MAVERIC's auto default, h = 0.5 for ROADS).

Pipeline per record:
    1. Acquisition — decimated, segmented noncoherent correlation against
       the known preamble+ASM waveform over a coarse CFO grid (the segments
       tolerate the intra-bin residual); candidate peaks above a robust
       threshold become burst hypotheses.
    2. Fine sync — full-rate per-segment coherent correlations at the
       candidate: weighted phase regression gives CFO to ~Hz and carrier
       phase; parabolic interpolation gives sub-sample timing.
    3. Coherent demod — the burst is derotated and resampled onto an
       integer samples-per-symbol grid; a Viterbi MLSE runs over the full
       CPM trellis (phase lattice x 4-bit pulse-memory window, branch
       references derived numerically from the SAME Gaussian pulse the
       synthesis uses), starting from the KNOWN preamble state. Per-survivor
       phase processing follows residual carrier motion independently on each
       trellis path, while a second-order early/late loop follows clock error.
    4. Deframe — Golay(24,12) nearest-codeword decode for the length,
       CCSDS descramble, shortened RS(255,223) decode via libfec's
       decode_rs_char (the same C library the production chain uses),
       all in-process.

Honesty notes: acquisition is blind (no ground-truth timing), the
benchmark channel applies random carrier phase and sample-clock error via
fer_harness, and the verdict criterion is FER-curve separation at matched
seeds against the production chain — not single-shot decodes.

Measured verdict (2026-07-11, seed 20260711, 32 trials/point, impaired
channel; go/no-go bar was >=1.5 dB at p50):

    synthetic  ROADS 4k8/1200:   production p50 7.78 dB -> CPM 3.25 dB (+4.5)
    synthetic  MAVERIC 9k6/3200: production p50 6.57 dB -> CPM 2.00 dB (+4.6)
    REAL IQ    ROADS 2 golden burst + calibrated noise: production dies at
               +1.0 dB added noise, CPM at +5.0 dB -> +4.0 dB on-orbit margin.

Two real-waveform properties broke the first prototype and shaped the
final design — synthetic-only benchmarks missed both: (1) real frames are
VARIABLE length (RF stops at frame end; fixed-length demod marched the
trellis through dead air), hence the Golay-first two-pass demod; (2) the
1 Hz Doppler-correction steps in doppler-engaged captures reverse the
residual CFO mid-burst, false-locking any single decision-directed phase
loop onto a lattice-rotated basin, hence per-survivor phase processing.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable

import numpy as np
from scipy.signal import oaconvolve

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mav_gss_lib.platform.framing import asm_golay  # noqa: E402
from mav_gss_lib.platform.framing.asm_golay import (  # noqa: E402
    ASM, PREAMBLE, RS_PARITY, ccsds_scrambler_sequence, golay_encode)

FS = 200_000


def gfsk_iq(wire: bytes, baud: float, deviation_hz: float, *,
            bt: float = 0.5, sample_rate: float = FS) -> np.ndarray:
    """Synthesize MSB-first Gaussian FSK at the production IQ rate."""
    bits = np.unpackbits(np.frombuffer(wire, dtype=np.uint8))
    nrz = bits.astype(np.float64) * 2.0 - 1.0
    n = int(round(len(bits) * sample_rate / baud))
    symbol_idx = np.minimum(
        (np.arange(n) * baud / sample_rate).astype(np.int64), len(bits) - 1)
    wave = nrz[symbol_idx]
    sigma = np.sqrt(np.log(2.0)) / (2.0 * np.pi * bt * baud)
    span = int(round(4 * sample_rate / baud)) | 1
    t = (np.arange(span) - span // 2) / sample_rate
    g = np.exp(-0.5 * (t / sigma) ** 2)
    g /= g.sum()
    shaped = np.convolve(wave, g, mode="same")
    phase = 2.0 * np.pi * deviation_hz * np.cumsum(shaped) / sample_rate
    return np.exp(1j * phase)


# Compatibility for the existing offline harness while callers migrate.
_gfsk_iq = gfsk_iq

BT = 0.5
GRID_SPS = 40              # integer samples/symbol for the demod grid
ACQ_DECIM = 8              # acquisition runs at FS/ACQ_DECIM
DATA_FIELD_BYTES = 255
GOLAY_BITS = 24
ASM_BITS = 32
PREAMBLE_TAIL_BITS = 24    # known 0xAA bits used to warm-start the trellis

CancelCallback = Callable[[], bool]


class CpmCancelled(RuntimeError):
    """Cooperative cancellation of an in-progress CPM analysis."""


def _check_cancelled(cancelled: CancelCallback | None) -> None:
    if cancelled is not None and cancelled():
        raise CpmCancelled("CPM analysis cancelled")


@dataclass(frozen=True)
class Hypothesis:
    """One AX100 Mode-5 waveform hypothesis."""

    baud: float
    deviation: float
    transmitter: str = ""

    def __post_init__(self) -> None:
        if self.baud <= 0 or self.deviation <= 0:
            raise ValueError("baud and deviation must be positive")


@dataclass(frozen=True)
class BurstResult:
    """A validated frame with its absolute stream location and RF fit."""

    payload: bytes
    sample_start: int
    sample_end: int
    baud: float
    deviation: float
    transmitter: str
    cfo_hz: float
    sync_score: float


# ------------------------------------------------------------- libfec decode

_libfec = asm_golay._libfec
if _libfec is not None:
    _libfec.decode_rs_char.restype = ctypes.c_int
    _libfec.decode_rs_char.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_int,
    ]


def rs_decode(codeword: bytes) -> bytes | None:
    """Decode a dynamically shortened RS(255,223) codeword (payload+32).

    Same libfec handle family the encoder uses; returns the corrected
    payload or None when RS declares the block uncorrectable."""
    plen = len(codeword) - RS_PARITY
    if plen < 1 or plen > 223:
        return None
    rs = asm_golay._get_rs(223 - plen)
    buf = (ctypes.c_ubyte * len(codeword))(*codeword)
    corrections = _libfec.decode_rs_char(rs, buf, None, 0)
    if corrections < 0:
        return None
    return bytes(buf[:plen])


# ------------------------------------------------------------- Golay decode

_GOLAY_CODEWORDS = np.array(
    [int.from_bytes(golay_encode(v), "big") for v in range(4096)],
    dtype=np.uint32)


def golay_decode(word24: int) -> tuple[int, int]:
    """Nearest-codeword Golay(24,12) decode -> (value, bit_errors)."""
    dist = np.bitwise_xor(_GOLAY_CODEWORDS, np.uint32(word24 & 0xFFFFFF))
    counts = np.unpackbits(dist.view(np.uint8).reshape(-1, 4), axis=1).sum(axis=1)
    best = int(np.argmin(counts))
    return best, int(counts[best])


# ------------------------------------------------------------- pulse model


def _gaussian_taps(sps: float, bt: float = BT) -> np.ndarray:
    """The same Gaussian pulse _gfsk_iq builds, at `sps` samples/symbol."""
    sigma = np.sqrt(np.log(2.0)) / (2.0 * np.pi * bt)   # in symbols
    span = int(round(4 * sps)) | 1
    t = (np.arange(span) - span // 2) / sps             # in symbols
    g = np.exp(-0.5 * (t / sigma) ** 2)
    return g / g.sum()


@dataclass
class CpmModel:
    """Per-(baud, deviation) trellis tables on the integer demod grid."""
    baud: float
    deviation_hz: float
    h: float
    lattice: int                 # phase states: phase = idx * pi * h mod 2pi
    branch_refs: np.ndarray      # [32, GRID_SPS] complex — pattern references
    branch_dphi: float           # per-symbol lattice step = pi*h
    patterns: np.ndarray         # [32, 5] bits (b-2..b+2), MSB-first index

    @classmethod
    def build(cls, baud: float, deviation_hz: float,
              bt: float = BT) -> "CpmModel":
        h = 2.0 * deviation_hz / baud
        frac = Fraction(h).limit_denominator(64)
        lattice = (2 * frac.denominator) // np.gcd(frac.numerator,
                                                   2 * frac.denominator)
        S = GRID_SPS
        g = _gaussian_taps(S, bt)
        # unit-bit frequency pulse: conv(rect(S), g), centre-aligned like the
        # 'same'-mode convolution in _gfsk_iq
        u = np.convolve(np.ones(S), g)
        centre = (len(u) - 1) / 2.0
        # sample u for bit at symbol-offset i over symbol-0's S samples, and
        # its accumulated integral before symbol 0 starts
        offsets = range(-2, 3)
        slices = np.zeros((5, S))
        pre_ints = np.zeros(5)
        for col, i in enumerate(offsets):
            # bit i's pulse is centred on the centre of symbol i
            bit_centre = (i + 0.5) * S
            start = bit_centre - centre    # u[0] position on the grid
            idx = np.arange(S) - start     # u index for symbol-0 samples
            valid = (idx >= 0) & (idx <= len(u) - 1)
            s = np.zeros(S)
            s[valid] = np.interp(idx[valid], np.arange(len(u)), u)
            slices[col] = s
            # integral of this bit's pulse over all samples before symbol 0
            n_pre = int(np.floor(-start)) + 1
            if n_pre > 0:
                pre = np.arange(min(n_pre, len(u)))
                pre_ints[col] = u[pre].sum()
        # normalize: full integral of u must be S (=> pi*h per bit)
        scale = S / u.sum()
        slices *= scale
        pre_ints *= scale

        rad_per_unit = np.pi * h / S
        patterns = np.array([[1 if (p >> (4 - j)) & 1 else -1
                              for j in range(5)] for p in range(32)])
        refs = np.zeros((32, S), dtype=np.complex128)
        for p in range(32):
            bits = patterns[p]
            freq = bits @ slices                       # symbol-0 freq shape
            pre_phase = rad_per_unit * (bits @ pre_ints)
            phase = pre_phase + rad_per_unit * np.cumsum(freq)
            # reference at sample n = phase AFTER accumulating sample n,
            # matching _gfsk_iq's cumsum convention
            refs[p] = np.exp(1j * phase)
        return cls(baud=baud, deviation_hz=deviation_hz, h=h,
                   lattice=int(lattice), branch_refs=refs,
                   branch_dphi=np.pi * h, patterns=patterns)


def reconstruct_grid_waveform(model: CpmModel, bits: np.ndarray) -> np.ndarray:
    """Model self-check: rebuild the full waveform from branch tables.

    Must match a resampled _gfsk_iq synthesis of the same bits — this is
    the alignment proof for every table the Viterbi uses."""
    n = len(bits) - 4
    out = np.zeros(n * GRID_SPS, dtype=np.complex128)
    lattice_phase = 0.0
    nrz = bits * 2 - 1
    for k in range(n):
        window = nrz[k:k + 5]
        p = int("".join("1" if b > 0 else "0" for b in window), 2)
        out[k * GRID_SPS:(k + 1) * GRID_SPS] = (
            np.exp(1j * lattice_phase) * model.branch_refs[p])
        lattice_phase += model.branch_dphi * window[0]
    return out


# ------------------------------------------------------------- sync reference

def _bytes_to_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def _sync_reference(baud: float, deviation_hz: float) -> np.ndarray:
    """Preamble+ASM waveform at FS, via the same synthesis as the bursts."""
    return gfsk_iq(PREAMBLE + ASM, baud, deviation_hz)


# ------------------------------------------------------------- acquisition

@dataclass
class SyncEstimate:
    start: float        # FS-domain sample of the reference start (fractional)
    cfo_hz: float
    phase: float        # carrier phase at `start`
    score: float


def _segment_bounds(total: int, n_seg: int) -> list[tuple[int, int]]:
    edges = np.linspace(0, total, n_seg + 1).astype(int)
    return [(int(a), int(b)) for a, b in zip(edges[:-1], edges[1:])]


FINE_CFO_SPAN_HZ = 650.0
FINE_CFO_STEP_HZ = 100.0
DEFAULT_WIDE_CFO_SPAN_HZ = 10_000.0


def _segmented_scores(dec: np.ndarray, ref_dec: np.ndarray,
                      bounds: list[tuple[int, int]], cfo_hz: float,
                      valid_starts: int,
                      cancelled: CancelCallback | None = None) -> np.ndarray:
    """Noncoherent segmented matched-correlation score by reference start."""
    fs_dec = FS / ACQ_DECIM
    t_dec = np.arange(len(ref_dec)) / fs_dec
    rotated = ref_dec * np.exp(2j * np.pi * cfo_hz * t_dec)
    score = np.zeros(valid_starts, dtype=np.float64)
    for index, (a, b) in enumerate(bounds):
        if index % 8 == 0:
            _check_cancelled(cancelled)
        kernel = np.conj(rotated[a:b])[::-1]
        full = oaconvolve(dec, kernel, mode="full")
        chunk = np.abs(full[b - 1:b - 1 + valid_starts])
        score[:len(chunk)] += chunk
    return score


def _wide_cfo_centres(dec: np.ndarray, ref_dec: np.ndarray,
                      valid_starts: int, span_hz: float,
                      count: int = 1,
                      cancelled: CancelCallback | None = None) -> list[float]:
    """Nominate wide-CFO regions with one differential correlation.

    For ``u[n] = x[n+1] conj(x[n])`` a constant carrier offset becomes a
    constant phasor.  Correlation against the equivalently differentiated
    reference is therefore timing-coherent while its magnitude is carrier
    invariant; the correlation phase estimates CFO over the decimated
    stream's unambiguous +/-12.5 kHz range.  The normal segmented correlator
    performs the sensitive +/-650 Hz refinement around these estimates.
    """
    if len(dec) < 2 or len(ref_dec) < 2:
        return [0.0]
    _check_cancelled(cancelled)
    observed = dec[1:] * np.conj(dec[:-1])
    reference = ref_dec[1:] * np.conj(ref_dec[:-1])
    full = oaconvolve(observed, np.conj(reference)[::-1], mode="full")
    _check_cancelled(cancelled)
    corr = full[len(reference) - 1:len(reference) - 1 + valid_starts]
    observed_energy = oaconvolve(
        np.abs(observed) ** 2, np.ones(len(reference)), mode="valid")
    observed_energy = observed_energy[:valid_starts]
    reference_energy = float(np.sum(np.abs(reference) ** 2))
    coherence = np.abs(corr) / np.sqrt(np.maximum(
        observed_energy * reference_energy, 1e-30))
    # Six-sigma-like normalized-correlation gate.  It is independent of
    # receiver gain and remains valid when a low-baud burst occupies nearly
    # the entire record, while pure noise does not nominate random fine banks.
    threshold = max(0.08, 6.0 / np.sqrt(len(reference)))
    order = np.argsort(coherence)[::-1]
    min_sep = max(1, int(len(ref_dec) * 0.8))
    positions: list[int] = []
    fs_dec = FS / ACQ_DECIM
    centres: list[float] = []
    for raw_idx in order:
        idx = int(raw_idx)
        if coherence[idx] < threshold:
            break
        if any(abs(idx - old) < min_sep for old in positions):
            continue
        cfo = float(np.angle(corr[idx]) * fs_dec / (2.0 * np.pi))
        if abs(cfo) <= span_hz + FINE_CFO_SPAN_HZ:
            positions.append(idx)
            centres.append(float(np.clip(cfo, -span_hz, span_hz)))
        if len(centres) >= max(1, count):
            break
    return centres


def _fine_cfo_bins(centres: list[float], span_hz: float) -> np.ndarray:
    bins: set[float] = set()
    for centre in centres:
        bins.add(round(float(np.clip(centre, -span_hz, span_hz)), 9))
        lo = max(-span_hz, centre - FINE_CFO_SPAN_HZ)
        hi = min(span_hz, centre + FINE_CFO_SPAN_HZ)
        for value in np.arange(lo, hi + FINE_CFO_STEP_HZ / 2,
                               FINE_CFO_STEP_HZ):
            bins.add(round(float(value), 9))
    return np.array(sorted(bins), dtype=float)


def _cfar_threshold(score: np.ndarray) -> float:
    """Lower-tail CFAR with a percentile cap for high-duty-cycle records.

    The original median/MAD estimator fails when a long 1k2 burst occupies
    more than half of a short record: its broad correlation sidelobes become
    the "noise" population and can push the threshold above the true peak.
    The lower half still contains the quiet samples.  The percentile cap is
    a final guard against contaminated scale estimates; RS validation, not
    acquisition alone, is the false-frame gate.
    """
    if score.size == 0:
        return np.inf
    q20, q50 = np.quantile(score, (0.20, 0.50))
    lower = score[score <= q50]
    centre = float(np.median(lower)) if lower.size else float(q20)
    mad = (float(np.median(np.abs(lower - centre)))
           if lower.size else 0.0)
    noise_threshold = centre + 10.0 * 1.4826 * max(mad, 1e-12)
    # Keep the cap tight: it exists only to rescue a real sync peak when a
    # long, high-duty-cycle 1k2/2k4 waveform contaminates the background
    # estimate.  The former 99.5th-percentile cap admitted a wide shelf of
    # pure-noise maxima and needlessly sent many candidates into Viterbi.
    cap_q = 0.9995 if score.size >= 2_000 else 0.995
    return min(noise_threshold, float(np.quantile(score, cap_q)))


def acquire(record: np.ndarray, baud: float, deviation_hz: float,
            max_candidates: int, cfo_span_hz: float = 650.0,
            cancelled: CancelCallback | None = None,
            ) -> list[tuple[int, float]]:
    """Hierarchical blind burst search over timing and carrier offset.

    A differential full-preamble correlator nominates wide-CFO regions when
    ``cfo_span_hz`` is larger than 650 Hz.  The original 4 ms segmented
    correlator then searches those regions on a 100 Hz grid.  Returns
    ``(reference_start, coarse_cfo)`` pairs in input-sample units.
    """
    _check_cancelled(cancelled)
    if max_candidates <= 0 or len(record) == 0:
        return []
    ref = _sync_reference(baud, deviation_hz)
    dec = record[::ACQ_DECIM]
    ref_dec = ref[::ACQ_DECIM]
    fs_dec = FS / ACQ_DECIM
    valid_starts = len(dec) - len(ref_dec) + 1
    if valid_starts <= 0:
        return []

    span_hz = max(0.0, float(cfo_span_hz))
    if span_hz <= FINE_CFO_SPAN_HZ:
        centres = [0.0]
    else:
        # Never regress the proven sensitive centred path: differential
        # acquisition is only a strong-signal aid for large residual CFO.
        centres = [0.0]
        centres.extend(_wide_cfo_centres(dec, ref_dec, valid_starts,
                                         span_hz, cancelled=cancelled))
    bins = _fine_cfo_bins(centres, span_hz)

    seg_s = 0.004
    n_seg = max(4, int(len(ref_dec) / fs_dec / seg_s))
    bounds = _segment_bounds(len(ref_dec), n_seg)
    best = np.zeros(valid_starts)
    best_cfo = np.zeros(valid_starts)
    for cfo in bins:
        _check_cancelled(cancelled)
        acc = _segmented_scores(dec, ref_dec, bounds, float(cfo),
                                valid_starts, cancelled=cancelled)
        better = acc > best
        best[better] = acc[better]
        best_cfo[better] = cfo
    threshold = _cfar_threshold(best)
    min_sep = int(len(ref_dec) * 0.8)
    order = np.argsort(best)[::-1]
    peaks: list[int] = []
    for idx in order:
        _check_cancelled(cancelled)
        if best[idx] < threshold or len(peaks) >= max_candidates:
            break
        if all(abs(idx - p) >= min_sep for p in peaks):
            peaks.append(int(idx))
    return [(p * ACQ_DECIM, float(best_cfo[p])) for p in sorted(peaks)]


def fine_sync(record: np.ndarray, coarse_start: int, baud: float,
              deviation_hz: float, cfo_coarse: float = 0.0,
              cancelled: CancelCallback | None = None,
              ) -> SyncEstimate | None:
    """Full-rate refinement: timing to sub-sample, CFO to ~Hz, phase.

    Starts from the acquisition's coarse CFO bin — the 4 ms coherent
    segments only tolerate ~±60 Hz of residual."""
    _check_cancelled(cancelled)
    ref = _sync_reference(baud, deviation_hz)
    n = len(ref)
    ref = ref * np.exp(2j * np.pi * cfo_coarse * np.arange(n) / FS)
    seg_s = 0.004
    n_seg = max(6, int(n / FS / seg_s))
    bounds = _segment_bounds(n, n_seg)
    span = ACQ_DECIM * 2
    lags = np.arange(-span, span + 1)
    scores = np.zeros(len(lags))
    for li, lag in enumerate(lags):
        if li % 8 == 0:
            _check_cancelled(cancelled)
        a0 = coarse_start + lag
        if a0 < 0 or a0 + n > len(record):
            continue
        chunk = record[a0:a0 + n]
        scores[li] = sum(abs(np.vdot(ref[a:b], chunk[a:b])) for a, b in bounds)
    li = int(np.argmax(scores))
    if scores[li] == 0:
        return None
    # parabolic sub-sample timing
    frac = 0.0
    if 0 < li < len(lags) - 1:
        y0, y1, y2 = scores[li - 1], scores[li], scores[li + 1]
        denom = y0 - 2 * y1 + y2
        if denom < 0:
            frac = float(np.clip(0.5 * (y0 - y2) / denom, -0.5, 0.5))
    start = coarse_start + lags[li]
    chunk = record[start:start + n]
    _check_cancelled(cancelled)
    segs = np.array([np.vdot(ref[a:b], chunk[a:b]) for a, b in bounds])
    centres = np.array([(a + b) / 2 / FS for a, b in bounds])
    weights = np.abs(segs)
    if weights.sum() == 0:
        return None
    phases = np.unwrap(np.angle(segs))
    wmean_t = np.average(centres, weights=weights)
    wmean_p = np.average(phases, weights=weights)
    slope = (np.sum(weights * (centres - wmean_t) * (phases - wmean_p))
             / max(np.sum(weights * (centres - wmean_t) ** 2), 1e-12))
    cfo = slope / (2 * np.pi)
    phase0 = wmean_p - slope * wmean_t
    # one refinement pass with the CFO removed
    t = np.arange(n) / FS
    derot = chunk * np.exp(-2j * np.pi * cfo * t)
    segs2 = np.array([np.vdot(ref[a:b], derot[a:b]) for a, b in bounds])
    phases2 = np.unwrap(np.angle(segs2))
    weights2 = np.abs(segs2)
    wmean_p2 = np.average(phases2, weights=weights2)
    slope2 = (np.sum(weights2 * (centres - wmean_t) * (phases2 - wmean_p2))
              / max(np.sum(weights2 * (centres - wmean_t) ** 2), 1e-12))
    cfo += slope2 / (2 * np.pi)
    phase0 = wmean_p2 - slope2 * wmean_t

    return SyncEstimate(start=start + frac, cfo_hz=float(cfo_coarse + cfo),
                        phase=float(phase0), score=float(np.sum(weights2)))


# ------------------------------------------------------------- Viterbi MLSE

SLOW_PHASE_LOOP_ALPHA = 0.06
MODEL_MISMATCH_PHASE_LOOP_ALPHA = 0.8
MODEL_MISMATCH_PHASE_LOOP_MIN_H = 0.8


def _phase_loop_alphas(model: CpmModel) -> tuple[float, ...]:
    """Return phase-tracker hypotheses without perturbing proven low-h modes.

    The conservative loop is the sensitivity path validated on ROADS (h=0.5)
    and MAVERIC (h=2/3).  NUSHSat's real 1k2 waveform differs from the exact
    BT=0.5/deviation model, so a wide per-survivor update is also needed to
    absorb that mismatch.  The h threshold is an empirical way to confine the
    extra hypothesis to the currently observed waveform family, not a general
    CPM law.  The slow hypothesis remains as a sensitivity fallback.
    """
    if abs(model.h) >= MODEL_MISMATCH_PHASE_LOOP_MIN_H:
        return (MODEL_MISMATCH_PHASE_LOOP_ALPHA, SLOW_PHASE_LOOP_ALPHA)
    return (SLOW_PHASE_LOOP_ALPHA,)


def _bits_to_pattern_index(bits5: np.ndarray) -> int:
    return int("".join("1" if b else "0" for b in bits5), 2)


def viterbi_demod(model: CpmModel, grid: np.ndarray, n_bits: int,
                  known_head: np.ndarray,
                  loop_alpha: float = SLOW_PHASE_LOOP_ALPHA,
                  cancelled: CancelCallback | None = None) -> np.ndarray:
    """MLSE over the CPM trellis with a decision-directed phase loop.

    `grid`: burst resampled to GRID_SPS, aligned so the first sample is the
    start of the symbol of known_head[0]. `known_head`: bits (0/1) whose
    state pins the trellis start (preamble tail + ASM). Returns the decoded
    n_bits that FOLLOW the known head."""
    _check_cancelled(cancelled)
    S = GRID_SPS
    L = model.lattice
    refs = model.branch_refs                     # [32, S]
    lat_phasors = np.exp(-1j * model.branch_dphi * np.arange(L))

    head = known_head.astype(int)
    total_known = len(head)

    # -- warm start over the known head ---------------------------------
    # First pass: measure the constant phase offset between the grid data
    # and the model's phase convention (sync normalization vs. reference
    # partial-pulse constants) from the known symbols coherently.
    lat = 0
    acc = 0.0 + 0.0j
    lat_track = []
    for k in range(2, total_known - 2):
        if k % 16 == 0:
            _check_cancelled(cancelled)
        p = _bits_to_pattern_index(head[k - 2:k + 3])
        seg = grid[k * S:(k + 1) * S]
        z = np.vdot(refs[p], seg) * np.exp(-1j * model.branch_dphi * lat)
        acc += z
        lat_track.append(lat)
        lat = (lat + (1 if head[k - 2] else -1)) % L
    phase_corr = float(np.angle(acc)) if abs(acc) else 0.0
    # Second pass: fine phase + timing loops over the same known symbols
    # (data-aided — the branch is known, so the loops converge before the
    # first unknown bit).
    base0 = np.arange(len(grid), dtype=float)
    rel0 = np.arange(S, dtype=float)

    def fetch0(k: int, t: float) -> np.ndarray:
        pos = k * S + rel0 + t
        return (np.interp(pos, base0, grid.real)
                + 1j * np.interp(pos, base0, grid.imag))

    lat = 0
    tau0 = 0.0
    tau_rate0 = 0.0
    for k in range(2, total_known - 2):
        if k % 16 == 0:
            _check_cancelled(cancelled)
        p = _bits_to_pattern_index(head[k - 2:k + 3])
        tau0 += tau_rate0
        seg = fetch0(k, tau0) * np.exp(-1j * phase_corr)
        z = np.vdot(refs[p], seg) * np.exp(-1j * model.branch_dphi * lat)
        if abs(z):
            phase_corr += loop_alpha * float(np.angle(z))
            z_e = abs(np.vdot(refs[p], fetch0(k, tau0 - 0.5)))
            z_late = abs(np.vdot(refs[p], fetch0(k, tau0 + 0.5)))
            denom = z_e + z_late
            if denom > 0:
                err = float((z_late - z_e) / denom)
                tau0 += 0.08 * err
                tau_rate0 = float(np.clip(tau_rate0 + 0.004 * err, -0.05, 0.05))
        lat = (lat + (1 if head[k - 2] else -1)) % L
    k0 = total_known - 2

    # -- trellis tables ---------------------------------------------------
    n_states = L * 16
    NEG = -1e18
    next_state = np.zeros((n_states, 2), dtype=np.int32)
    pattern_of = np.zeros((n_states, 2), dtype=np.int32)
    lat_of = np.zeros(n_states, dtype=np.int32)
    for st in range(n_states):
        if st % 32 == 0:
            _check_cancelled(cancelled)
        lat_i, w = divmod(st, 16)
        lat_of[st] = lat_i
        b = [(w >> 3) & 1, (w >> 2) & 1, (w >> 1) & 1, w & 1]
        for e in (0, 1):
            pattern_of[st, e] = _bits_to_pattern_index(np.array(b + [e]))
            new_lat = (lat_i + (1 if b[0] else -1)) % L
            next_state[st, e] = new_lat * 16 + (((w << 1) | e) & 0xF)
    # predecessor tables: each state has exactly 2 (one per dropped bit a)
    pred_state = np.zeros((n_states, 2), dtype=np.int32)
    pred_input = np.zeros((n_states, 2), dtype=np.int32)
    for ns in range(n_states):
        if ns % 32 == 0:
            _check_cancelled(cancelled)
        lat_n, w_n = divmod(ns, 16)
        e = w_n & 1
        for a in (0, 1):
            w_prev = (a << 3) | (w_n >> 1)
            lat_prev = (lat_n - (1 if a else -1)) % L
            pred_state[ns, a] = lat_prev * 16 + w_prev
            pred_input[ns, a] = e

    metrics = np.full(n_states, NEG)
    seed_w4 = int("".join(map(str, head[k0 - 2:k0 + 2])), 2)
    metrics[lat * 16 + seed_w4] = 0.0

    n_steps = (total_known - 2 - k0) + n_bits
    choices = np.zeros((n_steps, n_states), dtype=np.int8)

    base = np.arange(len(grid), dtype=float)
    rel = np.arange(S, dtype=float)

    def fetch(k: int, tau: float) -> np.ndarray:
        pos = k * S + rel + tau
        return (np.interp(pos, base, grid.real)
                + 1j * np.interp(pos, base, grid.imag))

    # Per-survivor phase processing (PSP): a single decision-directed loop
    # false-locks onto a lattice-rotated basin at phase transients (measured
    # on the ROADS golden burst: 1 Hz Doppler-correction steps reverse the
    # residual mid-burst) and then decodes garbage to the end. With PSP each
    # state carries the phase its OWN survivor implies, so the true path
    # tracks itself and cannot be dragged off by a wrong branch's feedback.
    phases = np.full(n_states, phase_corr)
    idx_states = np.arange(n_states)
    # Second-order early/late timing loop (global): real bauds are divider-
    # quantized ramps; the integrator tracks them without steady-state lag.
    tau = tau0
    tau_rate = tau_rate0
    beta = 0.08
    gamma = 0.004
    for step in range(n_steps):
        if step % 16 == 0:
            _check_cancelled(cancelled)
        k = k0 + step
        tau += tau_rate
        seg = fetch(k, tau)
        z = refs.conj() @ seg                                # [32]
        rot = np.exp(-1j * phases)                           # per-state
        zl = (z[pattern_of] * lat_phasors[lat_of][:, None]
              * rot[:, None])                                # [n_states, 2]
        bm = zl.real
        # candidates per next state via the 2-predecessor tables
        cand = (metrics[pred_state]
                + bm[pred_state, pred_input])                # [n_states, 2]
        pick = np.argmax(cand, axis=1).astype(np.int8)
        metrics = cand[np.arange(n_states), pick]
        choices[step] = pick
        # each next-state inherits its winning predecessor's phase, nudged
        # by that branch's own residual angle
        won_pred = pred_state[idx_states, pick]
        won_e = pred_input[idx_states, pick]
        zwin = zl[won_pred, won_e]
        phases = phases[won_pred] + loop_alpha * np.angle(zwin)
        # timing from the globally best branch
        st_b, e_b = np.unravel_index(int(np.argmax(bm + 0.0)), bm.shape)
        p_best = pattern_of[st_b, e_b]
        if abs(zl[st_b, e_b]) > 0:
            r = refs[p_best]
            z_e = abs(np.vdot(r, fetch(k, tau - 0.5)))
            z_late = abs(np.vdot(r, fetch(k, tau + 0.5)))
            denom = z_e + z_late
            if denom > 0:
                err = float((z_late - z_e) / denom)
                tau += beta * err
                tau_rate = float(np.clip(tau_rate + gamma * err, -0.05, 0.05))

    # traceback
    out_bits = np.zeros(n_steps, dtype=np.uint8)
    st = int(np.argmax(metrics))
    for step in range(n_steps - 1, -1, -1):
        if step % 32 == 0:
            _check_cancelled(cancelled)
        a = choices[step, st]
        out_bits[step] = st & 1                # e = LSB of the window
        st = pred_state[st, a]
    # step k decides bit b_{k+2}: the first NEW bit appears once
    # k + 2 >= total_known
    skip = max(0, total_known - (k0 + 2))
    return out_bits[skip:skip + n_bits]


# ------------------------------------------------------------- full pipeline

def _resample_to_grid(record: np.ndarray, sync: SyncEstimate, baud: float,
                      n_symbols: int) -> np.ndarray | None:
    step = FS / (baud * GRID_SPS)
    pos = sync.start + np.arange(n_symbols * GRID_SPS) * step
    if pos[-1] >= len(record) - 1:
        return None
    base = np.arange(len(record))
    t = pos / FS
    derot = np.exp(-1j * (2 * np.pi * sync.cfo_hz * t + sync.phase))
    return (np.interp(pos, base, record.real)
            + 1j * np.interp(pos, base, record.imag)) * derot


def _decode_burst_with_length(
        record: np.ndarray, sync: SyncEstimate,
        model: CpmModel,
        cancelled: CancelCallback | None = None) -> tuple[bytes, int] | None:
    _check_cancelled(cancelled)
    baud = model.baud
    ref_bits_total = len(PREAMBLE + ASM) * 8
    head_bits = PREAMBLE_TAIL_BITS + ASM_BITS
    # grid starts at the head (preamble tail): shift sync.start forward
    head_offset_symbols = ref_bits_total - head_bits
    head_start = sync.start + head_offset_symbols * FS / baud
    # carrier phase at the new start
    phase = (sync.phase
             + 2 * np.pi * sync.cfo_hz * (head_start - sync.start) / FS)
    # ALSO the CPM phase advances through the skipped preamble: alternating
    # 0xAA bits accumulate lattice phase; 0xAA = 10101010 repeated — equal
    # ones and zeros over any whole number of bytes -> net zero. The head
    # starts at a byte boundary, so the lattice is 0 there... except the
    # reference waveform itself carries that phase; sync.phase was measured
    # against the FULL reference, which embeds the preamble's CPM phase.
    # Simplest correct approach: measure phase against the reference's own
    # value at the head start.
    ref = _sync_reference(baud, model.deviation_hz)
    ref_idx = head_offset_symbols * FS / baud
    ref_phase = float(np.angle(np.interp(ref_idx, np.arange(len(ref)).astype(float),
                                         ref.real)
                               + 1j * np.interp(ref_idx, np.arange(len(ref)).astype(float),
                                                ref.imag)))
    shifted = SyncEstimate(start=head_start, cfo_hz=sync.cfo_hz,
                           phase=phase + ref_phase, score=sync.score)
    head = _bytes_to_bits(PREAMBLE + ASM)[-head_bits:]

    # Pass 1 — Golay length field only. The real AX100 frame is VARIABLE
    # length: the transmitter stops after golay+frame_len bytes, so
    # demodulating a fixed 255-byte field marches the trellis through dead
    # air and poisons the traceback deep into real data (measured on the
    # ROADS 2 golden burst: RF off at symbol ~1584 of the assumed 2120).
    _check_cancelled(cancelled)
    grid = _resample_to_grid(record, shifted, baud,
                             head_bits + GOLAY_BITS + 8)
    if grid is None:
        return None
    for loop_alpha in _phase_loop_alphas(model):
        _check_cancelled(cancelled)
        bits = viterbi_demod(
            model, grid, GOLAY_BITS, head,
            loop_alpha=loop_alpha, cancelled=cancelled)
        golay_word = int("".join(map(str, bits[:GOLAY_BITS])), 2)
        value, errs = golay_decode(golay_word)
        frame_len = value & 0xFF
        if (errs > 3 or frame_len < RS_PARITY + 1
                or frame_len > DATA_FIELD_BYTES):
            continue

        # Pass 2 — exactly the transmitted symbols, using the same survivor
        # phase dynamics that produced the valid Golay length.
        _check_cancelled(cancelled)
        data_bits = GOLAY_BITS + frame_len * 8
        data_grid = _resample_to_grid(
            record, shifted, baud, head_bits + data_bits + 4)
        if data_grid is None:
            continue
        bits = viterbi_demod(
            model, data_grid, data_bits, head,
            loop_alpha=loop_alpha, cancelled=cancelled)
        field = np.packbits(
            bits[GOLAY_BITS:GOLAY_BITS + frame_len * 8]).tobytes()
        pn = ccsds_scrambler_sequence(frame_len)
        codeword = bytes(a ^ b for a, b in zip(field, pn))
        payload = rs_decode(codeword)
        if payload is not None:
            return payload, frame_len
    return None


def decode_burst(record: np.ndarray, sync: SyncEstimate, model: CpmModel,
                 cancelled: CancelCallback | None = None,
                 ) -> bytes | None:
    """Compatibility wrapper returning only the validated payload."""
    decoded = _decode_burst_with_length(
        record, sync, model, cancelled=cancelled)
    return decoded[0] if decoded is not None else None


def detect_bursts(record: np.ndarray, hypothesis: Hypothesis,
                  max_candidates: int = 96, bt: float = BT,
                  cfo_span_hz: float = DEFAULT_WIDE_CFO_SPAN_HZ,
                  sample_offset: int = 0,
                  model: CpmModel | None = None,
                  cancelled: CancelCallback | None = None) -> list[BurstResult]:
    """Blind acquisition and coherent decode with structured results."""
    _check_cancelled(cancelled)
    baud = float(hypothesis.baud)
    deviation_hz = float(hypothesis.deviation)
    model = model or CpmModel.build(baud, deviation_hz, bt)
    results: list[BurstResult] = []
    for coarse, cfo0 in acquire(record, baud, deviation_hz, max_candidates,
                                cfo_span_hz=float(cfo_span_hz),
                                cancelled=cancelled):
        _check_cancelled(cancelled)
        sync = fine_sync(record, coarse, baud, deviation_hz, cfo0,
                         cancelled=cancelled)
        if sync is None:
            continue
        decoded = _decode_burst_with_length(
            record, sync, model, cancelled=cancelled)
        if decoded is None:
            continue
        payload, frame_len = decoded
        start = int(round(float(sync.start))) + int(sample_offset)
        frame_bytes = len(PREAMBLE) + len(ASM) + 3 + frame_len
        end_rel = float(sync.start) + frame_bytes * 8.0 * FS / baud
        results.append(BurstResult(
            payload=payload,
            sample_start=start,
            sample_end=int(np.ceil(end_rel)) + int(sample_offset),
            baud=baud,
            deviation=deviation_hz,
            transmitter=hypothesis.transmitter,
            cfo_hz=float(sync.cfo_hz),
            sync_score=float(sync.score),
        ))

    # One physical burst can produce nearby acquisition candidates. Keep the
    # strongest result within a symbol, without suppressing a later repeated
    # beacon carrying identical bytes.
    kept: list[BurstResult] = []
    tolerance = max(ACQ_DECIM * 2, int(np.ceil(FS / baud)))
    for result in sorted(results, key=lambda r: r.sync_score, reverse=True):
        _check_cancelled(cancelled)
        if any(result.payload == old.payload
               and abs(result.sample_start - old.sample_start) <= tolerance
               for old in kept):
            continue
        kept.append(result)
    return sorted(kept, key=lambda r: r.sample_start)


def detect_and_decode(record: np.ndarray, baud: float, deviation_hz: float,
                      max_candidates: int = 96, bt: float = BT,
                      cfo_span_hz: float = 650.0,
                      cancelled: CancelCallback | None = None) -> list[bytes]:
    """Compatibility API returning payloads from one waveform hypothesis."""
    hypothesis = Hypothesis(float(baud), float(deviation_hz))
    return [result.payload for result in detect_bursts(
        record, hypothesis, max_candidates=max_candidates, bt=bt,
        cfo_span_hz=cfo_span_hz, cancelled=cancelled)]


def maximum_mode5_burst_samples(baud: float) -> int:
    """Samples occupied by the longest 312-byte Mode-5 transmission."""
    return int(np.ceil((len(PREAMBLE) + len(ASM) + 3
                        + DATA_FIELD_BYTES) * 8.0 * FS / float(baud)))


class BurstStreamDetector:
    """Chunk-invariant rolling detector with absolute positions and dedup.

    This class is deliberately synchronous and single-worker internally.
    A GNU Radio wrapper should call it from an asynchronous bounded worker,
    never from the scheduler's ``work()`` method.  Public methods are locked
    so reset/finish cannot race an active feed from another control thread.
    """

    def __init__(self, hypotheses: list[Hypothesis] | tuple[Hypothesis, ...],
                 cfo_span_hz: float = DEFAULT_WIDE_CFO_SPAN_HZ,
                 *, max_candidates: int = 8,
                 window_samples: int | None = None,
                 hop_samples: int | None = None,
                 dedup_holdoff_samples: int | None = None,
                 bt: float = BT,
                 cancelled: CancelCallback | None = None) -> None:
        if not hypotheses:
            raise ValueError("at least one CPM hypothesis is required")
        self.hypotheses = tuple(hypotheses)
        if len(set(self.hypotheses)) != len(self.hypotheses):
            raise ValueError("duplicate CPM hypotheses are not allowed")
        self.cfo_span_hz = float(cfo_span_hz)
        if not 0.0 <= self.cfo_span_hz <= FS / (2 * ACQ_DECIM):
            raise ValueError("cfo_span_hz exceeds the acquisition Nyquist range")
        self.max_candidates = int(max_candidates)
        if self.max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        self.bt = float(bt)
        self.cancelled = cancelled

        longest = max(maximum_mode5_burst_samples(h.baud)
                      for h in self.hypotheses)
        guard = 4 * ACQ_DECIM
        self.hop_samples = int(hop_samples or longest)
        self.window_samples = int(window_samples or (2 * longest + guard))
        if self.hop_samples <= 0:
            raise ValueError("hop_samples must be positive")
        if self.window_samples < longest + self.hop_samples + guard:
            raise ValueError(
                "window_samples must contain a maximum burst at every hop phase")
        if self.hop_samples >= self.window_samples:
            raise ValueError("hop_samples must be smaller than window_samples")
        self.dedup_holdoff_samples = int(
            dedup_holdoff_samples or (self.window_samples + longest))
        if self.dedup_holdoff_samples <= 0:
            raise ValueError("dedup_holdoff_samples must be positive")

        self._models = {
            hypothesis: CpmModel.build(hypothesis.baud,
                                       hypothesis.deviation, self.bt)
            for hypothesis in self.hypotheses
        }
        self._dedup_tolerance = max(
            ACQ_DECIM * 2,
            int(np.ceil(FS / min(h.baud for h in self.hypotheses))),
        )
        self._lock = threading.RLock()
        self._buffer = np.empty(0, dtype=np.complex64)
        self._buffer_start = 0
        self._next_input_sample = 0
        self._recent: list[BurstResult] = []

    @property
    def next_sample_start(self) -> int:
        with self._lock:
            return self._next_input_sample

    @property
    def buffered_samples(self) -> int:
        with self._lock:
            return len(self._buffer)

    def _reset_unlocked(self, next_sample_start: int) -> None:
        self._buffer = np.empty(0, dtype=np.complex64)
        self._buffer_start = int(next_sample_start)
        self._next_input_sample = int(next_sample_start)
        self._recent.clear()

    def reset(self, next_sample_start: int = 0) -> None:
        """Discard overlap/dedup state, for example after an RX gap."""
        with self._lock:
            self._reset_unlocked(next_sample_start)

    def _deduplicate(self, candidates: list[BurstResult],
                     horizon: int) -> list[BurstResult]:
        cutoff = int(horizon) - self.dedup_holdoff_samples
        self._recent = [item for item in self._recent
                        if item.sample_start >= cutoff]
        emitted: list[BurstResult] = []
        for result in sorted(candidates,
                             key=lambda item: item.sync_score,
                             reverse=True):
            previous = self._recent + emitted
            if any(result.payload == old.payload
                   and abs(result.sample_start - old.sample_start)
                   <= self._dedup_tolerance
                   for old in previous):
                continue
            emitted.append(result)
        emitted.sort(key=lambda item: item.sample_start)
        self._recent.extend(emitted)
        return emitted

    def _analyse(self, record: np.ndarray, sample_offset: int,
                 cancelled: CancelCallback | None = None) -> list[BurstResult]:
        effective_cancelled = (cancelled if cancelled is not None
                               else self.cancelled)
        _check_cancelled(effective_cancelled)
        candidates: list[BurstResult] = []
        for hypothesis in self.hypotheses:
            _check_cancelled(effective_cancelled)
            candidates.extend(detect_bursts(
                record, hypothesis,
                max_candidates=self.max_candidates,
                bt=self.bt,
                cfo_span_hz=self.cfo_span_hz,
                sample_offset=sample_offset,
                model=self._models[hypothesis],
                cancelled=effective_cancelled,
            ))
        return self._deduplicate(candidates,
                                 sample_offset + len(record))

    def feed(self, chunk: np.ndarray,
             sample_start: int | None = None,
             cancelled: CancelCallback | None = None) -> list[BurstResult]:
        """Consume an arbitrary complex chunk and emit newly completed bursts.

        Supplying a non-contiguous ``sample_start`` automatically resets the
        rolling window, preventing a decode across missing USRP samples.
        """
        effective_cancelled = (cancelled if cancelled is not None
                               else self.cancelled)
        _check_cancelled(effective_cancelled)
        samples = np.asarray(chunk, dtype=np.complex64).reshape(-1)
        if samples.size == 0:
            return []
        with self._lock:
            start = (self._next_input_sample if sample_start is None
                     else int(sample_start))
            if start != self._next_input_sample:
                self._reset_unlocked(start)

            emitted: list[BurstResult] = []
            cursor = 0
            while cursor < len(samples):
                _check_cancelled(effective_cancelled)
                need = self.window_samples - len(self._buffer)
                take = min(need, len(samples) - cursor)
                part = samples[cursor:cursor + take]
                if self._buffer.size:
                    self._buffer = np.concatenate((self._buffer, part))
                else:
                    self._buffer = part.copy()
                cursor += take
                self._next_input_sample += take

                if len(self._buffer) == self.window_samples:
                    emitted.extend(self._analyse(self._buffer,
                                                 self._buffer_start,
                                                 effective_cancelled))
                    self._buffer = self._buffer[self.hop_samples:].copy()
                    self._buffer_start += self.hop_samples
            return sorted(emitted, key=lambda item: item.sample_start)

    def finish(self, cancelled: CancelCallback | None = None) -> list[BurstResult]:
        """Analyse the final partial window once, then clear sample storage."""
        effective_cancelled = (cancelled if cancelled is not None
                               else self.cancelled)
        _check_cancelled(effective_cancelled)
        with self._lock:
            emitted: list[BurstResult] = []
            if self._buffer.size:
                emitted = self._analyse(
                    self._buffer, self._buffer_start, effective_cancelled)
            self._buffer = np.empty(0, dtype=np.complex64)
            self._buffer_start = self._next_input_sample
            return emitted


# ------------------------------------------------------------- FER adapter

def run_point_mode5_cpm(baud: float, deviation_hz: float, ebn0_db: float,
                        channel, bursts=None):
    """FER point through the CPM detector — paired with run_point_mode5."""
    from fer_harness import (FerPoint, compose_fer_record, mode5_bursts,
                             noise_sigma)
    bursts = bursts if bursts is not None else mode5_bursts(channel, baud, deviation_hz)
    record = compose_fer_record(bursts, channel.lead_gaps,
                                noise_sigma(ebn0_db, baud), channel.noise_seed)
    decoded = set(detect_and_decode(record.astype(np.complex128), baud,
                                    deviation_hz,
                                    max_candidates=2 * len(channel.payloads)))
    k = sum(1 for p in channel.payloads if p in decoded)
    return FerPoint(ebn0_db=ebn0_db, decoded=k, trials=len(channel.payloads))


if __name__ == "__main__":
    # correctness ladder, quickest first
    from mav_gss_lib.platform.framing.asm_golay import build_asm_golay_frame

    rng = np.random.default_rng(7)

    print("== model self-check: branch tables vs _gfsk_iq synthesis ==")
    for baud, dev in ((4800, 1200), (9600, 3200)):
        model = CpmModel.build(baud, dev)
        bits = rng.integers(0, 2, 64)
        direct = gfsk_iq(np.packbits(bits).tobytes(), baud, dev)
        # resample direct synth onto the grid, skipping edge symbols
        step = FS / (baud * GRID_SPS)
        pos = np.arange(len(bits) * GRID_SPS) * step
        base = np.arange(len(direct))
        grid = (np.interp(pos, base, direct.real)
                + 1j * np.interp(pos, base, direct.imag))
        recon = reconstruct_grid_waveform(model, bits)
        a = grid[4 * GRID_SPS:(len(bits) - 6) * GRID_SPS]
        b = recon[2 * GRID_SPS:]
        b = b[:len(a)]
        # align: reconstruction starts at symbol 2 (first full window)
        err = np.angle(a * np.conj(b) * np.exp(-1j * np.angle((a * np.conj(b)).sum())))
        print(f"  {baud}/{dev}: h={model.h:.4f} lattice={model.lattice} "
              f"max|phase err|={np.max(np.abs(err)):.4f} rad")

    print("== clean burst, blind acquisition ==")
    for baud, dev in ((4800, 1200), (9600, 3200)):
        payload = bytes([0xC1]) + rng.bytes(39)
        burst = gfsk_iq(build_asm_golay_frame(payload), baud, dev)
        gap = np.zeros(int(0.1 * FS), dtype=complex)
        record = np.concatenate([gap, burst * np.exp(1j * 1.234), gap])
        noise = 0.02 * (rng.standard_normal(record.size)
                        + 1j * rng.standard_normal(record.size)) / np.sqrt(2)
        out = detect_and_decode(record + noise, baud, dev, max_candidates=4)
        ok = payload in out
        print(f"  {baud}/{dev}: decoded={len(out)} payload_match={ok}")
