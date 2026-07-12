#!/usr/bin/env python3
"""MAV_ASTROCAST — RX-only GNU Radio decoder for Astrocast 0.1 (NORAD 43798).

Qt GUI top block supervised by the GSS RadioService (set
`platform.radio.script: gnuradio/MAV_ASTROCAST.py`). Its live B210 path
mirrors MAV_DUO's RX acquisition conventions: A:A/RX2, 1 Msps, parked LO,
gain 40, explicit idle/RX relay GPIO, 5x decimation, and a broad 200 ksps
spectrum/waterfall before any beacon filtering. The decimated stream also
feeds MAV_DUO's waterfall autosave recorder (mission-tagged PNG per run
under GSS_WATERFALL_DIR). Matched decoder branches every 2 kHz from -12 to
+12 kHz cover +/-12 kHz of residual carrier error before FM demodulation
and gr-satellites' native Astrocast FX.25 decoder, and a dual-tone
matched-filter fine bank (500 Hz centres across +/-3 kHz) bypasses the
discriminator entirely for extra threshold where doppler-engaged residuals
actually land. Both NRZ-I and legacy NRZ failsafe deframers run in
parallel on every branch. Deframed PDUs publish on
the GSS RX frame bus.

Input modes:
  default          USRP B210 (same subdev/antenna/gain conventions as
                   MAV_DUO), parked-LO tuning from GSS_RX_FREQ_HZ /
                   GSS_RX_LO_OFFSET_HZ (RadioService injects both), and
                   Doppler tune messages consumed on tcp://127.0.0.1:52003
                   into the UHD command port.
  --wavfile PATH   Offline replay of a 48 kHz mono FM-demodulated wav
                   recording (e.g. satellite-recordings/astrocast.wav).
                   Single central decoder only — bypasses the banks.
  --iqfile PATH    Offline replay of a 200 ksps cf32 IQ recording (an
                   _IqRecorder capture, e.g. <log_dir>/iq/
                   iq_astrocast_*.sigmf-data) through the exact live
                   decode chain: all 13 discriminator branches plus the
                   matched-filter fine bank.

Pass --headless to skip the Qt GUI entirely (scripted replay / SSH use).
"""

import argparse
from math import pi
import json
import os
import signal
import struct
import sys
import threading
import time
import traceback

import numpy as np

from gnuradio import analog, blocks, digital, gr, zeromq
from gnuradio import filter as gr_filter
from gnuradio.fft import window
from gnuradio.filter import firdes

import satellites
import satellites.components.datasinks
import satellites.core
from satellites.components.deframers import astrocast_fx25_deframer
from satellites.hier.rms_agc_f import rms_agc_f


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DECODER_YML = os.path.join(SCRIPT_DIR, "ASTROCAST_DECODER.yml")

DEFAULT_RX_FREQ_HZ = 437.150e6
DEFAULT_RX_LO_OFFSET_HZ = 250e3
SAMP_RATE = 1_000_000
RX_DECIM = 5
RX_GAIN = 40
ACQUISITION_RATE = SAMP_RATE // RX_DECIM
# These firdes parameters reproduce MAV_DUO's RX FIR, which anti-aliases the
# /5 decimation (passband to ~72.5 kHz, stopband before the 100 kHz output
# Nyquist). Keep the same gain, response, decimation, decoder feed, and
# display tap so an Astrocast pass sees the same RF acquisition topology as
# MAVERIC.
RX_FRONTEND_GAIN = 2.0
RX_FRONTEND_CUTOFF_HZ = 80_000.0
RX_FRONTEND_TRANSITION_HZ = 15_000.0
# Matched-filter search bank: a branch every 2 kHz across +/-12 kHz. Any
# residual lands within 1 kHz of a centre, keeping both 1k2 tones inside the
# flat passband (1.0k residual + 1.2k deviation = 2.2k < cutoff - trans/2)
# while the discriminator sees only ~6.5 kHz of pre-detection noise — ~3 dB
# less than the previous 3-branch +/-5.5 kHz design at the same coverage.
BEACON_BRANCH_CENTERS_HZ = (
    -12_000.0, -10_000.0, -8_000.0, -6_000.0, -4_000.0, -2_000.0, 0.0,
    2_000.0, 4_000.0, 6_000.0, 8_000.0, 10_000.0, 12_000.0,
)
BEACON_CHANNEL_CUTOFF_HZ = 2_800.0
BEACON_CHANNEL_TRANSITION_HZ = 900.0
BEACON_CHANNEL_DECIM = 10
BEACON_DECODER_RATE = ACQUISITION_RATE // BEACON_CHANNEL_DECIM
BEACON_DEVIATION_HZ = 1_200.0
BEACON_BAUD = 1_200.0
# Matched-filter fine bank: deviation == baud (h = 2) makes the two beacon
# tones orthogonal, so a per-symbol dual-tone integrate-and-dump detector
# has no FM click threshold and out-decodes the discriminator branches at
# low CNR (proven on the 2026-07-11 capture). Its correlators lose ~0.6 dB
# at 250 Hz of mistune, hence dense 500 Hz centres across the +/-3 kHz
# where doppler-engaged residuals actually land (measured -1.4..+2.6 kHz);
# the discriminator bank keeps owning coverage out to +/-12 kHz.
MATCHED_FILTER_BRANCH_CENTERS_HZ = tuple(
    float(hz) for hz in range(-3_000, 3_001, 500))
MATCHED_FILTER_DECIM = 10
MATCHED_FILTER_CLK_BW = 0.06
MATCHED_FILTER_CLK_LIMIT = 0.008
MATCHED_FILTER_SYNC_THRESHOLD = 8
WAV_SAMP_RATE = 48_000
DECODER_OPTIONS = "--clk_limit 0.008"

FRAME_ZMQ_ADDR = "tcp://127.0.0.1:52001"
DOPPLER_ZMQ_ADDR = "tcp://127.0.0.1:52003"

# Same B210 FP0 H-bridge mapping and safe RX value as MAV_DUO. Astrocast is
# RX-only, so these pins never leave this state while the flowgraph is alive.
RX_GPIO_LIVE_PIN = 1 << 0
RX_GPIO_ENABLE_PIN = 1 << 1
RX_GPIO_INVERSE_PIN = 1 << 2
RX_GPIO_ENABLE_3_PIN = 1 << 3
RX_GPIO_MASK = (
    RX_GPIO_LIVE_PIN
    | RX_GPIO_ENABLE_PIN
    | RX_GPIO_INVERSE_PIN
    | RX_GPIO_ENABLE_3_PIN
)
RX_GPIO_IDLE_OUT = (
    RX_GPIO_ENABLE_PIN | RX_GPIO_INVERSE_PIN | RX_GPIO_ENABLE_3_PIN
)


def _rx_frontend_taps():
    """Reproduce MAV_DUO's decimating RX FIR from its design parameters."""
    return firdes.low_pass(
        RX_FRONTEND_GAIN,
        SAMP_RATE,
        RX_FRONTEND_CUTOFF_HZ,
        RX_FRONTEND_TRANSITION_HZ,
    )


def _beacon_channel_taps():
    """Pass one narrow 1k2 FSK acquisition branch."""
    return firdes.low_pass(
        1.0,
        ACQUISITION_RATE,
        BEACON_CHANNEL_CUTOFF_HZ,
        BEACON_CHANNEL_TRANSITION_HZ,
    )


def _force_rx_relay(usrp):
    """Put the external H-bridge/coax switch in MAV_DUO's safe RX state."""
    usrp.set_gpio_attr("FP0", "CTRL", 0x0, RX_GPIO_MASK)
    # Preload OUT before changing direction so startup cannot pulse TX.
    usrp.set_gpio_attr("FP0", "OUT", RX_GPIO_IDLE_OUT, RX_GPIO_MASK)
    usrp.set_gpio_attr("FP0", "DDR", RX_GPIO_MASK, RX_GPIO_MASK)


class _WaterfallLogger(gr.sync_block):
    """Post-pass waterfall recorder for the decimated RX stream.

    Appends timestamped 1024-bin dB rows to waterfall_<mission>_<start>.dat
    while the flowgraph runs; stop() renders a SatNOGS-style PNG via
    waterfall_render and deletes the .dat on success. Hard crashes leave the
    .dat behind, so __init__ sweeps the output dir for leftovers and renders
    them in a background thread. Every failure path prints and disables the
    block — waterfall capture must never take down the radio. Same recorder
    as MAV_DUO's; only the mission fallback differs.
    """

    FFT_SIZE = 1024
    FFTS_PER_ROW = 20  # ~9.8 rows/s at 200 ksps

    def __init__(self):
        gr.sync_block.__init__(self, name="waterfall_logger",
                               in_sig=[np.complex64], out_sig=None)
        self._file = None
        self._dat_path = ""
        self._render_mod = None
        raw_center = os.environ.get("GSS_RX_FREQ_HZ", "")
        self._center_hz = float(raw_center) if raw_center else None
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            if script_dir not in sys.path:
                sys.path.insert(0, script_dir)
            import waterfall_render
            self._render_mod = waterfall_render
            out_dir = os.environ.get("GSS_WATERFALL_DIR") or os.path.join(script_dir, "waterfalls")
            os.makedirs(out_dir, exist_ok=True)
            orphans = [os.path.join(out_dir, name) for name in sorted(os.listdir(out_dir))
                       if name.startswith("waterfall_") and name.endswith(".dat")]
            if orphans:
                threading.Thread(target=self._render_orphans, args=(orphans,),
                                 daemon=True, name="waterfall-orphans").start()
            mission = os.environ.get("GSS_MISSION") or "astrocast"
            stem = "waterfall_%s_%s" % (mission, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
            self._dat_path = os.path.join(out_dir, stem + ".dat")
            self._file = open(self._dat_path, "ab")
            self._win = np.asarray(window.blackmanharris(self.FFT_SIZE), dtype=np.float32)
            self._buf = np.empty(0, dtype=np.complex64)
            self._acc = np.zeros(self.FFT_SIZE, dtype=np.float64)
            self._nacc = 0
        except Exception:
            traceback.print_exc()
            print("waterfall_logger: init failed; waterfall capture disabled", flush=True)
            self._close_quietly()

    def _close_quietly(self):
        try:
            if self._file is not None:
                self._file.close()
        except Exception:
            pass
        self._file = None

    def _render_orphans(self, paths):
        for path in paths:
            try:
                png = self._render_mod.render(path, delete_dat=True,
                                              center_freq_hz=self._center_hz)
                if png is None:
                    print(f"waterfall_logger: removed empty leftover {path}", flush=True)
                else:
                    print(f"waterfall_logger: rendered leftover {png}", flush=True)
            except Exception:
                traceback.print_exc()
                print(f"waterfall_logger: leftover render failed; kept {path}", flush=True)

    def work(self, input_items, output_items):
        n_in = len(input_items[0])
        if self._file is None:
            return n_in
        try:
            self._buf = np.concatenate((self._buf, input_items[0]))
            while self._buf.size >= self.FFT_SIZE:
                chunk = self._buf[:self.FFT_SIZE]
                self._buf = self._buf[self.FFT_SIZE:]
                spec = np.fft.fft(chunk * self._win)
                self._acc += spec.real ** 2 + spec.imag ** 2
                self._nacc += 1
                if self._nacc >= self.FFTS_PER_ROW:
                    mean_power = self._acc / (self._nacc * self.FFT_SIZE ** 2)
                    row = 10.0 * np.log10(mean_power + 1e-20)
                    row = np.fft.fftshift(row).astype("<f4")
                    self._file.write(struct.pack("<d", time.time()) + row.tobytes())
                    self._file.flush()
                    self._acc[:] = 0.0
                    self._nacc = 0
        except Exception:
            traceback.print_exc()
            print("waterfall_logger: capture failed; waterfall disabled for this run", flush=True)
            self._close_quietly()
        return n_in

    def stop(self):
        if self._file is None:
            return True
        self._close_quietly()
        try:
            png = self._render_mod.render(self._dat_path, delete_dat=True,
                                          center_freq_hz=self._center_hz)
            if png is None:
                print("waterfall_logger: empty capture; nothing to render", flush=True)
            else:
                print(f"waterfall_logger: saved {png}", flush=True)
        except Exception:
            traceback.print_exc()
            print(f"waterfall_logger: render failed; kept {self._dat_path}", flush=True)
        return True


class _IqRecorder(gr.sync_block):
    """Env-gated raw IQ recorder for the decimated RX stream.

    Enabled when RadioService injects GSS_IQ_RECORD=1 (operator toggle
    `platform.radio.iq_record`). Appends the complex64 stream to
    iq_<mission>_<start>.sigmf-data under GSS_IQ_DIR and writes the matching
    SigMF metadata up front, so even a hard crash leaves a replayable pair
    (any cf32 prefix is valid). MAX_BYTES caps a forgotten toggle before it
    can fill the disk. Every failure path prints and disables the block —
    IQ capture must never take down the radio. Same recorder as MAV_DUO's;
    only the mission fallback differs.
    """

    MAX_BYTES = 8_000_000_000  # ~83 min of 200 ksps complex64

    def __init__(self, samp_rate=200_000.0):
        gr.sync_block.__init__(self, name="iq_recorder",
                               in_sig=[np.complex64], out_sig=None)
        self._file = None
        self._data_path = ""
        self._meta_path = ""
        self._written = 0
        gate = os.environ.get("GSS_IQ_RECORD", "").strip().lower()
        if gate in ("", "0", "false", "no", "off"):
            return
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            out_dir = os.environ.get("GSS_IQ_DIR") or os.path.join(script_dir, "iq")
            os.makedirs(out_dir, exist_ok=True)
            mission = os.environ.get("GSS_MISSION") or "astrocast"
            start = time.gmtime()
            stem = "iq_%s_%s" % (mission, time.strftime("%Y%m%dT%H%M%SZ", start))
            self._data_path = os.path.join(out_dir, stem + ".sigmf-data")
            self._meta_path = os.path.join(out_dir, stem + ".sigmf-meta")
            raw_center = os.environ.get("GSS_RX_FREQ_HZ", "")
            capture = {"core:sample_start": 0,
                       "core:datetime": time.strftime("%Y-%m-%dT%H:%M:%SZ", start)}
            if raw_center:
                capture["core:frequency"] = float(raw_center)
            meta = {
                "global": {
                    "core:datatype": "cf32_le",
                    "core:sample_rate": float(samp_rate),
                    "core:version": "1.0.0",
                    "core:recorder": "MAVERIC GSS",
                },
                "captures": [capture],
                "annotations": [],
            }
            with open(self._meta_path, "w") as meta_file:
                json.dump(meta, meta_file, indent=2)
            self._file = open(self._data_path, "ab")
            print(f"iq_recorder: recording {self._data_path} "
                  f"(cap {self.MAX_BYTES / 1e9:.0f} GB)", flush=True)
        except Exception:
            traceback.print_exc()
            print("iq_recorder: init failed; IQ capture disabled", flush=True)
            self._close_quietly()

    def _close_quietly(self):
        try:
            if self._file is not None:
                self._file.close()
        except Exception:
            pass
        self._file = None

    def work(self, input_items, output_items):
        n_in = len(input_items[0])
        if self._file is None:
            return n_in
        try:
            self._file.write(input_items[0].tobytes())
            self._written += n_in * 8
            if self._written >= self.MAX_BYTES:
                self._close_quietly()
                print(f"iq_recorder: size cap reached; saved {self._data_path} "
                      f"({self._written} bytes)", flush=True)
        except Exception:
            traceback.print_exc()
            print("iq_recorder: write failed; IQ capture disabled for this run", flush=True)
            self._close_quietly()
        return n_in

    def stop(self):
        if self._file is None:
            return True
        self._close_quietly()
        try:
            if self._written == 0:
                os.remove(self._data_path)
                os.remove(self._meta_path)
                print("iq_recorder: empty capture; files removed", flush=True)
            else:
                print(f"iq_recorder: saved {self._data_path} ({self._written} bytes)", flush=True)
        except Exception:
            traceback.print_exc()
        return True


def _attach_matched_filter_bank(tb, source):
    """Dual-tone noncoherent correlator decode bank on `source`.

    One chain per MATCHED_FILTER_BRANCH_CENTERS_HZ entry: a pair of
    one-symbol integrate-and-dump correlators parked on the two beacon
    tones, magnitude-difference soft symbols, Gardner recovery, and the
    FX.25 deframers directly — no discriminator. The rectangular tone
    templates are matched to a constant-frequency symbol, not to the
    exact BT=0.5 Gaussian pulse, so this is near-matched for h=2 rather
    than an exact CPM matched filter. NRZ-I decoding is
    polarity-invariant; the legacy-NRZ deframer gets both polarities.
    Returns the deframers whose 'out' ports carry decoded PDUs (the
    caller fans them into the PDU deduplicator, which collapses the
    multi-branch decodes of a strong burst).
    """
    fs = float(ACQUISITION_RATE)
    sps = (fs / MATCHED_FILTER_DECIM) / BEACON_BAUD
    ntaps = int(round(fs / BEACON_BAUD))
    taps = [1.0 / ntaps] * ntaps
    tb.matched_filter_blocks = []
    tb.matched_filter_constellations = []
    deframers = []
    for center_hz in MATCHED_FILTER_BRANCH_CENTERS_HZ:
        tone_high = gr_filter.freq_xlating_fir_filter_ccf(
            MATCHED_FILTER_DECIM, taps, center_hz + BEACON_DEVIATION_HZ, fs)
        tone_low = gr_filter.freq_xlating_fir_filter_ccf(
            MATCHED_FILTER_DECIM, taps, center_hz - BEACON_DEVIATION_HZ, fs)
        mag_high = blocks.complex_to_mag(1)
        mag_low = blocks.complex_to_mag(1)
        soft_symbols = blocks.sub_ff(1)
        agc = rms_agc_f(2e-2 / sps, 1)
        # symbol_sync_ff keeps only a raw pointer to the constellation;
        # the python object must stay referenced or it is GC'd mid-run.
        constellation = digital.constellation_bpsk()
        symbol_sync = digital.symbol_sync_ff(
            digital.TED_GARDNER, sps, MATCHED_FILTER_CLK_BW, 1.0, 1.47,
            MATCHED_FILTER_CLK_LIMIT * sps, 1, constellation.base(),
            digital.IR_PFB_NO_MF)
        invert = blocks.multiply_const_ff(-1.0)
        deframer_nrzi = astrocast_fx25_deframer(
            syncword_threshold=MATCHED_FILTER_SYNC_THRESHOLD,
            nrzi=True, options="")
        deframer_nrz = astrocast_fx25_deframer(
            syncword_threshold=MATCHED_FILTER_SYNC_THRESHOLD,
            nrzi=False, options="")
        deframer_nrz_inverted = astrocast_fx25_deframer(
            syncword_threshold=MATCHED_FILTER_SYNC_THRESHOLD,
            nrzi=False, options="")
        tb.connect(source, tone_high, mag_high)
        tb.connect(source, tone_low, mag_low)
        tb.connect(mag_high, (soft_symbols, 0))
        tb.connect(mag_low, (soft_symbols, 1))
        tb.connect(soft_symbols, agc, symbol_sync)
        tb.connect(symbol_sync, deframer_nrzi)
        tb.connect(symbol_sync, deframer_nrz)
        tb.connect(symbol_sync, invert, deframer_nrz_inverted)
        tb.matched_filter_constellations.append(constellation)
        tb.matched_filter_blocks.extend([
            tone_high, tone_low, mag_high, mag_low, soft_symbols, agc,
            symbol_sync, invert])
        deframers.extend([deframer_nrzi, deframer_nrz, deframer_nrz_inverted])
    return deframers


def _attach_decode_banks(tb, source):
    """Construct the full production decode chain — the 13 discriminator
    branches plus the matched-filter fine bank — fed from `source`, a
    200 ksps complex stream (rx_lpf live, or the --iqfile replay)."""
    tb.beacon_channelizers = []
    tb.beacon_demodulators = []
    tb.satellites_satellite_decoders = []
    channel_taps = _beacon_channel_taps()
    demod_gain = BEACON_DECODER_RATE / (
        2.0 * pi * BEACON_DEVIATION_HZ)
    for branch_center_hz in BEACON_BRANCH_CENTERS_HZ:
        channelizer = gr_filter.freq_xlating_fir_filter_ccf(
            BEACON_CHANNEL_DECIM,
            channel_taps,
            branch_center_hz,
            ACQUISITION_RATE,
        )
        demodulator = analog.quadrature_demod_cf(demod_gain)
        decoder = satellites.core.gr_satellites_flowgraph(
            file=DECODER_YML, samp_rate=BEACON_DECODER_RATE, iq=False,
            grc_block=True, options=DECODER_OPTIONS)
        tb.beacon_channelizers.append(channelizer)
        tb.beacon_demodulators.append(demodulator)
        tb.satellites_satellite_decoders.append(decoder)

    central_branch = BEACON_BRANCH_CENTERS_HZ.index(0.0)
    tb.beacon_channelizer = tb.beacon_channelizers[central_branch]
    tb.beacon_demodulator = tb.beacon_demodulators[central_branch]
    tb.satellites_satellite_decoder_0 = (
        tb.satellites_satellite_decoders[central_branch])
    branch_labels = ", ".join(
        f"{center_hz / 1_000:+g} kHz"
        for center_hz in BEACON_BRANCH_CENTERS_HZ)
    print(f"MAV_ASTROCAST 1k2 decoder branches: {branch_labels}",
          flush=True)
    for channelizer, demodulator, decoder in zip(
            tb.beacon_channelizers,
            tb.beacon_demodulators,
            tb.satellites_satellite_decoders):
        tb.connect(
            (source, 0),
            (channelizer, 0),
            (demodulator, 0),
            (decoder, 0),
        )
    tb.matched_filter_deframers = _attach_matched_filter_bank(tb, source)
    mf_centers = MATCHED_FILTER_BRANCH_CENTERS_HZ
    print(
        f"MAV_ASTROCAST matched-filter bank: {len(mf_centers)} branches, "
        f"{mf_centers[0] / 1_000:+g} to {mf_centers[-1] / 1_000:+g} kHz "
        f"every {(mf_centers[1] - mf_centers[0]) / 1_000:g} kHz",
        flush=True)


def _build_core(tb, wavfile, iqfile, zmq_addr, doppler_addr):
    """Construct the shared DSP chain (sources, decoder, ZMQ/hexdump sinks)
    on `tb`. GUI-agnostic: both the Qt and headless top blocks call this."""
    tb.zeromq_pub_msg_sink_0 = zeromq.pub_msg_sink(zmq_addr, 100, True)
    tb.satellites_hexdump_sink_0 = satellites.components.datasinks.hexdump_sink(options="")

    if wavfile:
        tb.blocks_wavfile_source_0 = blocks.wavfile_source(wavfile, False)
        # Pace finite recordings like a live radio stream. Without this,
        # symbol_sync receives scheduler-dependent burst sizes and the
        # marginal legacy-NRZ clock acquisition becomes nondeterministic.
        tb.blocks_wav_throttle = blocks.throttle(
            gr.sizeof_float, WAV_SAMP_RATE, True)
        tb.satellites_satellite_decoder_0 = satellites.core.gr_satellites_flowgraph(
            file=DECODER_YML, samp_rate=WAV_SAMP_RATE, iq=False,
            grc_block=True, options=DECODER_OPTIONS)
        tb.connect(
            (tb.blocks_wavfile_source_0, 0),
            (tb.blocks_wav_throttle, 0),
            (tb.satellites_satellite_decoder_0, 0))
    elif iqfile:
        print(f"MAV_ASTROCAST IQ replay: {iqfile} "
              f"({ACQUISITION_RATE} sps cf32 through the live decode banks)",
              flush=True)
        tb.blocks_iqfile_source = blocks.file_source(
            gr.sizeof_gr_complex, iqfile, False)
        # Same pacing rationale as the wav throttle: without it a finite
        # recording reaches symbol_sync in scheduler-dependent burst sizes
        # and marginal clock acquisition becomes nondeterministic.
        tb.blocks_iq_throttle = blocks.throttle(
            gr.sizeof_gr_complex, ACQUISITION_RATE, True)
        tb.connect((tb.blocks_iqfile_source, 0), (tb.blocks_iq_throttle, 0))
        _attach_decode_banks(tb, tb.blocks_iq_throttle)
    else:
        from gnuradio import uhd

        tb.rx_freq = float(os.environ.get("GSS_RX_FREQ_HZ", DEFAULT_RX_FREQ_HZ))
        tb.rx_lo_offset = float(os.environ.get("GSS_RX_LO_OFFSET_HZ", DEFAULT_RX_LO_OFFSET_HZ))
        # Log the tuning intent BEFORE touching the USRP so the Radio logs
        # show the target frequency even if the device open fails.
        print(f"MAV_ASTROCAST RX {tb.rx_freq/1e6:.6f} MHz "
              f"(LO parked {tb.rx_lo_offset/1e3:+.0f} kHz), "
              f"{ACQUISITION_RATE} sps acquisition channel", flush=True)
        tb.uhd_usrp_source_0 = uhd.usrp_source(
            ",".join(("", "")),
            uhd.stream_args(cpu_format="fc32", args='', channels=list(range(0, 1))),
        )
        tb.uhd_usrp_source_0.set_subdev_spec('A:A', 0)
        tb.uhd_usrp_source_0.set_samp_rate(SAMP_RATE)
        tb.uhd_usrp_source_0.set_center_freq(
            uhd.tune_request(tb.rx_freq, tb.rx_lo_offset), 0)
        tb.uhd_usrp_source_0.set_antenna("RX2", 0)
        tb.uhd_usrp_source_0.set_gain(RX_GAIN, 0)
        _force_rx_relay(tb.uhd_usrp_source_0)
        print("MAV_ASTROCAST relay GPIO forced to idle/RX", flush=True)

        tb.rx_lpf = gr_filter.fir_filter_ccf(
            RX_DECIM, _rx_frontend_taps())
        tb.zeromq_sub_msg_source_rxcmd = zeromq.sub_msg_source(
            doppler_addr, 100, False)

        tb.connect((tb.uhd_usrp_source_0, 0), (tb.rx_lpf, 0))
        _attach_decode_banks(tb, tb.rx_lpf)
        tb.waterfall_logger = _WaterfallLogger()
        tb.connect((tb.rx_lpf, 0), (tb.waterfall_logger, 0))
        tb.iq_recorder = _IqRecorder(samp_rate=float(ACQUISITION_RATE))
        tb.connect((tb.rx_lpf, 0), (tb.iq_recorder, 0))
        tb.msg_connect(
            (tb.zeromq_sub_msg_source_rxcmd, 'out'),
            (tb.uhd_usrp_source_0, 'command'))

    decoders = getattr(
        tb,
        "satellites_satellite_decoders",
        (tb.satellites_satellite_decoder_0,),
    )
    tb.beacon_pdu_deduplicator = _PduDeduplicator()
    for decoder in decoders:
        tb.msg_connect(
            (decoder, "out"),
            (tb.beacon_pdu_deduplicator, "in"))
    for deframer in getattr(tb, "matched_filter_deframers", ()):
        tb.msg_connect(
            (deframer, "out"),
            (tb.beacon_pdu_deduplicator, "in"))
    tb.msg_connect(
        (tb.beacon_pdu_deduplicator, "out"),
        (tb.zeromq_pub_msg_sink_0, "in"))
    tb.msg_connect(
        (tb.beacon_pdu_deduplicator, "out"),
        (tb.satellites_hexdump_sink_0, "in"))


class mav_astrocast_headless(gr.top_block):

    def __init__(self, wavfile=None, iqfile=None, zmq_addr=FRAME_ZMQ_ADDR,
                 doppler_addr=DOPPLER_ZMQ_ADDR):
        gr.top_block.__init__(self, "MAV_ASTROCAST", catch_exceptions=True)
        _build_core(self, wavfile, iqfile, zmq_addr, doppler_addr)


def _make_qt_class():
    """Import Qt lazily so --headless never touches PyQt5/qtgui."""
    from PyQt5 import Qt, QtCore
    from gnuradio import qtgui
    from gnuradio.fft import window
    import sip

    class mav_astrocast(gr.top_block, Qt.QWidget):

        def __init__(self, wavfile=None, iqfile=None, zmq_addr=FRAME_ZMQ_ADDR,
                     doppler_addr=DOPPLER_ZMQ_ADDR):
            gr.top_block.__init__(self, "MAV_ASTROCAST", catch_exceptions=True)
            Qt.QWidget.__init__(self)
            self.setWindowTitle("MAV ASTROCAST — Astrocast 0.1 RX")
            qtgui.util.check_set_qss()
            try:
                self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
            except BaseException as exc:
                print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
            self.top_scroll_layout = Qt.QVBoxLayout()
            self.setLayout(self.top_scroll_layout)
            self.top_scroll = Qt.QScrollArea()
            self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
            self.top_scroll_layout.addWidget(self.top_scroll)
            self.top_scroll.setWidgetResizable(True)
            self.top_widget = Qt.QWidget()
            self.top_scroll.setWidget(self.top_widget)
            self.top_layout = Qt.QVBoxLayout(self.top_widget)
            self.top_grid_layout = Qt.QGridLayout()
            self.top_layout.addLayout(self.top_grid_layout)

            self.settings = Qt.QSettings("gnuradio/flowgraphs", "MAV_ASTROCAST")
            try:
                geometry = self.settings.value("geometry")
                if geometry:
                    self.restoreGeometry(geometry)
            except BaseException as exc:
                print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)
            self.flowgraph_started = threading.Event()

            _build_core(self, wavfile, iqfile, zmq_addr, doppler_addr)
            self.wavfile = wavfile
            self.iqfile = iqfile

            if wavfile:
                spectrum_fc = 0
                spectrum_bw = WAV_SAMP_RATE
                spectrum_fft_size = 2048
                self.qtgui_freq_sink_x_0 = qtgui.freq_sink_f(
                    spectrum_fft_size, window.WIN_BLACKMAN_hARRIS,
                    spectrum_fc, spectrum_bw,
                    "", 1, None)
                spectrum_tap = self.blocks_wav_throttle
            else:
                # MAV_DUO presents this as a baseband view. The achieved-
                # frequency readout carries the absolute, Doppler-tuned center.
                spectrum_fc = 0
                spectrum_bw = ACQUISITION_RATE
                spectrum_fft_size = 1024
                self.qtgui_freq_sink_x_0 = qtgui.freq_sink_c(
                    spectrum_fft_size, window.WIN_BLACKMAN_hARRIS,
                    spectrum_fc, spectrum_bw,
                    "", 1, None)
                spectrum_tap = self.blocks_iq_throttle if iqfile else self.rx_lpf

            self.qtgui_freq_sink_x_0.set_update_time(0.05)
            self.qtgui_freq_sink_x_0.set_y_axis((-140), 10)
            self.qtgui_freq_sink_x_0.set_y_label('RX Spectrum', 'dB')
            self.qtgui_freq_sink_x_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
            self.qtgui_freq_sink_x_0.enable_autoscale(bool(wavfile))
            self.qtgui_freq_sink_x_0.enable_grid(bool(wavfile))
            self.qtgui_freq_sink_x_0.set_fft_average(0.2 if wavfile else 1.0)
            self.qtgui_freq_sink_x_0.enable_axis_labels(True)
            self.qtgui_freq_sink_x_0.enable_control_panel(False)
            self.qtgui_freq_sink_x_0.set_fft_window_normalized(False)
            self.qtgui_freq_sink_x_0.disable_legend()
            self.qtgui_freq_sink_x_0.set_line_label(0, "RX")
            self._qtgui_freq_sink_x_0_win = sip.wrapinstance(
                self.qtgui_freq_sink_x_0.qwidget(), Qt.QWidget)
            self.top_grid_layout.addWidget(self._qtgui_freq_sink_x_0_win, 1, 0, 1, 2)
            self.connect((spectrum_tap, 0), (self.qtgui_freq_sink_x_0, 0))

            if not wavfile:
                self.qtgui_waterfall_sink_x_0 = qtgui.waterfall_sink_c(
                    1024, window.WIN_BLACKMAN_hARRIS, spectrum_fc, spectrum_bw,
                    "", 1, None)
                self.qtgui_waterfall_sink_x_0.set_update_time(0.05)
                self.qtgui_waterfall_sink_x_0.enable_grid(False)
                self.qtgui_waterfall_sink_x_0.enable_axis_labels(True)
                self.qtgui_waterfall_sink_x_0.set_line_label(0, "RX")
                self.qtgui_waterfall_sink_x_0.set_intensity_range(-140, 10)
                self._qtgui_waterfall_sink_x_0_win = sip.wrapinstance(
                    self.qtgui_waterfall_sink_x_0.qwidget(), Qt.QWidget)
                self.top_grid_layout.addWidget(self._qtgui_waterfall_sink_x_0_win, 2, 0, 1, 2)
                self.connect((spectrum_tap, 0), (self.qtgui_waterfall_sink_x_0, 0))

            if not wavfile and not iqfile:
                self.rx_gain = RX_GAIN
                self._rx_gain_range = qtgui.Range(0, 76, 1, RX_GAIN, 200)
                self._rx_gain_win = qtgui.RangeWidget(
                    self._rx_gain_range, self.set_rx_gain, "RX Gain (dB)",
                    "counter_slider", float, QtCore.Qt.Horizontal)
                self.top_grid_layout.addWidget(self._rx_gain_win, 0, 0, 1, 1)

                self._rx_actual_freq_tool_bar = Qt.QToolBar(self)
                self._rx_actual_freq_tool_bar.addWidget(Qt.QLabel("USRP RX achieved"))
                self._rx_actual_freq_label = Qt.QLabel("--")
                self._rx_actual_freq_tool_bar.addWidget(self._rx_actual_freq_label)
                self.top_grid_layout.addWidget(self._rx_actual_freq_tool_bar, 0, 1, 1, 1)

                probe = threading.Thread(target=self._rx_actual_freq_probe, daemon=True)
                probe.start()

        def set_rx_gain(self, gain):
            self.rx_gain = gain
            self.uhd_usrp_source_0.set_gain(gain, 0)

        def _rx_actual_freq_probe(self):
            self.flowgraph_started.wait()
            while True:
                try:
                    val = self.uhd_usrp_source_0.get_center_freq(0)
                    Qt.QMetaObject.invokeMethod(
                        self._rx_actual_freq_label,
                        "setText",
                        Qt.Q_ARG("QString", f"{float(val)/1e6:.6f} MHz"),
                    )
                except (AttributeError, RuntimeError):
                    pass
                time.sleep(0.5)

        def closeEvent(self, event):
            self.settings = Qt.QSettings("gnuradio/flowgraphs", "MAV_ASTROCAST")
            self.settings.setValue("geometry", self.saveGeometry())
            self.stop()
            self.wait()
            event.accept()

    return mav_astrocast, Qt


class _PduDeduplicator(gr.basic_block):
    """Suppress the same frame emitted by overlapping search branches."""

    def __init__(self, holdoff_s=2.0):
        import pmt

        gr.basic_block.__init__(
            self,
            name="astrocast_pdu_deduplicator",
            in_sig=None,
            out_sig=None,
        )
        self._pmt = pmt
        self._holdoff_s = float(holdoff_s)
        self._seen = {}
        self._seen_lock = threading.Lock()
        self._in_port = pmt.intern("in")
        self._out_port = pmt.intern("out")
        self.message_port_register_in(self._in_port)
        self.message_port_register_out(self._out_port)
        self.set_msg_handler(self._in_port, self._handle)

    def _payload_key(self, message):
        pmt = self._pmt
        payload = pmt.cdr(message) if pmt.is_pair(message) else message
        if pmt.is_u8vector(payload):
            return bytes(pmt.u8vector_elements(payload))
        return pmt.serialize_str(payload)

    def _is_duplicate(self, message, *, now=None):
        now = time.monotonic() if now is None else float(now)
        key = self._payload_key(message)
        with self._seen_lock:
            stale = [
                old_key
                for old_key, seen_at in self._seen.items()
                if now - seen_at >= self._holdoff_s
            ]
            for old_key in stale:
                del self._seen[old_key]
            duplicate = key in self._seen
            self._seen[key] = now
        return duplicate

    def _handle(self, message):
        if not self._is_duplicate(message):
            self.message_port_pub(self._out_port, message)


def _run_headless(args):
    tb = mav_astrocast_headless(wavfile=args.wavfile, iqfile=args.iqfile,
                                zmq_addr=args.zmq_addr,
                                doppler_addr=args.doppler_addr)

    def _quit(signum, frame):
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, _quit)
    signal.signal(signal.SIGTERM, _quit)

    if args.wavfile or args.iqfile:
        time.sleep(args.wait_s)
        tb.run()
        time.sleep(0.5)  # let the ZMQ PUB flush before teardown
    else:
        tb.start()
        tb.wait()


def _run_gui(args):
    top_block_cls, Qt = _make_qt_class()
    qapp = Qt.QApplication(sys.argv)

    if args.wavfile or args.iqfile:
        time.sleep(args.wait_s)
    tb = top_block_cls(wavfile=args.wavfile, iqfile=args.iqfile,
                       zmq_addr=args.zmq_addr,
                       doppler_addr=args.doppler_addr)
    tb.start()
    tb.flowgraph_started.set()
    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()
        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--wavfile", help="48 kHz mono wav replay instead of the USRP")
    source.add_argument("--iqfile",
                        help="200 ksps cf32 IQ replay (an _IqRecorder capture) "
                             "through the full live decode banks")
    parser.add_argument("--zmq-addr", default=FRAME_ZMQ_ADDR,
                        help=f"frame PDU PUB bind address [default {FRAME_ZMQ_ADDR}]")
    parser.add_argument("--doppler-addr", default=DOPPLER_ZMQ_ADDR,
                        help=f"Doppler tune SUB address [default {DOPPLER_ZMQ_ADDR}]")
    parser.add_argument("--wait-s", type=float, default=1.0,
                        help="replay modes: delay before decode so ZMQ subscribers can join")
    parser.add_argument("--headless", action="store_true",
                        help="run without the Qt GUI (scripted replay / SSH)")
    args = parser.parse_args()

    if args.headless:
        _run_headless(args)
    else:
        _run_gui(args)


if __name__ == "__main__":
    main()
