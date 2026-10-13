"""SCION-X burst decoder: one burst of IQ in, one AX.25 frame (or less) out.

The same code runs live (MAV_DUO's _ScionxBurstDecoder block feeds it each
burst it detects) and offline on SigMF captures (`python3 scionx_burst.py
<capture>.sigmf-data`), so every recorded pass is a replay test and every
improvement found on recordings ships live unchanged. Pure numpy/scipy.

Signal (ground truth: the team's ground-test frame published in the SCION-X
decode writeup, N6RFM's analysis, the team's beacon field map):
  9600 bit/s 2-FSK, plain NRZ (no NRZI, no G3RUH), LSB-first, HDLC flags and
  bit stuffing, AX.25 UI BN0SCX -> BN0CU, 274-byte frame (16 header +
  256 info + 2 FCS CRC-16/X.25). Stock gr-satellites cannot decode it: its
  AX.25 deframer is NRZI-only and needs the opening flags.

Transmitter departures handled here:
  * late key-up: the start of the frame is often never radiated, so the
    opening flags/header are missing. The frame position comes from the
    closing flag train (counted back) and/or from aligning against the
    ground-test frame; such frames are CRC-unverifiable.
  * skipped stuffing bits (a 0 after five 1s that is data): every stuffing
    choice is scored (CRC when complete, field plausibility otherwise).
  * dropped bits in long zero runs: the transmitter sometimes radiates one
    or two 0s fewer than the frame holds inside a long run of 0s (measured
    on the edges: 417.985 bit periods where the frame has 419). One or two
    0s put back into each long run are scored alongside the stuffing
    choices; a candidate needing them wins only on strictly better evidence.

Clock: one fixed bit clock per burst. The period is found by searching for
the value that phase-aligns the most transitions (immune to the long
transition-free zero runs that make an adaptive loop drift or a seeded fit
miscount bits), then refined by least squares.

Field plausibility (tie-breaker between stuffing choices) needs the beacon
field layout, which is kept out of the repository: pass a FieldScorer built
from the mission's local beacon_fields.json, or none to decode without it.
"""

from __future__ import annotations

import itertools
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import signal as sps

BAUD = 9600.0
FRAME_BYTES = 274                      # 16 header + 256 info + 2 FCS
FRAME_BITS = FRAME_BYTES * 8
INFO_OFFSET = 16                       # field-map offsets count from the APID here
FLAG = np.array([0, 1, 1, 1, 1, 1, 1, 0], dtype=np.uint8)
TEMP_RANGE_C = (-55, 124)              # team beacon sheet: EPS temperatures "from -55 to 124 °C"
SLIP_MIN_RUN = 64                      # zero runs this long may have lost bits on air
SLIP_MAX_PER_RUN = 2                   # most 0s seen dropped from one run
DETECT_CUTOFF_HZ = 15000.0             # +-10 kHz Doppler + +-2.4 kHz deviation + margin

# Team ground-test frame (published with the SCION-X decode writeup), 272
# bytes without FCS. Static bytes in it anchor frames that lost their start.
REFERENCE_HEX = """
84 9c 60 86 aa 40 60 84 9c 60 a6 86 b0 e1 03 f0
00 7c 08 3c 81 33 6a 00 02 01 00 00 00 00 00 20
00 00 00 2b 81 33 6a 00 00 00 00 00 00 03 02 00
00 44 09 00 20 10 00 00 c3 ad d0 00 99 bf 4e 04
8b c5 78 05 61 d7 f6 08 d4 a1 1f 00 00 00 00 00
""" + " 00" * 16 * 4 + """
00 00 00 00 00 00 00 00 00 00 00 00 00 00 08 26
08 26 08 26 28 26 20 26 20 00 20 00 20 00 20 00
20 00 a0 3d 78 ff 77 0f 9b ff 8a 1f 16 15 15 e7
b0 b0 b0 b0 19 18 1a e8 0c 40 02 48 1b 18 00 00
""" + " 00" * 16 * 4
REFERENCE = bytes.fromhex(REFERENCE_HEX)
HEADER = REFERENCE[:16]


# ---------------------------------------------------------------- bit utils

def bytes_to_bits(data: bytes) -> np.ndarray:
    """LSB-first bit array (AX.25 on-wire order)."""
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8), bitorder="little")


def bits_to_bytes(bits: np.ndarray) -> bytes:
    n = len(bits) // 8 * 8
    return np.packbits(bits[:n].astype(np.uint8), bitorder="little").tobytes()


def crc16_x25(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


REFERENCE_BITS = bytes_to_bits(REFERENCE + crc16_x25(REFERENCE).to_bytes(2, "little"))


def find_flags(bits: np.ndarray) -> list[int]:
    """Start indices of 0x7E flags."""
    if len(bits) < 8:
        return []
    w = np.lib.stride_tricks.sliding_window_view(bits, 8)
    return list(np.flatnonzero((w == FLAG).all(axis=1)))


def destuff(bits: np.ndarray) -> tuple[np.ndarray, list[int]]:
    """Standard HDLC destuff. Returns (bits, sites) where each site is the
    index in the OUTPUT at which the removed 0 would be re-inserted."""
    out: list[int] = []
    sites: list[int] = []
    ones = 0
    i = 0
    while i < len(bits):
        b = int(bits[i])
        out.append(b)
        ones = ones + 1 if b else 0
        if ones == 5:
            ones = 0
            if i + 1 < len(bits) and bits[i + 1] == 0:
                sites.append(len(out))
                i += 1                      # drop the stuffed 0
        i += 1
    return np.array(out, dtype=np.uint8), sites


def zero_run_sites(bits: np.ndarray) -> list[int]:
    """Start of every run of >= SLIP_MIN_RUN 0s, listed SLIP_MAX_PER_RUN
    times: each entry is one dropped 0 that may be put back (anywhere in a
    run of 0s is the same place)."""
    edges = np.flatnonzero(np.diff(np.concatenate(([1], bits, [1])).astype(np.int8)))
    starts, ends = edges[0::2], edges[1::2]
    return [int(a) for a, b in zip(starts, ends) if b - a >= SLIP_MIN_RUN
            for _ in range(SLIP_MAX_PER_RUN)]


def insertion_sites(bits: np.ndarray) -> tuple[np.ndarray, list[int], int]:
    """Destuff, then list every place a 0 may be put back: the removed
    stuffing 0s first (indices < n_stuffing), then dropped-bit sites."""
    data, sites = destuff(bits)
    return data, sites + zero_run_sites(data), len(sites)


def reinsert(bits: np.ndarray, sites: list[int], keep: tuple[int, ...]) -> np.ndarray:
    """Put back 0s at the chosen sites (stuffing 0s kept as data, or 0s the
    transmitter dropped)."""
    out = bits
    for pos in sorted((sites[k] for k in keep), reverse=True):
        out = np.insert(out, pos, 0)
    return out


def crc_ok(frame: bytes) -> bool:
    return crc16_x25(frame[:-2]) == int.from_bytes(frame[-2:], "little")


# ---------------------------------------------------------------- plausibility

@dataclass(frozen=True)
class BeaconField:
    name: str
    ctype: str
    size: int
    offset: int                       # from the APID (frame byte INFO_OFFSET)
    endian: str = "Little"

    @property
    def byteorder(self) -> str:
        return "big" if self.endian.lower().startswith("big") else "little"


_RAIL_RE = re.compile(r"(\d+(?:\.\d+)?)V(\d)?\b")


def rail_nominal_v(name: str) -> float | None:
    """Nominal of a rail named like 'EPS_<rail>3V3 Voltage' -> 3.3,
    'EPS_<rail>7V Voltage' -> 7.0; None when the name carries no voltage."""
    head = name.split(" ")[0]
    m = _RAIL_RE.search(head)
    if not m:
        return None
    whole = m.group(1)
    return float(f"{whole}.{m.group(2)}") if m.group(2) and "." not in whole else float(whole)


def load_beacon_fields(path: str | Path) -> list[BeaconField]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [BeaconField(f["name"], f["ctype"], int(f["size"]), int(f["offset"]),
                        f.get("endian", "Little")) for f in data["fields"]]


class FieldScorer:
    """Counts physically impossible values among received beacon fields.

    INA3221 registers (EPS voltages/currents other than the fuel gauge) have
    bits [2:0] == 0; bus voltages stay <= 26 V and powered rails within 15 %
    of nominal; currents <= 5 A; SOC 0-100 %; int8 temperatures within the
    sensor range the team's beacon sheet documents (TEMP_RANGE_C). These are
    integrity checks (could the sensor produce this value?), not operating
    limits: the fuel gauge voltage has no documented range and is not checked.
    """

    def __init__(self, fields: list[BeaconField]) -> None:
        self.fields = fields
        self.ina = [f for f in fields if f.name.startswith("EPS_") and "Gauge" not in f.name
                    and f.name.endswith(("Voltage", "Current"))]

    @classmethod
    def from_json(cls, path: str | Path) -> "FieldScorer | None":
        try:
            return cls(load_beacon_fields(path))
        except (OSError, ValueError, KeyError):
            return None

    def __call__(self, received: dict[int, int]) -> tuple[int, int]:
        """(INA3221 violations, implausible values) for frame-byte -> value."""
        ina = sum(1 for f in self.ina
                  if (INFO_OFFSET + f.offset) in received
                  and received[INFO_OFFSET + f.offset] & 0x07)
        bad = 0
        for f in self.fields:
            fb = INFO_OFFSET + f.offset
            if not all((fb + i) in received for i in range(f.size)):
                continue
            raw = bytes(received[fb + i] for i in range(f.size))
            u = int.from_bytes(raw, f.byteorder)
            n = f.name
            if n.startswith("EPS_") and "Gauge" not in n and n.endswith("Voltage"):
                v = (u >> 3) * 0.008
                nominal = rail_nominal_v(n)
                if v > 26.0 or (nominal and v > 0.5 and abs(v - nominal) > 0.15 * nominal):
                    bad += 1
            elif n.startswith("EPS_") and "Gauge" not in n and n.endswith("Current"):
                c = u >> 3
                c = c - 0x2000 if c & 0x1000 else c
                bad += abs(c * 4e-3) > 5.0
            elif "SOC" in n:
                bad += u / 100 > 100.0
            elif "Temperature" in n and f.size == 1:
                t = int.from_bytes(raw, f.byteorder, signed=True)
                bad += not TEMP_RANGE_C[0] <= t <= TEMP_RANGE_C[1]
        return ina, bad


def _no_scorer(received: dict[int, int]) -> tuple[int, int]:
    return 0, 0


# ---------------------------------------------------------------- demod

def channel_filter(x: np.ndarray, fs: float) -> np.ndarray:
    if fs <= 2.5 * DETECT_CUTOFF_HZ:
        return x
    taps = sps.firwin(129, DETECT_CUTOFF_HZ, fs=fs)
    return sps.fftconvolve(x, taps, "same")


def find_bursts(x: np.ndarray, fs: float, min_dur_s=0.05, thresh_db=3.0):
    """Bursts in a capture as (start, stop, dB over floor). Detection runs in
    a +-15 kHz channel: at 200 ksps a 12 dB (9.6 kHz) beacon is only ~1 dB
    above the full-band floor."""
    x = channel_filter(x, fs)
    win = max(1, int(fs * 0.002))
    p = np.convolve(np.abs(x) ** 2, np.ones(win) / win, "same")
    db = 10 * np.log10(p + 1e-30)
    floor = np.median(db)
    on = db > floor + thresh_db
    edges = np.diff(on.astype(np.int8), prepend=0, append=0)
    starts, stops = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    bursts = [(a, b) for a, b in zip(starts, stops) if (b - a) > min_dur_s * fs]
    # merge bursts separated by short dropouts (long zero runs are still carrier)
    merged: list[list[int]] = []
    for a, b in bursts:
        if merged and a - merged[-1][1] < 0.02 * fs:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    fine_win = max(1, int(fs * 0.0005))
    fine_db = 10 * np.log10(np.convolve(np.abs(x) ** 2, np.ones(fine_win) / fine_win, "same") + 1e-30)
    out = []
    for a, b in merged:
        level = np.median(db[a:b])
        lo_edge = max(0, a - win)
        on_fine = np.flatnonzero(fine_db[lo_edge:b] > level - 3.0)
        a2 = lo_edge + int(on_fine[0]) if len(on_fine) else a
        out.append((int(a2), int(b), float(level - floor)))
    return out


def two_level(f: np.ndarray) -> tuple[float, float]:
    """1-D 2-means on instantaneous frequency."""
    lo, hi = np.percentile(f, 20), np.percentile(f, 80)
    for _ in range(30):
        thr = (lo + hi) / 2
        lo, hi = f[f < thr].mean(), f[f >= thr].mean()
    return float(lo), float(hi)


def fit_clock(t: np.ndarray, t_nom: float, span: float = 4e-3) -> tuple[float, float, np.ndarray]:
    """Bit clock (t0, T) from fractional transition times, and the inlier mask.

    Searches T within +-span of nominal for the period that phase-aligns the
    most transitions (circular resultant length), so long transition-free
    gaps cannot cause a miscounted bit index; then refines t0 and T by least
    squares over the inliers.
    """
    if len(t) < 4:
        return float(t[0]) if len(t) else 0.0, t_nom, np.ones(len(t), dtype=bool)
    rel = t - t[0]
    periods = t_nom * (1 + np.linspace(-span, span, 801))
    best_r, best_T = -1.0, t_nom
    for chunk in np.array_split(periods, 8):
        ph = np.exp(2j * np.pi * rel[None, :] / chunk[:, None])
        r = np.abs(ph.mean(axis=1))
        k = int(r.argmax())
        if r[k] > best_r:
            best_r, best_T = float(r[k]), float(chunk[k])
    T = best_T
    phase = np.angle(np.exp(2j * np.pi * rel / T).mean())
    t0 = t[0] + phase / (2 * np.pi) * T
    use = np.ones(len(t), dtype=bool)
    for _ in range(4):
        nk = np.round((t - t0) / T)
        resid = t - (t0 + nk * T)
        use = np.abs(resid) < 0.25 * T
        if use.sum() < 4:
            break
        A = np.vstack([np.ones(use.sum()), nk[use]]).T
        (t0, T), *_ = np.linalg.lstsq(A, t[use], rcond=None)
    return float(t0), float(T), use


@dataclass
class Burst:
    start: int
    stop: int
    snr_db: float
    f_lo: float = 0.0
    f_hi: float = 0.0
    period: float = 0.0
    t0: float = 0.0
    rms: float = 0.0
    n_trans: int = 0
    soft: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def marginal(self) -> int:
        mag = np.abs(self.soft)
        return int((mag < 0.25 * np.median(mag)).sum()) if mag.size else 0


def demod_burst(x: np.ndarray, fs: float, a: int, b: int, snr: float) -> Burst:
    pad = int(0.01 * fs)
    seg = x[max(0, a - pad): b + pad]
    # coarse centre from the spectrum, then shift to zero
    nfft = 1 << int(np.ceil(np.log2(len(seg))))
    spec = np.fft.fftshift(np.abs(np.fft.fft(seg, nfft)) ** 2)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / fs))
    spec = np.convolve(spec, np.ones(max(1, int(6000 * nfft / fs))), "same")   # ~6 kHz: both tones
    centre = float(freqs[int(spec.argmax())])
    n = np.arange(len(seg))
    seg = seg * np.exp(-2j * np.pi * centre * n / fs)
    taps = sps.firwin(101, 8000.0, fs=fs)
    seg = sps.lfilter(taps, 1.0, seg)[50:]           # remove group delay
    f = np.angle(seg[1:] * np.conj(seg[:-1])) * fs / (2 * np.pi)
    lo, hi = two_level(f[pad:len(f) - pad])
    thr = (lo + hi) / 2
    swing = (hi - lo) / 2
    d = f - thr

    # fractional mid-level crossings with full swing on both sides
    t_nom = fs / BAUD
    half = max(1, int(round(t_nom / 2)))
    idx = np.flatnonzero(np.sign(d[:-1]) != np.sign(d[1:]))
    idx = idx[(idx > half) & (idx < len(d) - half - 1)]
    keep = (np.abs(d[idx - half]) > 0.6 * swing) & (np.abs(d[idx + 1 + half]) > 0.6 * swing) \
        & (np.sign(d[idx - half]) != np.sign(d[idx + 1 + half]))
    idx = idx[keep]
    t = idx + d[idx] / (d[idx] - d[idx + 1])
    t0, T, use = fit_clock(t, t_nom)
    resid = t[use] - (t0 + np.round((t[use] - t0) / T) * T) if use.any() else np.zeros(1)

    # per-bit non-coherent two-tone correlation over each fitted bit period
    # (benchmarked ~2 dB better than sampling the discriminator at centre)
    first = int(np.ceil((pad - t0) / T))
    last = int(np.floor((len(d) - pad - t0) / T)) - 1
    w_lo = np.exp(-2j * np.pi * lo * np.arange(len(seg)) / fs)
    w_hi = np.exp(-2j * np.pi * hi * np.arange(len(seg)) / fs)
    c_lo, c_hi = np.concatenate([[0], np.cumsum(seg * w_lo)]), np.concatenate([[0], np.cumsum(seg * w_hi)])
    edges = np.round(t0 + np.arange(first, last + 1) * T).astype(int)
    edges = edges[(edges >= 0) & (edges < len(c_lo))]
    e_lo = np.abs(c_lo[edges[1:]] - c_lo[edges[:-1]])
    e_hi = np.abs(c_hi[edges[1:]] - c_hi[edges[:-1]])
    soft = (e_hi - e_lo) / (e_hi + e_lo + 1e-12)
    return Burst(a, b, snr, lo + centre, hi + centre, T, t0, float(np.sqrt(np.mean(resid ** 2))),
                 int(use.sum()), soft)


# ---------------------------------------------------------------- framing

@dataclass
class Candidate:
    frame: bytes                 # bytes from start_byte .. 273
    start_byte: int
    keep: tuple[int, ...]        # put-back sites (see insertion_sites)
    crc: str                     # "pass" | "pass after N bit flip(s) ..." | "fail" | "unverifiable"
    ina_violations: int
    ref_match: float
    static_match: int = 0        # received bytes equal to the reference frame
    implausible: int = 0         # physically impossible field values
    anchor: str = "reference"    # what fixed the frame position
    n_stuffing: int = 0          # keep indices below this are stuffing sites
    restored_zeros_at: tuple[int, ...] = ()   # frame bytes of dropped 0s put back
    restored_run_bits: int = 0   # summed length of the zero runs they went into

    @property
    def crc_passed(self) -> bool:
        return self.crc.startswith("pass")

    @property
    def stuffing_keep(self) -> tuple[int, ...]:
        return tuple(k for k in self.keep if k < self.n_stuffing)

    def rank(self) -> tuple:
        """Sort key: CRC, then plausibility, then fewest dropped 0s assumed,
        then the longest runs for them (a bit lost at random in time more
        likely falls in a longer run), then static reference bytes."""
        return (not self.crc_passed, self.implausible + self.ina_violations,
                len(self.restored_zeros_at), -self.restored_run_bits, -self.static_match)


def align_to_reference(data_bits: np.ndarray) -> tuple[int, float]:
    """Frame bit index of data_bits[0], by sliding 64-bit windows of
    received bits along the reference frame and keeping the best match."""
    best = (0, 0.0)
    sw = np.lib.stride_tricks.sliding_window_view(REFERENCE_BITS, 64)
    for w0 in range(0, max(1, len(data_bits) - 64), 32):
        win = data_bits[w0:w0 + 64]
        if len(win) < 64:
            break
        if win.sum() < 8:          # all-zero windows align anywhere
            continue
        score = (sw == win).mean(axis=1)
        k = int(score.argmax())
        if score[k] > best[1]:
            best = (k - w0, float(score[k]))
    return best


def _complete_frame(raw: np.ndarray, max_keep: int, slips: bool = True):
    """Destuff a flag-to-flag span; yield (frame bytes, keep, sites,
    n_stuffing) for every choice of put-back 0s that yields exactly
    FRAME_BITS. slips=False limits the choice to stuffing sites."""
    data, sites, n_stuffing = insertion_sites(raw)
    if not slips:
        sites = sites[:n_stuffing]
    missing = FRAME_BITS - len(data)
    if not 0 <= missing <= max_keep:
        return
    for keep in itertools.combinations(range(len(sites)), missing):
        yield bits_to_bytes(reinsert(data, sites, keep)), keep, sites, n_stuffing


def restored_bytes(sites: list[int], keep: tuple[int, ...], n_stuffing: int,
                   start_bit: int) -> tuple[int, ...]:
    """Frame byte of each dropped 0 put back by `keep` (stuffing sites
    excluded); start_bit is the frame bit of the first destuffed bit."""
    chosen = sorted(sites[k] for k in keep)
    return tuple((start_bit + sites[k] + chosen.index(sites[k])) // 8
                 for k in sorted(keep) if k >= n_stuffing)


def repair(raw: np.ndarray, conf: np.ndarray, max_keep: int, weakest=18, max_flips=3):
    """Flip up to max_flips of the `weakest` least-confident raw bits until
    the CRC passes. Returns (frame, keep, flipped positions, patterns tried).
    Stuffing choices only: more choices per flip pattern would raise the
    odds of a false CRC pass."""
    order = np.argsort(conf)[:weakest]
    tried = 0
    for k in range(1, max_flips + 1):
        for combo in itertools.combinations(order, k):
            trial = raw.copy()
            trial[list(combo)] ^= 1
            for frame, keep, _, _ in _complete_frame(trial, max_keep, slips=False):
                tried += 1
                if crc_ok(frame):
                    return frame, keep, sorted(int(c) for c in combo), tried
    return None


def flag_trains(flags: list[int]) -> list[tuple[int, int]]:
    """Group flags spaced exactly 8 bits apart (or sharing a 0, 7 bits)
    into (first_flag, last_flag) trains."""
    trains: list[list[int]] = []
    for f in flags:
        if trains and 7 <= f - trains[-1][1] <= 8:
            trains[-1][1] = f
        else:
            trains.append([f, f])
    return [tuple(t) for t in trains]


def frame_candidates(bits: np.ndarray, scorer=None, max_keep=3,
                     conf: np.ndarray | None = None) -> list[Candidate]:
    """bits: hard bits of one burst (one polarity); conf: |soft| per bit."""
    scorer = scorer or _no_scorer
    trains = flag_trains(find_flags(bits))
    if not trains:
        return []
    # closing = the longest flag train (SCION-X sends ~10 closing flags);
    # opening = a train of >= 2 flags ending ~one frame earlier, if any.
    # Isolated flag patterns are ignored: one bit error in an unscrambled
    # frame easily fakes one.
    multi = [t for t in trains if t[1] - t[0] >= 7] or trains

    def gap_before(t):
        prev = [u[1] + 8 for u in multi if u[1] < t[0]]
        return t[0] - (max(prev) if prev else 0)
    close = max(multi, key=gap_before)
    body_end = close[0]
    body_start = 0
    for t0_, t1_ in trains:
        if t1_ < body_end and t1_ - t0_ >= 8 and 0.9 * FRAME_BITS < body_end - (t1_ + 8) < 1.25 * FRAME_BITS:
            body_start = t1_ + 8
    raw = bits[body_start:body_end]
    data, sites, n_stuffing = insertion_sites(raw)
    opened = body_start > 0
    if opened and conf is not None:
        direct = [f for f, *_ in _complete_frame(raw, max_keep) if crc_ok(f)]
        if not direct:
            fixed = repair(raw, conf[body_start:body_end], max_keep)
            if fixed:
                frame, keep, flipped, tried = fixed
                return [Candidate(frame, 0, keep, f"pass after {len(flipped)} bit flip(s) "
                                  f"(of {tried} patterns tried)", 0, 1.0, 0, 0, "opening flags",
                                  n_stuffing=max(keep, default=-1) + 1)]

    if opened:
        start_bit, score = 0, 1.0
    else:
        start_bit, score = align_to_reference(data)
    expected = FRAME_BITS - start_bit
    missing = expected - len(data)
    if opened and not 0 <= missing <= max_keep:
        # opening train was not real (or the length is corrupt): treat as headerless
        return frame_candidates(bits[body_start:], scorer, max_keep,
                                None if conf is None else conf[body_start:])
    # aligning against the reference is costly per trial: headerless frames
    # try stuffing choices there; the closing-flag count-back below also
    # tries dropped 0s
    pool = len(sites) if opened else n_stuffing
    if missing < 0 or missing > max_keep:
        keeps = [()]
    else:
        keeps = list(itertools.combinations(range(pool), missing))
    trials = [(keep, "opening flags" if opened else "reference") for keep in keeps]
    if not opened and close[1] - close[0] >= 24:
        # A late enough key-up leaves no static reference bytes on air, so
        # align_to_reference has nothing to lock to. A real closing train
        # (>= 4 flags) still marks the frame end: count back from it for
        # every plausible choice of put-back 0s (a skipped stuffing bit or a
        # dropped 0 shifts everything before it by one bit).
        trials += [(keep, "closing flags") for k in range(max_keep + 1)
                   for keep in itertools.combinations(range(len(sites)), k)]
    cands: list[Candidate] = []
    for keep, anchor in trials:
        full = reinsert(data, sites, keep)
        if anchor == "closing flags":
            start_bit = FRAME_BITS - len(full)
            n = min(len(full), FRAME_BITS - start_bit)
            score = float((full[:n] == REFERENCE_BITS[start_bit:start_bit + n]).mean()) if n > 0 else 0.0
        elif not opened:
            start_bit, score = align_to_reference(full)
        if start_bit < 0:
            full = full[-start_bit:]
            sb = 0
        else:
            sb = start_bit
        lead = (-sb) % 8
        body = full[lead:]
        first_byte = (sb + lead) // 8
        frame = bits_to_bytes(body)[:max(0, FRAME_BYTES - first_byte)]
        if not frame:
            continue
        received = {first_byte + i: v for i, v in enumerate(frame)}
        if first_byte == 0 and len(frame) == FRAME_BYTES:
            crc = "pass" if crc_ok(frame) else "fail"
        else:
            crc = "unverifiable"
        static = sum(1 for i, v in received.items() if i < 272 and v == REFERENCE[i] and v != 0)
        restored = restored_bytes(sites, keep, n_stuffing, start_bit)
        run_bits = sum(int(np.argmax(np.append(data[sites[k]:], 1))) for k in keep if k >= n_stuffing)
        same = next((c for c in cands if c.start_byte == first_byte and c.frame == frame), None)
        if same is not None:
            # both anchors put the frame in the same place: one candidate,
            # stronger evidence
            if anchor not in same.anchor:
                same.anchor += f" + {anchor}"
            if (len(restored), -run_bits) < (len(same.restored_zeros_at), -same.restored_run_bits):
                same.keep, same.restored_zeros_at, same.restored_run_bits = keep, restored, run_bits
            continue
        ina, bad = scorer(received)
        cands.append(Candidate(frame, first_byte, keep, crc, ina, score, static, bad, anchor,
                               n_stuffing, restored, run_bits))
    cands.sort(key=Candidate.rank)
    return cands


# ---------------------------------------------------------------- burst API

@dataclass
class BurstDecode:
    """Outcome for one burst. status: "crc_pass" (complete, CRC verified),
    "unverified" (frame position found, CRC unverifiable or failed), or
    "undecoded" (no alignable frame)."""
    status: str
    frame: bytes = b""
    start_byte: int = 0
    crc: str = ""
    polarity: int = 1
    keep: tuple[int, ...] = ()
    restored_zeros_at: tuple[int, ...] = ()
    anchor: str = ""
    ambiguous: bool = False
    implausible: int = 0
    ina_violations: int = 0
    static_match: int = 0
    snr_db: float = 0.0
    f_lo_hz: float = 0.0
    f_hi_hz: float = 0.0
    bit_rate: float = 0.0
    clock_rms: float = 0.0
    transitions: int = 0
    bits: int = 0
    marginal_bits: int = 0

    def meta(self) -> dict:
        """JSON-ready description carried with the frame to the GSS."""
        return {
            "v": 1, "status": self.status, "start_byte": self.start_byte, "crc": self.crc,
            "polarity": self.polarity, "kept_stuffing_sites": list(self.keep),
            "restored_zeros_at_bytes": list(self.restored_zeros_at),
            "anchor": self.anchor, "ambiguous": self.ambiguous,
            "implausible": self.implausible, "ina_violations": self.ina_violations,
            "static_ref_bytes": self.static_match, "snr_db": round(self.snr_db, 1),
            "tones_hz": [round(self.f_lo_hz), round(self.f_hi_hz)],
            "bit_rate": round(self.bit_rate, 1), "clock_rms_samples": round(self.clock_rms, 2),
            "transitions": self.transitions, "bits": self.bits, "marginal_bits": self.marginal_bits,
        }


def decode_burst(x: np.ndarray, fs: float, a: int, b: int, snr: float,
                 scorer=None) -> BurstDecode:
    """Demodulate x[a:b] and return its best frame (either polarity)."""
    bu = demod_burst(x, fs, a, b, snr)
    stats = dict(snr_db=snr, f_lo_hz=bu.f_lo, f_hi_hz=bu.f_hi, bit_rate=fs / bu.period,
                 clock_rms=bu.rms, transitions=bu.n_trans, bits=len(bu.soft),
                 marginal_bits=bu.marginal)
    results: list[tuple[int, Candidate]] = []
    for pol in (+1, -1):
        hard = (pol * bu.soft > 0).astype(np.uint8)
        for c in frame_candidates(hard, scorer, conf=np.abs(bu.soft)):
            # a headerless candidate with no anchoring evidence (typically the
            # wrong polarity locking onto spurious flags) is dropped
            if c.crc_passed or c.start_byte == 0 or c.static_match > 0 or "closing flags" in c.anchor:
                results.append((pol, c))
    if not results:
        return BurstDecode(status="undecoded", **stats)
    results.sort(key=lambda pc: pc[1].rank())
    pol, top = results[0]
    ties = [c for _, c in results if c.crc == top.crc
            and c.implausible + c.ina_violations == top.implausible + top.ina_violations
            and len(c.restored_zeros_at) == len(top.restored_zeros_at)
            and c.static_match == top.static_match]
    return BurstDecode(
        status="crc_pass" if top.crc_passed else "unverified",
        frame=top.frame, start_byte=top.start_byte, crc=top.crc, polarity=pol, keep=top.stuffing_keep,
        restored_zeros_at=top.restored_zeros_at,
        anchor=top.anchor, ambiguous=len(ties) > 1, implausible=top.implausible,
        ina_violations=top.ina_violations, static_match=top.static_match, **stats)


class BurstCollector:
    """Live burst finder for a continuous complex stream.

    Coarse detection on 2 ms blocks of channel-filtered power against a
    running median floor; once a burst has ended and PAD_S of trailing
    samples have arrived, the raw segment [start - PAD_S, end + PAD_S] is
    handed out for find_bursts + decode_burst exactly as on a recording
    (the pads keep the burst under half the segment, so the segment median
    stays the noise floor).
    """

    PAD_S = 0.5
    BLOCK_S = 0.002
    HISTORY_S = 4.0
    MAX_BURST_S = 2.5
    THRESH_DB = 3.0
    HANG_S = 0.03                 # tolerate short dropouts inside a burst

    def __init__(self, fs: float) -> None:
        self.fs = float(fs)
        self.block = max(1, int(self.fs * self.BLOCK_S))
        self.taps = sps.firwin(129, DETECT_CUTOFF_HZ, fs=self.fs) if self.fs > 2.5 * DETECT_CUTOFF_HZ else None
        self.zi = np.zeros(128, dtype=np.complex128) if self.taps is not None else None
        self.cap = int(self.fs * self.HISTORY_S)
        self.ring = np.zeros(self.cap, dtype=np.complex64)
        self.total = 0                    # samples ever pushed
        self.pending = np.empty(0, dtype=np.complex64)
        self.floor_hist: list[float] = []
        self.on_start: int | None = None
        self.last_on: int | None = None
        self.ready: list[tuple[int, int]] = []    # (start, end) sample indices awaiting tail

    def _store(self, x: np.ndarray) -> None:
        n = len(x)
        if n >= self.cap:
            self.ring[:] = x[-self.cap:]
        else:
            pos = self.total % self.cap
            first = min(n, self.cap - pos)
            self.ring[pos:pos + first] = x[:first]
            self.ring[:n - first] = x[first:]
        self.total += n

    def _slice(self, a: int, b: int) -> np.ndarray | None:
        if a < self.total - self.cap or b > self.total or a >= b:
            return None
        idx = np.arange(a, b) % self.cap
        return self.ring[idx].copy()

    def push(self, x: np.ndarray) -> list[tuple[np.ndarray, int, int]]:
        """Feed samples; returns finished bursts as (segment, segment start
        sample, burst end sample)."""
        x = np.asarray(x, dtype=np.complex64)
        base = self.total
        self._store(x)
        if self.taps is not None:
            y, self.zi = sps.lfilter(self.taps, 1.0, x, zi=self.zi)
        else:
            y = x
        buf = np.concatenate((self.pending, y.astype(np.complex64)))
        nblk = len(buf) // self.block
        blk_start = base - len(self.pending)
        p = (np.abs(buf[:nblk * self.block].reshape(nblk, self.block)) ** 2).mean(axis=1) if nblk else []
        self.pending = buf[nblk * self.block:]
        hang = int(self.HANG_S * self.fs)
        for k in range(nblk):
            db = 10 * np.log10(float(p[k]) + 1e-30)
            s = blk_start + k * self.block
            floor = float(np.median(self.floor_hist)) if len(self.floor_hist) >= 50 else None
            on = floor is not None and db > floor + self.THRESH_DB
            if on:
                if self.on_start is None:
                    self.on_start = s
                self.last_on = s + self.block
            else:
                self.floor_hist.append(db)
                if len(self.floor_hist) > 2500:          # ~5 s of quiet blocks
                    del self.floor_hist[:500]
                if self.on_start is not None and s - self.last_on > hang:
                    if (self.last_on - self.on_start) >= 0.03 * self.fs:
                        self.ready.append((self.on_start, self.last_on))
                    self.on_start = self.last_on = None
            if self.on_start is not None and s - self.on_start > self.MAX_BURST_S * self.fs:
                # a carrier that never ends is not a beacon: drop and re-arm
                self.on_start = self.last_on = None
        out = []
        pad = int(self.PAD_S * self.fs)
        still = []
        for a, b in self.ready:
            if self.total >= b + pad:
                seg = self._slice(a - pad, b + pad)
                if seg is not None:
                    out.append((seg, a - pad, b))
            else:
                still.append((a, b))
        self.ready = still
        return out


def decode_segment(seg: np.ndarray, fs: float, scorer=None) -> list[tuple[int, int, BurstDecode]]:
    """find_bursts + decode_burst over one collected segment."""
    out = []
    for a, b, snr in find_bursts(seg, fs):
        out.append((a, b, decode_burst(seg, fs, a, b, snr, scorer)))
    return out


# ---------------------------------------------------------------- offline CLI

def _default_fields_path() -> Path:
    return (Path(__file__).resolve().parent.parent / "mav_gss_lib" / "missions" / "scionx"
            / "beacon_fields.json")


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Decode SCION-X beacons in a SigMF/cf32 capture "
                                             "with the same code MAV_DUO runs live.")
    ap.add_argument("iq", type=Path)
    ap.add_argument("--rate", type=float, help="sample rate; read from .sigmf-meta when omitted")
    ap.add_argument("--fields", type=Path, default=_default_fields_path(),
                    help="beacon field map JSON (local, not in the repo)")
    args = ap.parse_args(argv)
    fs = args.rate
    meta = args.iq.with_suffix(".sigmf-meta")
    if fs is None and meta.is_file():
        fs = float(json.loads(meta.read_text())["global"]["core:sample_rate"])
    if fs is None:
        ap.error("--rate is required when there is no .sigmf-meta beside the capture")
    scorer = FieldScorer.from_json(args.fields)
    x = np.fromfile(args.iq, dtype=np.complex64)
    bursts = find_bursts(x, fs)
    print(f"{args.iq.name}: {len(x) / fs:.2f} s, {len(bursts)} burst(s), "
          f"field map {'loaded' if scorer else 'NOT found (no plausibility scoring)'}")
    for a, b, snr in bursts:
        r = decode_burst(x, fs, a, b, snr, scorer)
        print(f"\n=== {a / fs:.3f}-{b / fs:.3f} s  {r.status.upper()}"
              f"{'  AMBIGUOUS' if r.ambiguous else ''}")
        print("  " + json.dumps(r.meta()))
        if r.frame:
            print(f"  frame bytes {r.start_byte}..{r.start_byte + len(r.frame) - 1}: {r.frame.hex()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
