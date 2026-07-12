"""Deterministic decode-loopback tests through the production decoder databases.

Synthesizes over-the-air bursts with the repo's own framers
(platform.framing.asm_golay / ax25) and a numpy GFSK modulator at the
production RX rate (200 ksps = MAV_DUO samp_rate/rx_decim), then runs them
through gr_satellites_flowgraph instantiated exactly as MAV_DUO does it:
same database file, same samp_rate, same options string.

Positive matrix — every production MAV_DUO decoder profile:

    MAVERIC_DECODER.yml     9k6/3200
    ROADS_DECODER.yml       4k8/1200  9k6/2400
    SUOMI100_DECODER.yml    9k6/2400
    LUOJIA1_DECODER.yml     4k8/1600
    CATSAT_DECODER.yml      2k4/750
    AISTECHSAT2_DECODER.yml 4k8/1600
    INNOCUBE_DECODER.yml    9k6/3200
    SNIPE_DECODER.yml       4k8/1600  4k8/1200
    NUSHSAT1_DECODER.yml    1k2/575  2k4/600  2k4/800
    SHARJAHSAT_DECODER.yml  9k6/3000 AX.25 G3RUH

The GNU Radio tests are gated behind MAVERIC_FULL_GR=1 (they spawn real
flowgraphs); that gate is the ONLY skip. Once enabled, every failure mode
— timeout, crash, no decode, wrong payload — fails the test. This module
replaces the former ops_test_support decode_golay_via_* helpers, which had
no callers, ran at a non-production 1.92 Msps, and converted failures into
skips.

The flowgraph subprocess ends with os._exit(0): GNU Radio teardown at
interpreter exit segfaults sporadically on macOS, and a segfault after the
results are printed must not fail the test.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import numpy as np
import yaml

TESTS_DIR = Path(__file__).resolve().parent
CODE_DIR = TESTS_DIR.parent
sys.path.insert(0, str(CODE_DIR))

from mav_gss_lib.platform.framing.asm_golay import build_asm_golay_frame  # noqa: E402
from mav_gss_lib.platform.framing.ax25 import AX25Config, build_ax25_gfsk_frame  # noqa: E402

GNURADIO = CODE_DIR / "gnuradio"
DECODERS = GNURADIO / "decoders"
FS = 200_000  # production decode rate: MAV_DUO samp_rate (1 Msps) / rx_decim (5)
GNURADIO_PYTHON = os.environ.get("MAVERIC_GNURADIO_PYTHON", sys.executable)

# gr-satellites needs enough of a run-in for AGC + clock recovery to settle
# before the preamble; zero-gaps between bursts keep decodes attributable.
GAP_S = 0.08
NOISE_RMS = 0.02          # -34 dB floor: keeps AGC/squelch behaviour realistic
NOISE_SEED = 20260711     # fixed — the whole record is deterministic


def _gfsk_iq(wire: bytes, baud: float, deviation_hz: float, *, bt: float = 0.5) -> np.ndarray:
    """Modulate wire bytes (MSB-first NRZ) as GFSK at FS, matching
    digital.gfsk_mod(sensitivity=pi*h/sps, bt=0.5) with h = 2*dev/baud."""
    bits = np.unpackbits(np.frombuffer(wire, dtype=np.uint8))
    nrz = bits.astype(np.float64) * 2.0 - 1.0
    n = int(round(len(bits) * FS / baud))
    symbol_idx = np.minimum((np.arange(n) * baud / FS).astype(np.int64), len(bits) - 1)
    wave = nrz[symbol_idx]
    # Gaussian pulse, BT relative to the symbol period, 4-symbol span
    sigma = np.sqrt(np.log(2.0)) / (2.0 * np.pi * bt * baud)
    span = int(round(4 * FS / baud)) | 1
    t = (np.arange(span) - span // 2) / FS
    g = np.exp(-0.5 * (t / sigma) ** 2)
    g /= g.sum()
    shaped = np.convolve(wave, g, mode="same")
    phase = 2.0 * np.pi * deviation_hz * np.cumsum(shaped) / FS
    return np.exp(1j * phase)


def _compose_record(bursts: list[np.ndarray]) -> np.ndarray:
    gap = np.zeros(int(GAP_S * FS), dtype=np.complex128)
    parts: list[np.ndarray] = [gap]
    for burst in bursts:
        parts.extend((burst, gap))
    record = np.concatenate(parts)
    rng = np.random.default_rng(NOISE_SEED)
    noise = (rng.standard_normal(record.size) + 1j * rng.standard_normal(record.size))
    record = record + NOISE_RMS / np.sqrt(2.0) * noise
    return record.astype(np.complex64)


_DECODE_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    from gnuradio import gr, blocks
    import pmt
    from satellites.core.gr_satellites_flowgraph import gr_satellites_flowgraph

    db, iq_path, options = sys.argv[1], sys.argv[2], sys.argv[3]

    fg = gr_satellites_flowgraph(file=db, samp_rate=200000, iq=True,
                                 grc_block=True, options=options)
    src = blocks.file_source(gr.sizeof_gr_complex, iq_path, False)
    dbg = blocks.message_debug()
    tb = gr.top_block()
    tb.connect(src, fg)
    tb.msg_connect((fg, 'out'), (dbg, 'store'))
    # Run to EOF, never to a message count: multi-hypothesis databases decode
    # one burst on several branches, and counting duplicates toward an
    # expected total truncates the read mid-record (caught as a spurious
    # high-SNR FER floor on the dual-branch MAVERIC database).
    tb.start()
    tb.wait()
    time.sleep(1.0)   # let the last deframer messages drain into the store
    pdus = []
    for i in range(dbg.num_messages()):
        pdus.append(bytes(pmt.u8vector_elements(pmt.cdr(dbg.get_message(i)))).hex())
    print("PDUS " + " ".join(pdus), flush=True)
    os._exit(0)
    """
)


def _run_decoder(db: Path, options: str, record: np.ndarray,
                 wait_s: float = 120.0) -> list[bytes]:
    """Run one record through the production-instantiated decoder to EOF;
    return every PDU it emitted (duplicates included).

    Raises AssertionError on any failure mode — never skips."""
    with tempfile.TemporaryDirectory() as tmp:
        iq_path = Path(tmp) / "record.cf32"
        record.tofile(iq_path)
        proc = subprocess.run(
            [GNURADIO_PYTHON, "-u", "-c", _DECODE_SCRIPT,
             str(db), str(iq_path), options],
            capture_output=True, text=True, timeout=wait_s + 60,
        )
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise AssertionError(f"decoder subprocess failed rc={proc.returncode}:\n{tail}")
    for line in proc.stdout.splitlines():
        if line.startswith("PDUS"):
            return [bytes.fromhex(h) for h in line.split()[1:]]
    raise AssertionError(f"decoder subprocess printed no PDUS line:\n{proc.stdout[-800:]}")


def _requires_full_gr() -> None:
    if os.environ.get("MAVERIC_FULL_GR") != "1":
        raise unittest.SkipTest("set MAVERIC_FULL_GR=1 to run the GNU Radio loopback tests")


def _decoder_path(db_name: str) -> Path:
    path = Path(db_name)
    return GNURADIO / path if len(path.parts) > 1 else DECODERS / path


def _production_options(db_name: str) -> str:
    """The options string a RadioService-launched MAV_DUO would pair with
    this database — derived by the flowgraph's own `_decoder_options()`, so
    the loopbacks exercise the real production pairing instead of a literal
    that could silently diverge from it. Imported lazily: MAV_DUO pulls Qt
    and UHD at module import, which only the gated tests may pay for."""
    if str(GNURADIO) not in sys.path:
        sys.path.insert(0, str(GNURADIO))
    import MAV_DUO
    return MAV_DUO._decoder_options(str(_decoder_path(db_name)))


class DecodeLoopbackTests(unittest.TestCase):
    """Synthetic bursts through the exact production decoder instantiation."""

    maxDiff = None

    def setUp(self) -> None:
        _requires_full_gr()

    def _assert_decodes(self, db: str, options: str, cases: list[tuple[bytes, np.ndarray]]) -> None:
        record = _compose_record([burst for _, burst in cases])
        pdus = _run_decoder(_decoder_path(db), options, record)
        decoded = {p for p in pdus}
        for payload, _ in cases:
            self.assertIn(payload, decoded,
                          f"{db}: payload {payload[:16].hex()}… not decoded "
                          f"(got {[p[:8].hex() for p in pdus]})")
        self.assertEqual(decoded, {payload for payload, _ in cases},
                         f"{db}: unexpected extra payloads decoded")

    def test_maveric_databases_decode_flight_mode(self):
        # Options derived by the production selector — this functionally
        # proves _decoder_options() still pairs threshold 6 with an AX100
        # database AND that the pairing parses and decodes.
        payload = b"\xA0" + b"MAVERIC-LOOP-9600-3200".ljust(40, b"\x5A")
        burst = _gfsk_iq(build_asm_golay_frame(payload), 9600, 3200)
        for database in (
            "MAVERIC_DECODER.yml",
            "public/MAVERIC_beacon_decoder/MAVERIC_BEACON.yml",
        ):
            with self.subTest(database=database):
                options = _production_options(database)
                self.assertEqual(options, "--syncword_threshold 6")
                self._assert_decodes(database, options, [(payload, burst)])

    def test_roads_database_decodes_measured_h05_branches(self):
        options = _production_options("ROADS_DECODER.yml")
        self.assertEqual(options, "--syncword_threshold 6")
        cases = []
        for i, (baud, dev) in enumerate([(4800, 1200), (9600, 2400)]):
            payload = bytes([0xB0 + i]) + f"ROADS-LOOP-{baud}-{dev}".encode().ljust(40, b"\x3C")
            cases.append((payload, _gfsk_iq(build_asm_golay_frame(payload), baud, dev)))
        self._assert_decodes("ROADS_DECODER.yml", options, cases)

    def test_other_ax100_databases_decode_exact_mission_profiles(self):
        profiles = {
            "SUOMI100_DECODER.yml": [(9600, 2400)],
            "LUOJIA1_DECODER.yml": [(4800, 1600)],
            "CATSAT_DECODER.yml": [(2400, 750)],
            "AISTECHSAT2_DECODER.yml": [(4800, 1600)],
            "INNOCUBE_DECODER.yml": [(9600, 3200)],
            "SNIPE_DECODER.yml": [(4800, 1600), (4800, 1200)],
            "NUSHSAT1_DECODER.yml": [(1200, 575), (2400, 600), (2400, 800)],
        }
        for db, waveforms in profiles.items():
            with self.subTest(database=db):
                options = _production_options(db)
                self.assertEqual(options, "--syncword_threshold 6")
                cases = []
                for i, (baud, dev) in enumerate(waveforms):
                    payload = bytes([0xC0 + i]) + (
                        f"{db}-{baud}-{dev}".encode().ljust(48, b"\x69")
                    )
                    cases.append((
                        payload,
                        _gfsk_iq(build_asm_golay_frame(payload), baud, dev),
                    ))
                self._assert_decodes(db, options, cases)

    def test_sharjahsat_database_decodes_official_g3ruh(self):
        # The selector must return NO options for a pure-AX.25 database —
        # the companion test below proves the flag would abort argparse.
        options = _production_options("SHARJAHSAT_DECODER.yml")
        self.assertEqual(options, "")
        cfg = AX25Config()
        cfg.dest_call, cfg.src_call = "GS1UOS", "A62UOS"
        packet = cfg.wrap(b"SHARJAHSAT-LOOPBACK" + bytes(range(48)))
        burst = _gfsk_iq(build_ax25_gfsk_frame(packet), 9600, 3000)
        self._assert_decodes("SHARJAHSAT_DECODER.yml", options, [(packet, burst)])

    def test_syncword_threshold_rejected_by_pure_ax25_database(self):
        # Pins the failure mode _decoder_options() exists to prevent: the
        # flag only parses when an AX100/FX.25 deframer defines it, so a
        # pure-AX.25 database must abort argparse at construction.
        script = textwrap.dedent(
            """
            import sys
            from satellites.core.gr_satellites_flowgraph import gr_satellites_flowgraph
            gr_satellites_flowgraph(file=sys.argv[1], samp_rate=200000, iq=True,
                                    grc_block=True, options="--syncword_threshold 6")
            print("CONSTRUCTED", flush=True)
            """
        )
        proc = subprocess.run(
            [GNURADIO_PYTHON, "-u", "-c", script,
             str(DECODERS / "SHARJAHSAT_DECODER.yml")],
            capture_output=True, text=True, timeout=120,
        )
        self.assertNotEqual(proc.returncode, 0,
                            "pure-AX.25 database accepted --syncword_threshold 6; "
                            "_decoder_options() gating is no longer needed — revisit it")
        self.assertIn("unrecognized arguments", proc.stderr)


class FlowgraphParamGuards(unittest.TestCase):
    """Always-on pins for the hardware-verified TX waveform (no GNU Radio)."""

    def test_tx_modindex_pinned_to_ax100_auto_default(self):
        # 1/1.5 (h = 2/3) is the AX100 auto-modindex value for 1300-60000
        # baud and the value bench-verified against the flight unit. A
        # stray edit here silently detunes the uplink.
        py = (GNURADIO / "MAV_DUO.py").read_text(encoding="utf-8")
        self.assertRegex(py, re.compile(r"^\s*self\.modindex = modindex = 1/1\.5$", re.M))
        grc = yaml.safe_load((GNURADIO / "MAV_DUO.grc").read_text(encoding="utf-8"))
        (block,) = [b for b in grc["blocks"] if b["name"] == "modindex"]
        self.assertEqual(block["parameters"]["value"], "1/1.5")

    def test_decoder_selector_is_fail_closed_in_py_and_grc(self):
        py = (GNURADIO / "MAV_DUO.py").read_text(encoding="utf-8")
        self.assertIn("resolve_mav_duo_decoder", py)
        self.assertNotIn("MAVERIC_DECODER.yml (default)", py)

        grc = yaml.safe_load((GNURADIO / "MAV_DUO.grc").read_text(encoding="utf-8"))
        blocks = {block["name"]: block for block in grc["blocks"]}
        selector = blocks["decoder_yml"]["parameters"]["value"]
        self.assertIn("GSS_DECODER_YML", selector)
        self.assertIn("GSS_MISSION", selector)
        self.assertIn("'decoders'", selector)
        self.assertIn("'_DECODER.yml'", selector)
        self.assertNotIn("isfile", selector)

    def test_stream_instrumentation_mirrored_in_py_and_grc(self):
        # Pre-FIR health probe + raw 1 Msps recorder + env RX gain must stay
        # present in BOTH hand-edited files (the .grc is never regenerated).
        py = (GNURADIO / "MAV_DUO.py").read_text(encoding="utf-8")
        self.assertIn("class _StreamHealthMonitor", py)
        self.assertIn("STREAM_HEALTH", py)
        self.assertIn(
            "self.connect((self.uhd_usrp_source_0, 0), (self.stream_health, 0))", py)
        self.assertIn(
            "self.connect((self.uhd_usrp_source_0, 0), (self.iq_raw_recorder, 0))", py)
        self.assertIn("GSS_IQ_RAW_RECORD", py)
        self.assertIn("maveric:rx_gain_db", py)
        self.assertRegex(py, re.compile(
            r"^\s*self\.rx_gain = rx_gain = "
            r"float\(__import__\('os'\)\.environ\.get\('GSS_RX_GAIN', 40\)\)$", re.M))

        grc = yaml.safe_load((GNURADIO / "MAV_DUO.grc").read_text(encoding="utf-8"))
        blocks = {b["name"]: b for b in grc["blocks"]}
        for name in ("epy_block_iq", "epy_block_iq_raw", "epy_block_stream_health"):
            source = blocks[name]["parameters"]["_source_code"]
            compile(source, name, "exec")  # every epy mirror stays valid python
        self.assertIn("GSS_IQ_RAW_RECORD",
                      blocks["epy_block_iq_raw"]["parameters"]["_source_code"])
        self.assertIn("STREAM_HEALTH",
                      blocks["epy_block_stream_health"]["parameters"]["_source_code"])
        self.assertIn("maveric:rx_gain_db",
                      blocks["epy_block_iq"]["parameters"]["_source_code"])
        # First-sample capture timestamps (construction time kept as
        # maveric:constructed_utc) must stay in every recorder copy.
        self.assertIn("maveric:constructed_utc", py)
        for name in ("epy_block_iq", "epy_block_iq_raw"):
            self.assertIn("maveric:constructed_utc",
                          blocks[name]["parameters"]["_source_code"])
        self.assertIn("GSS_RX_GAIN", blocks["rx_gain"]["parameters"]["value"])
        conns = {tuple(c) for c in grc["connections"]}
        self.assertIn(("uhd_usrp_source_0", "0", "epy_block_iq_raw", "0"), conns)
        self.assertIn(("uhd_usrp_source_0", "0", "epy_block_stream_health", "0"), conns)


if __name__ == "__main__":
    unittest.main()
