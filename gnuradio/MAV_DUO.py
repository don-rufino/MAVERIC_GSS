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
        self.tx_lo_offset = tx_lo_offset = float(__import__('os').environ.get('GSS_TX_LO_OFFSET_HZ', -400e3))
        self.tx_freq = tx_freq = 437.575e6
        self.tx_amp = tx_amp = 0.7
        self.tx_actual_freq_label = tx_actual_freq_label = tx_actual_freq
        self.samp_ratetx = samp_ratetx = 2400000
        self.samp_rate = samp_rate = 1000000
        self.rx_lo_offset = rx_lo_offset = float(__import__('os').environ.get('GSS_RX_LO_OFFSET_HZ', 250e3))
        self.rx_freq = rx_freq = 437.575e6
        self.rx_decim = rx_decim = 5
        self.rx_actual_freq_label = rx_actual_freq_label = rx_actual_freq
        self.rx_gain = rx_gain = 40
        self.rf_gain = rf_gain = 50
        self.modindex = modindex = 1/1.5
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
            time.sleep(1.0 / (2))
        _tx_actual_freq_thread = threading.Thread(target=_tx_actual_freq_probe)
        _tx_actual_freq_thread.daemon = True
        _tx_actual_freq_thread.start()
        self.satellites_satellite_decoder_0 = satellites.core.gr_satellites_flowgraph(file = 'MAVERIC_DECODER.yml', samp_rate = (int(samp_rate/rx_decim)), grc_block = True, iq = True, options = "")
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
        self.qtgui_waterfall_sink_x_0.set_update_time(0.10)
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
        self.qtgui_freq_sink_x_1.set_update_time(0.10)
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
        self.fir_filter_xxx_1 = filter.fir_filter_ccc(rx_decim, [0.000332675437675789,-0.0003077496658079326,-0.0005765556124970317,-0.00011316717427689582,0.0005280854529701173,0.0005271750269457698,-0.0001697568513918668,-0.0007128709694370627,-0.0003659247304312885,0.0005136664258316159,0.0008155554533004761,5.7438082876615226e-05,-0.0008846476557664573,-0.0007628710591234267,0.0004127955762669444,0.0012023845920339227,0.0004748026258312166,-0.0010086969705298543,-0.0013431194238364697,9.995983418775722e-05,0.0016230839537456632,0.0011655620764940977,-0.0009432621882297099,-0.0020785757806152105,-0.0005570970242843032,0.0019362160237506032,0.0021571165416389704,-0.0005106113385409117,-0.0028525725938379765,-0.0016568709397688508,0.0019336760742589831,0.003387211123481393,0.0004655412049032748,-0.003454529447481036,-0.0032212010119110346,0.0013689876068383455,0.0046819280833005905,0.0021136386785656214,-0.0035935540217906237,-0.00515820411965251,3.445093100358828e-17,0.00575077161192894,0.004466922953724861,-0.002929744776338339,-0.007238117977976799,-0.0023611208889633417,0.006200101692229509,0.007423438131809235,-0.0011174040846526623,-0.009085707366466522,-0.0057998886331915855,0.005560645833611488,0.010719634592533112,0.0021501574665308,-0.010186892002820969,-0.01026318408548832,0.003317480208352208,0.013918614946305752,0.00710933655500412,-0.009897337295114994,-0.015542840585112572,-0.0010805262718349695,0.01640382409095764,0.013931216672062874,-0.007421362679451704,-0.02128412388265133,-0.00828002393245697,0.017346151173114777,0.02280677668750286,-0.0016788907814770937,-0.027019726112484932,-0.019278328865766525,0.015544342808425426,0.03423699364066124,0.009204991161823273,-0.032225217670202255,-0.03633292764425278,0.008750508539378643,0.0500480942428112,0.029977144673466682,-0.036387741565704346,-0.0669822171330452,-0.009797654114663601,0.07862082123756409,0.08094607293605804,-0.03907700628042221,-0.15815693140029907,-0.0901409462094307,0.21769128739833832,0.5918497443199158,0.7601303458213806,0.5918497443199158,0.21769128739833832,-0.0901409462094307,-0.15815693140029907,-0.03907700628042221,0.08094607293605804,0.07862082123756409,-0.009797654114663601,-0.0669822171330452,-0.036387741565704346,0.029977144673466682,0.0500480942428112,0.008750508539378643,-0.03633292764425278,-0.032225217670202255,0.009204991161823273,0.03423699364066124,0.015544342808425426,-0.019278328865766525,-0.027019726112484932,-0.0016788907814770937,0.02280677668750286,0.017346151173114777,-0.00828002393245697,-0.02128412388265133,-0.007421362679451704,0.013931216672062874,0.01640382409095764,-0.0010805262718349695,-0.015542840585112572,-0.009897337295114994,0.00710933655500412,0.013918614946305752,0.003317480208352208,-0.01026318408548832,-0.010186892002820969,0.0021501574665308,0.010719634592533112,0.005560645833611488,-0.0057998886331915855,-0.009085707366466522,-0.0011174040846526623,0.007423438131809235,0.006200101692229509,-0.0023611208889633417,-0.007238117977976799,-0.002929744776338339,0.004466922953724861,0.00575077161192894,3.445093100358828e-17,-0.00515820411965251,-0.0035935540217906237,0.0021136386785656214,0.0046819280833005905,0.0013689876068383455,-0.0032212010119110346,-0.003454529447481036,0.0004655412049032748,0.003387211123481393,0.0019336760742589831,-0.0016568709397688508,-0.0028525725938379765,-0.0005106113385409117,0.0021571165416389704,0.0019362160237506032,-0.0005570970242843032,-0.0020785757806152105,-0.0009432621882297099,0.0011655620764940977,0.0016230839537456632,9.995983418775722e-05,-0.0013431194238364697,-0.0010086969705298543,0.0004748026258312166,0.0012023845920339227,0.0004127955762669444,-0.0007628710591234267,-0.0008846476557664573,5.7438082876615226e-05,0.0008155554533004761,0.0005136664258316159,-0.0003659247304312885,-0.0007128709694370627,-0.0001697568513918668,0.0005271750269457698,0.0005280854529701173,-0.00011316717427689582,-0.0005765556124970317,-0.0003077496658079326,0.000332675437675789])
        self.fir_filter_xxx_1.declare_sample_delay(0)
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
        self.connect((self.fir_filter_xxx_1, 0), (self.qtgui_freq_sink_x_1, 0))
        self.connect((self.fir_filter_xxx_1, 0), (self.qtgui_waterfall_sink_x_0, 0))
        self.connect((self.fir_filter_xxx_1, 0), (self.satellites_satellite_decoder_0, 0))
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