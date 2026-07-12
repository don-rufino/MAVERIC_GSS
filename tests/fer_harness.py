"""Calibrated frame-error-rate harness for the production decode paths.

Measures decode probability vs explicit Eb/N0 through the exact production
decoder instantiations (Mode 5 via gr_satellites_flowgraph + database;
Astrocast via MAV_ASTROCAST --iqfile), with the statistics discipline a
regression gate needs:

  * Explicit Eb/N0. Bursts are unit-envelope GFSK, so per-sample noise
    variance is sigma^2 = (FS/baud) / 10^(EbN0_dB/10). Carson-bandwidth
    CNR is reported alongside: CNR = EbN0 + 10log10(baud/B_carson).
  * Paired seeds. One base seed freezes the whole channel-realization
    set — noise, per-trial CFO, timing phase, payload — reused at every
    Eb/N0 point (noise scaled, not redrawn) and by every decoder config
    under comparison. Curve separation between two configs is then the
    decoders' doing, not the channel's.
  * Wilson 95% confidence intervals per point, and linear-interpolated
    Eb/N0 thresholds at 50% and 90% decode probability.

Trials are batched: all N trials of a point ride one record (distinct
payloads, randomized inter-burst gaps = timing phases) through one
flowgraph spawn, so a full point costs one construction.

CLI (full curves, local tool use):

    python3 tests/fer_harness.py --db ROADS_DECODER.yml --baud 4800 \
        --dev 1200 --ebn0 6:14:1 --trials 32
    python3 tests/fer_harness.py --path astrocast --ebn0 8:16:2 --trials 16

The coarse regression gate lives in tests/test_fer_baseline.py
(MAVERIC_FER=1). Detector prototypes (blanker, coherent CPM) are judged
by FER-curve separation on this harness at matched seeds — not by
single-shot lowest-CNR decodes.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

from test_decode_loopback import (  # noqa: E402
    FS, GNURADIO, _gfsk_iq, _run_decoder)

DEFAULT_BASE_SEED = 20260711
DEFAULT_CFO_SPAN_HZ = 500.0   # uniform +/- span, the post-Doppler residual regime
MODE5_PAYLOAD_LEN = 40        # wire frame is fixed-length regardless (RS pad)


# ---------------------------------------------------------------- statistics

def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


@dataclass
class FerPoint:
    ebn0_db: float
    decoded: int
    trials: int

    @property
    def p(self) -> float:
        return self.decoded / self.trials if self.trials else 0.0

    @property
    def ci(self) -> tuple[float, float]:
        return wilson_ci(self.decoded, self.trials)


def interp_threshold(points: list[FerPoint], target_p: float) -> float | None:
    """Eb/N0 at which decode probability crosses target_p (linear interp
    between the bracketing points; None when the sweep never brackets it)."""
    pts = sorted(points, key=lambda x: x.ebn0_db)
    for lo, hi in zip(pts, pts[1:]):
        if lo.p < target_p <= hi.p:
            if hi.p == lo.p:
                return hi.ebn0_db
            frac = (target_p - lo.p) / (hi.p - lo.p)
            return lo.ebn0_db + frac * (hi.ebn0_db - lo.ebn0_db)
    return None


# ---------------------------------------------------------------- channel

def noise_sigma(ebn0_db: float, baud: float) -> float:
    """Per-complex-sample AWGN sigma for a unit-envelope burst at FS."""
    return math.sqrt((FS / baud) / (10.0 ** (ebn0_db / 10.0)))


def carson_cnr_db(ebn0_db: float, baud: float, deviation_hz: float) -> float:
    b_carson = 2.0 * (deviation_hz + baud / 2.0)
    return ebn0_db + 10.0 * math.log10(baud / b_carson)


@dataclass
class ChannelSet:
    """The frozen per-trial channel parameters (paired across points/configs)."""
    payloads: list[bytes]
    cfos_hz: list[float]
    lead_gaps: list[int]      # samples; randomizes timing phase per trial
    noise_seed: int


def make_channel_set(trials: int, base_seed: int,
                     cfo_span_hz: float = DEFAULT_CFO_SPAN_HZ,
                     payload_len: int = MODE5_PAYLOAD_LEN) -> ChannelSet:
    rng = np.random.default_rng(base_seed)
    payloads = [bytes([i & 0xFF]) + rng.bytes(payload_len - 1) for i in range(trials)]
    cfos = rng.uniform(-cfo_span_hz, cfo_span_hz, size=trials).tolist()
    # 0..2 symbol-ish extra lead per trial on top of the base gap
    leads = rng.integers(0, int(0.02 * FS), size=trials).tolist()
    return ChannelSet(payloads=payloads, cfos_hz=cfos, lead_gaps=leads,
                      noise_seed=int(rng.integers(0, 2**31 - 1)))


def compose_fer_record(bursts: list[np.ndarray], lead_gaps: list[int],
                       sigma: float, noise_seed: int,
                       gap_s: float = 0.15) -> np.ndarray:
    gap = np.zeros(int(gap_s * FS), dtype=np.complex128)
    parts: list[np.ndarray] = []
    for burst, lead in zip(bursts, lead_gaps):
        parts.append(gap)
        parts.append(np.zeros(lead, dtype=np.complex128))
        parts.append(burst)
    parts.append(gap)
    signal = np.concatenate(parts)
    # Unit-variance noise from the frozen seed, scaled per point — the same
    # realization underlies every Eb/N0 point and every config compared.
    rng = np.random.default_rng(noise_seed)
    unit = (rng.standard_normal(signal.size)
            + 1j * rng.standard_normal(signal.size)) / np.sqrt(2.0)
    return (signal + sigma * unit).astype(np.complex64)


def _apply_cfo(iq: np.ndarray, cfo_hz: float) -> np.ndarray:
    if not cfo_hz:
        return iq
    return iq * np.exp(2j * np.pi * cfo_hz * np.arange(iq.size) / FS)


# ---------------------------------------------------------------- Mode 5 path

def mode5_bursts(channel: ChannelSet, baud: float, deviation_hz: float) -> list[np.ndarray]:
    from mav_gss_lib.platform.framing.asm_golay import build_asm_golay_frame
    return [
        _apply_cfo(_gfsk_iq(build_asm_golay_frame(payload), baud, deviation_hz), cfo)
        for payload, cfo in zip(channel.payloads, channel.cfos_hz)
    ]


def run_point_mode5(db: str, options: str, baud: float, deviation_hz: float,
                    ebn0_db: float, channel: ChannelSet,
                    bursts: list[np.ndarray] | None = None) -> FerPoint:
    bursts = bursts if bursts is not None else mode5_bursts(channel, baud, deviation_hz)
    record = compose_fer_record(bursts, channel.lead_gaps,
                                noise_sigma(ebn0_db, baud), channel.noise_seed)
    wait_s = record.size / FS * 3.0 + 30.0
    pdus = _run_decoder(GNURADIO / db, options, record, wait_s=wait_s)
    decoded = set(pdus)
    k = sum(1 for p in channel.payloads if p in decoded)
    return FerPoint(ebn0_db=ebn0_db, decoded=k, trials=len(channel.payloads))


# ---------------------------------------------------------------- Astrocast path

def astrocast_channel_set(trials: int, base_seed: int,
                          cfo_span_hz: float = 2_500.0) -> ChannelSet:
    """Astrocast variant: AX.25 beacon frames (no 0x7E), wider CFO span
    matching the fine-bank coverage."""
    from test_mission_astrocast import _AX25_UI_HEADER
    rng = np.random.default_rng(base_seed)
    payloads = []
    for i in range(trials):
        text = f"$FER,{i:03d}," + "".join(
            rng.choice(list("0123456789ABCDEF"), size=32)) + "*00"
        payloads.append(_AX25_UI_HEADER + text.encode("ascii").ljust(171, b" "))
    cfos = rng.uniform(-cfo_span_hz, cfo_span_hz, size=trials).tolist()
    leads = rng.integers(0, int(0.02 * FS), size=trials).tolist()
    return ChannelSet(payloads=payloads, cfos_hz=cfos, lead_gaps=leads,
                      noise_seed=int(rng.integers(0, 2**31 - 1)))


def run_point_astrocast(ebn0_db: float, channel: ChannelSet, *,
                        zmq_port: int = 52092) -> FerPoint:
    from test_astrocast_loopback import (
        BEACON_BAUD, BEACON_DEVIATION_HZ, astrocast_wire_bytes,
        replay_iq_through_banks)
    bursts = [
        _apply_cfo(_gfsk_iq(astrocast_wire_bytes(frame), BEACON_BAUD,
                            BEACON_DEVIATION_HZ), cfo)
        for frame, cfo in zip(channel.payloads, channel.cfos_hz)
    ]
    record = compose_fer_record(bursts, channel.lead_gaps,
                                noise_sigma(ebn0_db, BEACON_BAUD),
                                channel.noise_seed, gap_s=0.4)
    timeout_s = record.size / FS + 90.0   # replay is throttled to realtime
    frames = set(replay_iq_through_banks(record, zmq_port=zmq_port,
                                         timeout_s=timeout_s))
    k = sum(1 for p in channel.payloads if p in frames)
    return FerPoint(ebn0_db=ebn0_db, decoded=k, trials=len(channel.payloads))


# ---------------------------------------------------------------- sweeps + CLI

def sweep_mode5(db: str, options: str, baud: float, deviation_hz: float,
                ebn0_points: list[float], trials: int,
                base_seed: int = DEFAULT_BASE_SEED,
                log=print) -> list[FerPoint]:
    channel = make_channel_set(trials, base_seed)
    bursts = mode5_bursts(channel, baud, deviation_hz)  # reused across points
    points = []
    for ebn0 in ebn0_points:
        pt = run_point_mode5(db, options, baud, deviation_hz, ebn0, channel,
                             bursts=bursts)
        lo, hi = pt.ci
        log(f"  Eb/N0 {ebn0:5.1f} dB (Carson CNR "
            f"{carson_cnr_db(ebn0, baud, deviation_hz):5.1f} dB): "
            f"{pt.decoded:3d}/{pt.trials}  p={pt.p:.2f}  CI[{lo:.2f},{hi:.2f}]")
        points.append(pt)
    return points


def report_thresholds(points: list[FerPoint], baud: float, deviation_hz: float,
                      log=print) -> dict[str, float | None]:
    out = {}
    for label, target in (("p50", 0.5), ("p90", 0.9)):
        thr = interp_threshold(points, target)
        out[label] = thr
        if thr is None:
            log(f"  {label}: not bracketed by the sweep")
        else:
            log(f"  {label}: Eb/N0 {thr:.2f} dB "
                f"(Carson CNR {carson_cnr_db(thr, baud, deviation_hz):.2f} dB)")
    return out


def _parse_ebn0(spec: str) -> list[float]:
    if ":" in spec:
        start, stop, step = (float(x) for x in spec.split(":"))
        n = int(round((stop - start) / step)) + 1
        return [start + i * step for i in range(n)]
    return [float(x) for x in spec.split(",")]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--path", choices=["mode5", "astrocast"], default="mode5")
    ap.add_argument("--db", default="ROADS_DECODER.yml",
                    help="mode5: decoder database in gnuradio/")
    ap.add_argument("--options", default="--syncword_threshold 6")
    ap.add_argument("--baud", type=float, default=4800)
    ap.add_argument("--dev", type=float, default=1200)
    ap.add_argument("--ebn0", default="6:14:1", help="start:stop:step or comma list (dB)")
    ap.add_argument("--trials", type=int, default=32)
    ap.add_argument("--seed", type=int, default=DEFAULT_BASE_SEED)
    args = ap.parse_args()

    ebn0_points = _parse_ebn0(args.ebn0)
    if args.path == "mode5":
        print(f"FER sweep: {args.db} {args.baud:g} baud dev {args.dev:g} Hz, "
              f"{args.trials} trials/point, seed {args.seed}")
        points = sweep_mode5(args.db, args.options, args.baud, args.dev,
                             ebn0_points, args.trials, args.seed)
        report_thresholds(points, args.baud, args.dev)
    else:
        channel = astrocast_channel_set(args.trials, args.seed)
        print(f"FER sweep: MAV_ASTROCAST banks 1k2 h=2, "
              f"{args.trials} trials/point, seed {args.seed}")
        points = []
        for i, ebn0 in enumerate(ebn0_points):
            pt = run_point_astrocast(ebn0, channel, zmq_port=52092 + i)
            lo, hi = pt.ci
            print(f"  Eb/N0 {ebn0:5.1f} dB: {pt.decoded:3d}/{pt.trials}  "
                  f"p={pt.p:.2f}  CI[{lo:.2f},{hi:.2f}]")
            points.append(pt)
        report_thresholds(points, 1200, 1200)


if __name__ == "__main__":
    main()
