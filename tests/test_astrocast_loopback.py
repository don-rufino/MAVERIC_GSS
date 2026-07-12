"""Deterministic Astrocast loopback through the production MAV_ASTROCAST banks.

Synthesizes the complete non-compliant FX.25 chain exactly as
gr-satellites' astrocast_fx25_deframer expects to undo it:

    AX.25 UI frame -> [0x7E] frame FCS16-LE [0x7E] pad-to-223 (byte-aligned
    HDLC, no bit stuffing) -> RS(255,223) dual-basis (satellites.encode_rs,
    the same library the deframer decodes with) -> per-byte bit reflection
    -> 64-bit syncword prepend -> NRZI -> GFSK h=2 (1k2 baud, 1200 Hz
    deviation) at the 200 ksps acquisition rate.

The burst replays through `MAV_ASTROCAST.py --headless --iqfile`, which
feeds the EXACT live decode chain (13 discriminator branches + the
matched-filter fine bank) — the wav path bypasses the banks and proves
nothing about them. Decoded PDUs are collected from the flowgraph's ZMQ
PUB, the same bus the GSS consumes in production.

Gated behind MAVERIC_FULL_GR=1 (spawns a 26-branch flowgraph); once
enabled, every failure mode fails the test.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

import numpy as np

TESTS_DIR = Path(__file__).resolve().parent
CODE_DIR = TESTS_DIR.parent
sys.path.insert(0, str(CODE_DIR))
sys.path.insert(0, str(TESTS_DIR))

from mav_gss_lib.platform.framing.ax25 import _crc_ccitt  # noqa: E402

from test_decode_loopback import (  # noqa: E402
    FS, GNURADIO, GNURADIO_PYTHON, _gfsk_iq, _requires_full_gr)
from test_mission_astrocast import BEACON_FRAME_1, BEACON_FRAME_2  # noqa: E402

BEACON_BAUD = 1200
BEACON_DEVIATION_HZ = 1200  # h = 2: the dual-tone geometry the MF bank matches
RS_BLOCK_LEN = 223
# From gr-satellites astrocast_fx25_deframer._syncword (bit order as deframed).
ASTROCAST_SYNCWORD = ("01110101111110101100000110100011"
                      "01011000110100000110010001110110")

_RS_ENCODE_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    from gnuradio import gr, blocks
    import pmt
    import satellites

    data = bytes.fromhex(sys.argv[1])
    enc = satellites.encode_rs(True, 1)   # dual basis, interleave 1 —
    dbg = blocks.message_debug()          # mirrors the deframer's decode_rs
    tb = gr.top_block()
    tb.msg_connect((enc, 'out'), (dbg, 'store'))
    tb.start()
    enc.to_basic_block()._post(
        pmt.intern('in'),
        pmt.cons(pmt.PMT_NIL, pmt.init_u8vector(len(data), list(data))))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and dbg.num_messages() < 1:
        time.sleep(0.05)
    out = (bytes(pmt.u8vector_elements(pmt.cdr(dbg.get_message(0))))
           if dbg.num_messages() else b"")
    print("RS " + out.hex(), flush=True)
    os._exit(0)
    """
)


def _rs_encode_dual_basis(block: bytes) -> bytes:
    assert len(block) == RS_BLOCK_LEN
    proc = subprocess.run(
        [GNURADIO_PYTHON, "-u", "-c", _RS_ENCODE_SCRIPT, block.hex()],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"encode_rs subprocess failed:\n{proc.stderr[-800:]}")
    for line in proc.stdout.splitlines():
        if line.startswith("RS "):
            codeword = bytes.fromhex(line[3:])
            assert len(codeword) == 255, f"encode_rs returned {len(codeword)} bytes"
            return codeword
    raise AssertionError(f"encode_rs printed no result:\n{proc.stdout[-400:]}")


def _reflect(data: bytes) -> bytes:
    bits = np.unpackbits(np.frombuffer(data, np.uint8)).reshape(-1, 8)
    return np.packbits(bits[:, ::-1]).tobytes()


def astrocast_wire_bytes(ax25_frame: bytes) -> bytes:
    """Full over-the-air byte stream (pre-GFSK, NRZI channel bits packed
    MSB-first) for one Astrocast beacon."""
    assert b"\x7e" not in ax25_frame, "0x7E in the frame would truncate the HDLC scan"
    fcs = _crc_ccitt(ax25_frame)
    inner = b"\x7e" + ax25_frame + fcs.to_bytes(2, "little") + b"\x7e"
    block = inner.ljust(RS_BLOCK_LEN, b"\x7e")  # flag fill after the close
    codeword = _rs_encode_dual_basis(block)
    data_bits = np.unpackbits(np.frombuffer(_reflect(codeword), np.uint8))
    sync_bits = np.array([int(c) for c in ASTROCAST_SYNCWORD], dtype=np.uint8)
    # Pre-NRZI zeros toggle every symbol -> alternating channel tones, the
    # classic clock-recovery run-in. 48 bytes ~ 0.32 s at 1k2.
    preamble = np.zeros(48 * 8, dtype=np.uint8)
    tail = np.zeros(16, dtype=np.uint8)
    bits = np.concatenate([preamble, sync_bits, data_bits, tail])
    # NRZI: 1 = hold, 0 = toggle (inverse of satellites.nrzi_decode).
    levels = np.empty_like(bits)
    level = 1
    for i, bit in enumerate(bits):
        if bit == 0:
            level ^= 1
        levels[i] = level
    return np.packbits(levels).tobytes()


def _beacon_burst(ax25_frame: bytes, offset_hz: float) -> np.ndarray:
    iq = _gfsk_iq(astrocast_wire_bytes(ax25_frame), BEACON_BAUD, BEACON_DEVIATION_HZ)
    if offset_hz:
        iq = iq * np.exp(2j * np.pi * offset_hz * np.arange(iq.size) / FS)
    return iq


def _compose(bursts: list[np.ndarray], gap_s: float = 0.5,
             noise_rms: float = 0.02, seed: int = 20260711) -> np.ndarray:
    gap = np.zeros(int(gap_s * FS), dtype=np.complex128)
    parts = [gap]
    for burst in bursts:
        parts.extend((burst, gap))
    record = np.concatenate(parts)
    rng = np.random.default_rng(seed)
    record = record + noise_rms / np.sqrt(2.0) * (
        rng.standard_normal(record.size) + 1j * rng.standard_normal(record.size))
    return record.astype(np.complex64)


_REPLAY_ADDR_COUNTER = itertools.count()


def _throwaway_ipc_addr() -> str:
    """Collision-free per-call frame-bus endpoint. ipc (not fixed TCP ports)
    so parallel test runs cannot collide; kept under gettempdir with a short
    name because macOS caps unix-socket paths at 104 chars."""
    name = f"mavtest_{os.getpid()}_{next(_REPLAY_ADDR_COUNTER)}.ipc"
    return "ipc://" + os.path.join(tempfile.gettempdir(), name)


def replay_iq_through_banks(record: np.ndarray | Path, *,
                            timeout_s: float = 120.0,
                            zmq_addr: str | None = None) -> list[bytes]:
    """Run IQ through `MAV_ASTROCAST.py --headless --iqfile`, collecting
    deframed PDUs from its ZMQ PUB (the production frame-bus path, bound to
    a throwaway ipc endpoint). Raises AssertionError on any failure mode —
    never skips; the flowgraph child is always terminated."""
    import zmq
    import pmt

    addr = zmq_addr or _throwaway_ipc_addr()
    with tempfile.TemporaryDirectory() as tmp:
        if isinstance(record, Path):
            iq_path = record
        else:
            iq_path = Path(tmp) / "record.cf32"
            record.tofile(iq_path)
        ctx = zmq.Context()
        sub = ctx.socket(zmq.SUB)
        sub.connect(addr)
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        proc = subprocess.Popen(
            [GNURADIO_PYTHON, "-u", str(GNURADIO / "MAV_ASTROCAST.py"),
             "--headless", "--iqfile", str(iq_path), "--zmq-addr", addr],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            frames: list[bytes] = []
            deadline = time.monotonic() + timeout_s
            exited_at = None
            while time.monotonic() < deadline:
                if sub.poll(200):
                    msg = pmt.deserialize_str(sub.recv())
                    frames.append(bytes(pmt.u8vector_elements(pmt.cdr(msg))))
                    continue
                if proc.poll() is not None:
                    if exited_at is None:
                        exited_at = time.monotonic()
                    elif time.monotonic() - exited_at > 1.0:
                        break  # drained for a second past process exit
            if proc.poll() is None:
                out, err = "", ""
                try:
                    proc.kill()
                    out, err = proc.communicate(timeout=10)
                except Exception:
                    pass
                raise AssertionError(
                    f"MAV_ASTROCAST --iqfile did not finish in {timeout_s}s:\n"
                    f"{err[-800:]}")
            out, err = proc.communicate()
            if proc.returncode != 0:
                raise AssertionError(
                    f"MAV_ASTROCAST --iqfile exited rc={proc.returncode}:\n{err[-800:]}")
            return frames
        finally:
            # The child must never outlive the test — an exception anywhere
            # above (e.g. a deserialize error) must not orphan a 26-branch
            # flowgraph.
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.communicate(timeout=10)
                except Exception:
                    pass
            sub.close(0)
            ctx.term()


class AstrocastLoopbackTests(unittest.TestCase):
    """Synthetic FX.25 bursts through the full production decode banks."""

    maxDiff = None

    def setUp(self) -> None:
        _requires_full_gr()

    def test_synthetic_beacons_decode_through_live_banks(self):
        # Burst 1 rides at DC (centre branches); burst 2 at +1.7 kHz — 200 Hz
        # off the +1.5 kHz matched-filter centre and 300 Hz inside the +2 kHz
        # discriminator branch, i.e. the doppler-engaged residual regime the
        # fine bank exists for.
        record = _compose([
            _beacon_burst(BEACON_FRAME_1, 0.0),
            _beacon_burst(BEACON_FRAME_2, 1_700.0),
        ])
        frames = replay_iq_through_banks(record)
        decoded = set(frames)
        self.assertIn(BEACON_FRAME_1, decoded,
                      f"DC burst not decoded (got {len(frames)} PDUs)")
        self.assertIn(BEACON_FRAME_2, decoded,
                      f"+1.7 kHz burst not decoded (got {len(frames)} PDUs)")
        self.assertEqual(decoded, {BEACON_FRAME_1, BEACON_FRAME_2},
                         "unexpected extra payloads decoded")


if __name__ == "__main__":
    unittest.main()
