#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: MAVERIC DUAL FRAME
# Author: Irfan Annuar - USC ISI SERC
# Copyright: USC ISI SERC
# Description: MAVERIC CubeSat ASM+Golay Uplink (AX100 Mode 5) + Full Duplex RX
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from gnuradio import blocks
from gnuradio import digital
from gnuradio import eng_notation
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import gr, pdu
from gnuradio import uhd
import time
from gnuradio import zeromq
from math import pi
import functools
import satellites.components.datasinks
import satellites.core
import sip
import threading
import pmt
import os
import json
import struct
import traceback
import numpy as np


def _decoder_yml():
    """Per-mission decoder database for the shared flowgraph.

    Explicit GSS_DECODER_YML wins; otherwise <GSS_MISSION>_DECODER.yml is
    used when that file ships next to this script (e.g. roads ->
    ROADS_DECODER.yml, carrying its measured deviations), falling back to
    the MAVERIC database for every mission without one. Missions need no
    config: RadioService already injects GSS_MISSION.
    """
    explicit = os.environ.get("GSS_DECODER_YML")
    if explicit:
        print(f"MAV_DUO decoder database: {explicit} (GSS_DECODER_YML)", flush=True)
        return explicit
    mission = (os.environ.get("GSS_MISSION") or "maveric").upper()
    candidate = f"{mission}_DECODER.yml"
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidate_path = os.path.join(script_dir, candidate)
    if os.path.isfile(candidate_path):
        print(f"MAV_DUO decoder database: {candidate} (mission {mission.lower()})", flush=True)
        return candidate_path
    print("MAV_DUO decoder database: MAVERIC_DECODER.yml (default)", flush=True)
    return "MAVERIC_DECODER.yml"


def _decoder_options(decoder_yml):
    """gr-satellites options matched to the selected database.

    --syncword_threshold 6 (default 4): at threshold BER a real frame can
    lose 5-6 of the 32 ASM bits; the extra ~2.6 false correlations/s/branch
    die in the Golay length check + RS(255,223), and the GSS flags any
    survivor via its CRC checks. The flag only parses when a deframer that
    defines it (AX100 / FX.25) is in the file — passing it with a pure
    AX.25 database (e.g. SHARJAHSAT_DECODER.yml) aborts argparse at
    flowgraph start.
    """
    try:
        with open(decoder_yml) as fh:
            text = fh.read()
    except OSError:
        text = ""
    for line in text.splitlines():
        payload = line.split('#')[0]
        if payload.strip().startswith('framing:') and (
                'AX100' in payload or 'FX.25' in payload):
            return "--syncword_threshold 6"
    return ""


def snipfcn_layout_stretch_snippet(self):
    self.top_grid_layout.setRowStretch(0, 0)
    self.top_grid_layout.setRowStretch(1, 0)
    self.top_grid_layout.setRowStretch(2, 1)
    self.top_grid_layout.setRowStretch(3, 1)
    self.top_grid_layout.setRowStretch(4, 0)


def snippets_main_after_init(tb):
    snipfcn_layout_stretch_snippet(tb)


class _PttGate(gr.basic_block):
    """T/R sequencer for an external-PA + physical-relay chain.

    Drives a complementary GPIO pair into an H-bridge; always-HIGH enable lines
    are set once at init and left alone. The pair (toggle_mask) flips to its
    `live_value` during transmit and to the complement at idle/RX. The PA is
    always powered, so the pair must reach the live/TX state and the relays must
    settle BEFORE any RF, and must not flip back until RF has drained. TX gain is
    gated with the same sequence: the sink idles at idle_gain (0 dB) so the TX LO
    feedthrough cannot land in the receive passband between bursts, and burst_gain
    is applied only while the switch is off the RX position. Per TX PDU:
      1. drive the pair live       (e.g. GPIO0 HIGH / GPIO2 LOW)
      2. wait lead_s               (relays settle cold, before any RF)
      3. raise TX gain to burst_gain()   (switch has left RX; still pre-RF)
      4. forward the PDU           (PA drives the antenna)
      5. hold for air-time + tail_s, then drop TX gain to idle_gain BEFORE
         flipping the pair back to idle/RX (leak silenced before RX reconnects;
         relays move only after RF is gone)
    Sequences are serialized so back-to-back commands never flip mid-burst.
    """

    def __init__(self, usrp_sink, bank="FP0", toggle_mask=0x1, live_value=0x1,
                 lead_s=1.0, tail_s=1.0, baud=9600,
                 burst_gain=lambda: 0.0, idle_gain=0.0):
        gr.basic_block.__init__(self, name="ptt_gate", in_sig=[], out_sig=[])
        self.sink = usrp_sink
        self.bank = bank
        self.toggle_mask = toggle_mask
        self.live_value = live_value
        self.lead_s = lead_s
        self.tail_s = tail_s
        self.baud = baud
        self.burst_gain = burst_gain
        self.idle_gain = idle_gain
        self._lock = threading.Lock()
        self.message_port_register_in(pmt.intern("pdu_in"))
        self.message_port_register_out(pmt.intern("pdu_out"))
        self.set_msg_handler(pmt.intern("pdu_in"), self._handle)

    def _line(self, live):
        # Flip the complementary pair to live/idle; enables (outside the mask) untouched.
        val = self.live_value if live else (self.toggle_mask & ~self.live_value)
        self.sink.set_gpio_attr(self.bank, "OUT", val, self.toggle_mask)

    def _air_seconds(self, msg):
        try:
            return len(pmt.u8vector_elements(pmt.cdr(msg))) * 8.0 / self.baud
        except Exception:
            return 0.5  # conservative fallback if payload length is unavailable

    def _handle(self, msg):
        # Never let an exception escape: it would kill this block's message
        # thread for the rest of the session (GR logs it and the thread
        # exits), and the finally guarantees the switch cannot stay latched
        # on TX with the gain up.
        with self._lock:
            try:
                air_s = self._air_seconds(msg)
                gain_db = float(self.burst_gain())
                self._line(True)                                   # live: GPIO0 HIGH, GPIO2 LOW
                print(f"[PTT] TX key  -> GPIO0 HIGH / GPIO2 LOW, lead {self.lead_s:.1f}s", flush=True)
                time.sleep(self.lead_s)                            # cold-switch settle, pre-RF
                self.sink.set_gain(gain_db, 0)                     # raise gain only once the switch is off RX
                self.message_port_pub(pmt.intern("pdu_out"), msg)  # PA drives antenna
                print(f"[PTT] RF out  -> gain {gain_db:.0f} dB, air {air_s:.2f}s, tail {self.tail_s:.1f}s", flush=True)
                time.sleep(air_s + self.tail_s)                    # hold until RF fully drained
            except Exception as exc:
                try:
                    print(f"[PTT] ERROR   -> {exc!r}; forcing idle/RX", flush=True)
                except Exception:
                    pass
            finally:
                self._safe_idle()

    def _safe_idle(self):
        # Restore idle even mid-failure, in leak-safe order: gain to idle
        # first, relay back to RX second. Both hardware steps run before any
        # printing (stdout is a pipe to the GSS supervisor and can itself
        # raise if the server died), and each is guarded separately so a
        # dead UHD control channel cannot leave the switch driven to TX.
        gain_err = line_err = None
        try:
            self.sink.set_gain(self.idle_gain, 0)
        except Exception as exc:
            gain_err = exc
        try:
            self._line(False)                                  # idle: GPIO0 LOW, GPIO2 HIGH
        except Exception as exc:
            line_err = exc
        try:
            if gain_err is not None:
                print(f"[PTT] ERROR   -> idle gain restore failed: {gain_err!r}", flush=True)
            if line_err is not None:
                print(f"[PTT] ERROR   -> RX switch restore failed: {line_err!r}", flush=True)
            else:
                print("[PTT] RX       -> GPIO0 LOW / GPIO2 HIGH", flush=True)
        except Exception:
            pass


class _WaterfallLogger(gr.sync_block):
    """Post-pass waterfall recorder for the decimated RX stream.

    Appends timestamped 1024-bin dB rows to waterfall_<mission>_<start>.dat while the
    flowgraph runs; stop() renders a SatNOGS-style PNG via waterfall_render
    and deletes the .dat on success. Hard crashes leave the .dat behind, so
    __init__ sweeps the output dir for leftovers and renders them in a
    background thread. Every failure path prints and disables the block —
    waterfall capture must never take down the radio.
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
            mission = os.environ.get("GSS_MISSION") or "maveric"
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
    IQ capture must never take down the radio.

    Offline replay: gr_satellites MAVERIC_DECODER.yml --iq --samp_rate 200e3
    --rawfile <capture>.sigmf-data
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
            mission = os.environ.get("GSS_MISSION") or "maveric"
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


class MAV_DUO(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "MAVERIC DUAL FRAME", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("MAVERIC DUAL FRAME")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "MAV_DUO")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)
        self.flowgraph_started = threading.Event()

        ##################################################
        # Variables
        ##################################################
        self.tx_actual_freq = tx_actual_freq = 0
        self.rx_actual_freq = rx_actual_freq = 0
        self.zmq_port_tx = zmq_port_tx = "52002"
        self.zmq_port_rx = zmq_port_rx = "52001"
        self.decoder_yml = decoder_yml = _decoder_yml()
        self.decoder_options = decoder_options = _decoder_options(decoder_yml)
        self.tx_lo_offset = tx_lo_offset = float(__import__('os').environ.get('GSS_TX_LO_OFFSET_HZ', -400e3))
        self.tx_freq = tx_freq = float(__import__('os').environ.get('GSS_TX_FREQ_HZ', 437.575e6))
        self.tx_amp = tx_amp = 0.7
        self.tx_actual_freq_label = tx_actual_freq_label = tx_actual_freq
        self.samp_ratetx = samp_ratetx = 2400000
        self.samp_rate = samp_rate = 1000000
        self.rx_lo_offset = rx_lo_offset = float(__import__('os').environ.get('GSS_RX_LO_OFFSET_HZ', 250e3))
        self.rx_freq = rx_freq = float(__import__('os').environ.get('GSS_RX_FREQ_HZ', 437.575e6))
        self.rx_decim = rx_decim = 5
        self.rx_actual_freq_label = rx_actual_freq_label = rx_actual_freq
        self.rx_gain = rx_gain = 40
        self.rf_gain = rf_gain = 50
        self.modindex = modindex = 0.5
        self.baud = baud = 9600

        ##################################################
        # Blocks
        ##################################################

        self._rf_gain_range = qtgui.Range(0, 89, 1, 50, 200)
        self._rf_gain_win = qtgui.RangeWidget(self._rf_gain_range, self.set_rf_gain, "TX Gain (dB)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._rf_gain_win, 0, 1, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(1, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self._rx_gain_range = qtgui.Range(0, 76, 1, 40, 200)
        self._rx_gain_win = qtgui.RangeWidget(self._rx_gain_range, self.set_rx_gain, "RX Gain (dB)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._rx_gain_win, 4, 0, 1, 2)
        for r in range(4, 5):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.uhd_usrp_source_0 = uhd.usrp_source(
            ",".join(("", "")),
            uhd.stream_args(
                cpu_format="fc32",
                args='',
                channels=list(range(0,1)),
            ),
        )
        self.uhd_usrp_source_0.set_subdev_spec('A:A', 0)
        self.uhd_usrp_source_0.set_samp_rate(samp_rate)
        # No synchronization enforced.

        self.uhd_usrp_source_0.set_center_freq(uhd.tune_request(rx_freq, rx_lo_offset), 0)
        self.uhd_usrp_source_0.set_antenna("RX2", 0)
        self.uhd_usrp_source_0.set_gain(rx_gain, 0)
        self.uhd_usrp_sink_0 = uhd.usrp_sink(
            ",".join(("", "")),
            uhd.stream_args(
                cpu_format="fc32",
                args='',
                channels=list(range(0,1)),
            ),
            '',
        )
        self.uhd_usrp_sink_0.set_subdev_spec("A:A", 0)
        self.uhd_usrp_sink_0.set_samp_rate(samp_ratetx)
        # No synchronization enforced.

        self.uhd_usrp_sink_0.set_center_freq(uhd.tune_request(tx_freq, tx_lo_offset), 0)
        self.uhd_usrp_sink_0.set_antenna('TX/RX', 0)
        self.uhd_usrp_sink_0.set_gain(0, 0)
        
        # ==================================================
        # B210 GPIO -> H-bridge PTT  (external PA + physical relays)
        # --------------------------------------------------
        # Manual lines (not ATR), driven by _PttGate. GPIO0/GPIO2 are a
        # complementary H-bridge pair; GPIO1/GPIO3 are always-HIGH enables:
        #   GPIO0 / J504 pin 1 : LOW idle/RX, HIGH when live (TX)
        #   GPIO1 / J504 pin 2 : always HIGH (enable)
        #   GPIO2 / J504 pin 3 : HIGH idle/RX, LOW when live  (inverse of GPIO0)
        #   GPIO3 / J504 pin 4 : always HIGH (enable)
        # During a burst GPIO0 -> HIGH and GPIO2 -> LOW together (lead before RF,
        # held through the burst, flipped back only after RF drains -> cold switch).
        # FAIL-SAFE = idle/RX state, set by each external resistor:
        #   GPIO0 -> PULL-DOWN (idle LOW)        GPIO2 -> PULL-UP (idle HIGH)
        #   GPIO1, GPIO3 -> pull to each enable's safe state (up = on, down = off, on crash).
        # ==================================================

        LIVE_PIN = 1 << 0   # GPIO0 / J504 pin 1 : LOW idle, HIGH when live (TX)
        EN_PIN   = 1 << 1   # GPIO1 / J504 pin 2 : always HIGH (enable)
        INV_PIN  = 1 << 2   # GPIO2 / J504 pin 3 : HIGH idle, LOW when live (inverse of GPIO0)
        EN3_PIN  = 1 << 3   # GPIO3 / J504 pin 4 : always HIGH (enable)
        MASK     = LIVE_PIN | EN_PIN | INV_PIN | EN3_PIN
        IDLE_OUT = EN_PIN | INV_PIN | EN3_PIN   # idle/RX: GPIO0 LOW, GPIO1/2/3 HIGH

        # Manual control (NOT ATR); preload the idle/RX state (GPIO0 LOW, rest HIGH).
        self.uhd_usrp_sink_0.set_gpio_attr("FP0", "CTRL", 0x0,      MASK)  # manual, not ATR
        self.uhd_usrp_sink_0.set_gpio_attr("FP0", "OUT",  IDLE_OUT, MASK)  # idle: GPIO0 low, rest high
        self.uhd_usrp_sink_0.set_gpio_attr("FP0", "DDR",  MASK,     MASK)  # outputs

        # Sequencer flips the GPIO0/GPIO2 pair for TX (enables untouched); pre-key before RF.
        self.ptt_gate = _PttGate(
            self.uhd_usrp_sink_0, bank="FP0",
            toggle_mask=(LIVE_PIN | INV_PIN), live_value=LIVE_PIN,
            lead_s=1.5, tail_s=0.2, baud=baud,
            burst_gain=self.get_rf_gain, idle_gain=0.0)
        
        
        # ==================================================
        # B210 GPIO PTT Configuration END
        # ==================================================
        
        
        self._tx_amp_range = qtgui.Range(0, 1.0, 0.01, 0.7, 200)
        self._tx_amp_win = qtgui.RangeWidget(self._tx_amp_range, self.set_tx_amp, "TX Amplitude", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._tx_amp_win, 0, 0, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.zeromq_sub_msg_source_txcmd = zeromq.sub_msg_source("tcp://127.0.0.1:52004", 100, False)
        self.zeromq_sub_msg_source_rxcmd = zeromq.sub_msg_source("tcp://127.0.0.1:52003", 100, False)
        self.zeromq_sub_msg_source_0 = zeromq.sub_msg_source(f"tcp://127.0.0.1:{zmq_port_tx}", 200, False)
        self.zeromq_pub_msg_sink_0 = zeromq.pub_msg_sink(f"tcp://127.0.0.1:{zmq_port_rx}", 100, True)
        self._tx_actual_freq_label_tool_bar = Qt.QToolBar(self)

        if lambda v: f"{float(v)/1e6:.6f} MHz":
            self._tx_actual_freq_label_formatter = lambda v: f"{float(v)/1e6:.6f} MHz"
        else:
            self._tx_actual_freq_label_formatter = lambda x: eng_notation.num_to_str(x)

        self._tx_actual_freq_label_tool_bar.addWidget(Qt.QLabel("USRP TX achieved"))
        self._tx_actual_freq_label_label = Qt.QLabel(str(self._tx_actual_freq_label_formatter(self.tx_actual_freq_label)))
        self._tx_actual_freq_label_tool_bar.addWidget(self._tx_actual_freq_label_label)
        self.top_grid_layout.addWidget(self._tx_actual_freq_label_tool_bar, 1, 1, 1, 1)
        for r in range(1, 2):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(1, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        def _tx_actual_freq_probe():
          self.flowgraph_started.wait()
          while True:

            val = self.uhd_usrp_sink_0.get_center_freq(0)
            try:
              try:
                self.doc.add_next_tick_callback(functools.partial(self.set_tx_actual_freq,val))
              except AttributeError:
                self.set_tx_actual_freq(val)
            except AttributeError:
              pass
            except RuntimeError:
              return  # deleted C++ QWidget: sip raises RuntimeError, not AttributeError
            time.sleep(1.0 / (2))
        _tx_actual_freq_thread = threading.Thread(target=_tx_actual_freq_probe)
        _tx_actual_freq_thread.daemon = True
        _tx_actual_freq_thread.start()
        self.satellites_satellite_decoder_0 = satellites.core.gr_satellites_flowgraph(file = decoder_yml, samp_rate = (int(samp_rate/rx_decim)), grc_block = True, iq = True, options = decoder_options)
        self.satellites_hexdump_sink_0_0 = satellites.components.datasinks.hexdump_sink(options="")
        self.satellites_hexdump_sink_0 = satellites.components.datasinks.hexdump_sink(options="")
        self._rx_actual_freq_label_tool_bar = Qt.QToolBar(self)

        if lambda v: f"{float(v)/1e6:.6f} MHz":
            self._rx_actual_freq_label_formatter = lambda v: f"{float(v)/1e6:.6f} MHz"
        else:
            self._rx_actual_freq_label_formatter = lambda x: eng_notation.num_to_str(x)

        self._rx_actual_freq_label_tool_bar.addWidget(Qt.QLabel("USRP RX achieved"))
        self._rx_actual_freq_label_label = Qt.QLabel(str(self._rx_actual_freq_label_formatter(self.rx_actual_freq_label)))
        self._rx_actual_freq_label_tool_bar.addWidget(self._rx_actual_freq_label_label)
        self.top_grid_layout.addWidget(self._rx_actual_freq_label_tool_bar, 1, 0, 1, 1)
        for r in range(1, 2):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        def _rx_actual_freq_probe():
          self.flowgraph_started.wait()
          while True:

            val = self.uhd_usrp_source_0.get_center_freq(0)
            try:
              try:
                self.doc.add_next_tick_callback(functools.partial(self.set_rx_actual_freq,val))
              except AttributeError:
                self.set_rx_actual_freq(val)
            except AttributeError:
              pass
            except RuntimeError:
              return  # deleted C++ QWidget: sip raises RuntimeError, not AttributeError
            time.sleep(1.0 / (2))
        _rx_actual_freq_thread = threading.Thread(target=_rx_actual_freq_probe)
        _rx_actual_freq_thread.daemon = True
        _rx_actual_freq_thread.start()
        self.qtgui_waterfall_sink_x_0 = qtgui.waterfall_sink_c(
            1024, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            0, #fc
            (samp_rate/rx_decim), #bw
            "", #name
            1, #number of inputs
            None # parent
        )
        self.qtgui_waterfall_sink_x_0.set_update_time(0.05)
        self.qtgui_waterfall_sink_x_0.enable_grid(False)
        self.qtgui_waterfall_sink_x_0.enable_axis_labels(True)



        labels = ['', '', '', '', '',
                  '', '', '', '', '']
        colors = [0, 0, 0, 0, 0,
                  0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
                  1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_waterfall_sink_x_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_waterfall_sink_x_0.set_line_label(i, labels[i])
            self.qtgui_waterfall_sink_x_0.set_color_map(i, colors[i])
            self.qtgui_waterfall_sink_x_0.set_line_alpha(i, alphas[i])

        self.qtgui_waterfall_sink_x_0.set_intensity_range(-140, 10)

        self._qtgui_waterfall_sink_x_0_win = sip.wrapinstance(self.qtgui_waterfall_sink_x_0.qwidget(), Qt.QWidget)

        self.top_grid_layout.addWidget(self._qtgui_waterfall_sink_x_0_win, 3, 0, 1, 2)
        for r in range(3, 4):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.qtgui_freq_sink_x_1 = qtgui.freq_sink_c(
            1024, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            0, #fc
            (samp_rate/rx_decim), #bw
            "", #name
            1,
            None # parent
        )
        self.qtgui_freq_sink_x_1.set_update_time(0.05)
        self.qtgui_freq_sink_x_1.set_y_axis((-140), 10)
        self.qtgui_freq_sink_x_1.set_y_label('Relative Gain', 'dB')
        self.qtgui_freq_sink_x_1.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.qtgui_freq_sink_x_1.enable_autoscale(False)
        self.qtgui_freq_sink_x_1.enable_grid(False)
        self.qtgui_freq_sink_x_1.set_fft_average(1.0)
        self.qtgui_freq_sink_x_1.enable_axis_labels(True)
        self.qtgui_freq_sink_x_1.enable_control_panel(False)
        self.qtgui_freq_sink_x_1.set_fft_window_normalized(False)

        self.qtgui_freq_sink_x_1.disable_legend()


        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_freq_sink_x_1.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_freq_sink_x_1.set_line_label(i, labels[i])
            self.qtgui_freq_sink_x_1.set_line_width(i, widths[i])
            self.qtgui_freq_sink_x_1.set_line_color(i, colors[i])
            self.qtgui_freq_sink_x_1.set_line_alpha(i, alphas[i])

        self._qtgui_freq_sink_x_1_win = sip.wrapinstance(self.qtgui_freq_sink_x_1.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._qtgui_freq_sink_x_1_win, 2, 0, 1, 1)
        for r in range(2, 3):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.qtgui_freq_sink_x_0 = qtgui.freq_sink_c(
            2048, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            tx_freq, #fc
            samp_ratetx, #bw
            "", #name
            1,
            None # parent
        )
        self.qtgui_freq_sink_x_0.set_update_time(0.05)
        self.qtgui_freq_sink_x_0.set_y_axis((-100), 0)
        self.qtgui_freq_sink_x_0.set_y_label('TX Spectrum', 'dB')
        self.qtgui_freq_sink_x_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.qtgui_freq_sink_x_0.enable_autoscale(True)
        self.qtgui_freq_sink_x_0.enable_grid(True)
        self.qtgui_freq_sink_x_0.set_fft_average(0.2)
        self.qtgui_freq_sink_x_0.enable_axis_labels(True)
        self.qtgui_freq_sink_x_0.enable_control_panel(False)
        self.qtgui_freq_sink_x_0.set_fft_window_normalized(False)

        self.qtgui_freq_sink_x_0.disable_legend()


        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_freq_sink_x_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_freq_sink_x_0.set_line_label(i, labels[i])
            self.qtgui_freq_sink_x_0.set_line_width(i, widths[i])
            self.qtgui_freq_sink_x_0.set_line_color(i, colors[i])
            self.qtgui_freq_sink_x_0.set_line_alpha(i, alphas[i])

        self._qtgui_freq_sink_x_0_win = sip.wrapinstance(self.qtgui_freq_sink_x_0.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._qtgui_freq_sink_x_0_win, 2, 1, 1, 1)
        for r in range(2, 3):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(1, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.pdu_pdu_to_tagged_stream_0 = pdu.pdu_to_tagged_stream(gr.types.byte_t, 'packet_len')
        self.fir_filter_xxx_1 = filter.fir_filter_ccf(rx_decim, firdes.low_pass(2.0, samp_rate, 80e3, 15e3))
        self.fir_filter_xxx_1.declare_sample_delay(0)
        self.waterfall_logger = _WaterfallLogger()
        self.iq_recorder = _IqRecorder(samp_rate=(int(samp_rate/rx_decim)))
        self.digital_gfsk_mod_0 = digital.gfsk_mod(
            samples_per_symbol=(int(samp_ratetx/baud)),
            sensitivity=((pi*modindex) / int(samp_ratetx/baud)),
            bt=0.5,
            verbose=False,
            log=False,
            do_unpack=True)
        self.blocks_multiply_const_vxx_0 = blocks.multiply_const_cc(tx_amp)


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.satellites_satellite_decoder_0, 'out'), (self.satellites_hexdump_sink_0, 'in'))
        self.msg_connect((self.satellites_satellite_decoder_0, 'out'), (self.zeromq_pub_msg_sink_0, 'in'))
        self.msg_connect((self.zeromq_sub_msg_source_0, 'out'), (self.ptt_gate, 'pdu_in'))
        self.msg_connect((self.ptt_gate, 'pdu_out'), (self.pdu_pdu_to_tagged_stream_0, 'pdus'))
        self.msg_connect((self.zeromq_sub_msg_source_0, 'out'), (self.satellites_hexdump_sink_0_0, 'in'))
        self.msg_connect((self.zeromq_sub_msg_source_rxcmd, 'out'), (self.uhd_usrp_source_0, 'command'))
        self.msg_connect((self.zeromq_sub_msg_source_txcmd, 'out'), (self.uhd_usrp_sink_0, 'command'))
        self.connect((self.blocks_multiply_const_vxx_0, 0), (self.qtgui_freq_sink_x_0, 0))
        self.connect((self.blocks_multiply_const_vxx_0, 0), (self.uhd_usrp_sink_0, 0))
        self.connect((self.digital_gfsk_mod_0, 0), (self.blocks_multiply_const_vxx_0, 0))
        self.connect((self.fir_filter_xxx_1, 0), (self.iq_recorder, 0))
        self.connect((self.fir_filter_xxx_1, 0), (self.qtgui_freq_sink_x_1, 0))
        self.connect((self.fir_filter_xxx_1, 0), (self.qtgui_waterfall_sink_x_0, 0))
        self.connect((self.fir_filter_xxx_1, 0), (self.satellites_satellite_decoder_0, 0))
        self.connect((self.fir_filter_xxx_1, 0), (self.waterfall_logger, 0))
        self.connect((self.pdu_pdu_to_tagged_stream_0, 0), (self.digital_gfsk_mod_0, 0))
        self.connect((self.uhd_usrp_source_0, 0), (self.fir_filter_xxx_1, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "MAV_DUO")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_tx_actual_freq(self):
        return self.tx_actual_freq

    def set_tx_actual_freq(self, tx_actual_freq):
        self.tx_actual_freq = tx_actual_freq
        self.set_tx_actual_freq_label(self.tx_actual_freq)

    def get_rx_actual_freq(self):
        return self.rx_actual_freq

    def set_rx_actual_freq(self, rx_actual_freq):
        self.rx_actual_freq = rx_actual_freq
        self.set_rx_actual_freq_label(self.rx_actual_freq)

    def get_zmq_port_tx(self):
        return self.zmq_port_tx

    def set_zmq_port_tx(self, zmq_port_tx):
        self.zmq_port_tx = zmq_port_tx

    def get_decoder_yml(self):
        return self.decoder_yml

    def set_decoder_yml(self, decoder_yml):
        self.decoder_yml = decoder_yml

    def get_decoder_options(self):
        return self.decoder_options

    def set_decoder_options(self, decoder_options):
        self.decoder_options = decoder_options

    def get_zmq_port_rx(self):
        return self.zmq_port_rx

    def set_zmq_port_rx(self, zmq_port_rx):
        self.zmq_port_rx = zmq_port_rx

    def get_tx_lo_offset(self):
        return self.tx_lo_offset

    def set_tx_lo_offset(self, tx_lo_offset):
        self.tx_lo_offset = tx_lo_offset
        self.uhd_usrp_sink_0.set_center_freq(uhd.tune_request(self.tx_freq, self.tx_lo_offset), 0)

    def get_tx_freq(self):
        return self.tx_freq

    def set_tx_freq(self, tx_freq):
        self.tx_freq = tx_freq
        self.qtgui_freq_sink_x_0.set_frequency_range(self.tx_freq, self.samp_ratetx)
        self.uhd_usrp_sink_0.set_center_freq(uhd.tune_request(self.tx_freq, self.tx_lo_offset), 0)

    def get_tx_amp(self):
        return self.tx_amp

    def set_tx_amp(self, tx_amp):
        self.tx_amp = tx_amp
        self.blocks_multiply_const_vxx_0.set_k(self.tx_amp)

    def get_tx_actual_freq_label(self):
        return self.tx_actual_freq_label

    def set_tx_actual_freq_label(self, tx_actual_freq_label):
        self.tx_actual_freq_label = tx_actual_freq_label
        Qt.QMetaObject.invokeMethod(self._tx_actual_freq_label_label, "setText", Qt.Q_ARG("QString", str(self._tx_actual_freq_label_formatter(self.tx_actual_freq_label))))

    def get_samp_ratetx(self):
        return self.samp_ratetx

    def set_samp_ratetx(self, samp_ratetx):
        self.samp_ratetx = samp_ratetx
        self.qtgui_freq_sink_x_0.set_frequency_range(self.tx_freq, self.samp_ratetx)
        self.uhd_usrp_sink_0.set_samp_rate(self.samp_ratetx)

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.fir_filter_xxx_1.set_taps(firdes.low_pass(2.0, self.samp_rate, 80e3, 15e3))
        self.qtgui_freq_sink_x_1.set_frequency_range(0, (self.samp_rate/self.rx_decim))
        self.qtgui_waterfall_sink_x_0.set_frequency_range(0, (self.samp_rate/self.rx_decim))
        self.uhd_usrp_source_0.set_samp_rate(self.samp_rate)

    def get_rx_lo_offset(self):
        return self.rx_lo_offset

    def set_rx_lo_offset(self, rx_lo_offset):
        self.rx_lo_offset = rx_lo_offset
        self.uhd_usrp_source_0.set_center_freq(uhd.tune_request(self.rx_freq, self.rx_lo_offset), 0)

    def get_rx_freq(self):
        return self.rx_freq

    def set_rx_freq(self, rx_freq):
        self.rx_freq = rx_freq
        self.uhd_usrp_source_0.set_center_freq(uhd.tune_request(self.rx_freq, self.rx_lo_offset), 0)

    def get_rx_decim(self):
        return self.rx_decim

    def set_rx_decim(self, rx_decim):
        self.rx_decim = rx_decim
        self.qtgui_freq_sink_x_1.set_frequency_range(0, (self.samp_rate/self.rx_decim))
        self.qtgui_waterfall_sink_x_0.set_frequency_range(0, (self.samp_rate/self.rx_decim))

    def get_rx_actual_freq_label(self):
        return self.rx_actual_freq_label

    def set_rx_actual_freq_label(self, rx_actual_freq_label):
        self.rx_actual_freq_label = rx_actual_freq_label
        Qt.QMetaObject.invokeMethod(self._rx_actual_freq_label_label, "setText", Qt.Q_ARG("QString", str(self._rx_actual_freq_label_formatter(self.rx_actual_freq_label))))

    def get_rx_gain(self):
        return self.rx_gain

    def set_rx_gain(self, rx_gain):
        self.rx_gain = rx_gain
        self.uhd_usrp_source_0.set_gain(self.rx_gain, 0)

    def get_rf_gain(self):
        return self.rf_gain

    def set_rf_gain(self, rf_gain):
        # Burst gain only: applied by _PttGate at key-up. Hardware idles at
        # 0 dB so the TX LO feedthrough stays quiet while listening.
        self.rf_gain = rf_gain

    def get_modindex(self):
        return self.modindex

    def set_modindex(self, modindex):
        self.modindex = modindex

    def get_baud(self):
        return self.baud

    def set_baud(self, baud):
        self.baud = baud





def main(top_block_cls=MAV_DUO, options=None):

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls()
    snippets_main_after_init(tb)
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

if __name__ == '__main__':
    main()