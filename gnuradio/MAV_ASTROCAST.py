#!/usr/bin/env python3
"""MAV_ASTROCAST — RX-only GNU Radio decoder for Astrocast 0.1 (NORAD 43798).

Qt GUI top block supervised by the GSS RadioService (set
`platform.radio.script: gnuradio/MAV_ASTROCAST.py`). Its live B210 path
mirrors MAV_DUO's RX acquisition conventions: A:A/RX2, 1 Msps, parked LO,
gain 40, explicit idle/RX relay GPIO, 5x decimation, and a broad 200 ksps
spectrum/waterfall before any beacon filtering. It then decodes the 1k2 FSK
FX.25 beacon on 437.150 MHz via gr-satellites (ASTROCAST_DECODER.yml),
running both NRZ-I and legacy NRZ failsafe deframers through gr-satellites'
native Astrocast support. Deframed PDUs publish on the GSS RX frame bus.

Input modes:
  default          USRP B210 (same subdev/antenna/gain conventions as
                   MAV_DUO), parked-LO tuning from GSS_RX_FREQ_HZ /
                   GSS_RX_LO_OFFSET_HZ (RadioService injects both), and
                   Doppler tune messages consumed on tcp://127.0.0.1:52003
                   into the UHD command port.
  --wavfile PATH   Offline replay of a 48 kHz mono FM-demodulated wav
                   recording (e.g. satellite-recordings/astrocast.wav).

Pass --headless to skip the Qt GUI entirely (scripted replay / SSH use).
"""

import argparse
from math import pi
import os
import signal
import sys
import threading
import time

from gnuradio import blocks, gr, zeromq
from gnuradio import filter as gr_filter
from gnuradio.filter import firdes

import satellites
import satellites.components.datasinks
import satellites.core


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DECODER_YML = os.path.join(SCRIPT_DIR, "ASTROCAST_DECODER.yml")

DEFAULT_RX_FREQ_HZ = 437.150e6
DEFAULT_RX_LO_OFFSET_HZ = 250e3
SAMP_RATE = 1_000_000
RX_DECIM = 5
RX_GAIN = 40
ACQUISITION_RATE = SAMP_RATE // RX_DECIM
# These firdes parameters reproduce MAV_DUO's checked-in 181-tap RX FIR.
# Keep the same gain, response, decimation, decoder feed, and display tap so
# an Astrocast pass sees the same RF acquisition topology as MAVERIC.
RX_FRONTEND_GAIN = 2.0
RX_FRONTEND_CUTOFF_HZ = 190_000.0
RX_FRONTEND_TRANSITION_HZ = 13_333.0
BEACON_CHANNEL_CUTOFF_HZ = 5_000.0
BEACON_CHANNEL_TRANSITION_HZ = 2_000.0
BEACON_CHANNEL_DECIM = 10
BEACON_DECODER_RATE = ACQUISITION_RATE // BEACON_CHANNEL_DECIM
BEACON_DEVIATION_HZ = 1_200.0
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
    """Pass a centred 1k2 FSK signal with up to about 3 kHz residual error."""
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


def _build_core(tb, wavfile, zmq_addr, doppler_addr):
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
    else:
        from gnuradio import analog, uhd

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

        tb.rx_lpf = gr_filter.fir_filter_ccc(
            RX_DECIM, _rx_frontend_taps())
        tb.beacon_channelizer = gr_filter.fir_filter_ccf(
            BEACON_CHANNEL_DECIM, _beacon_channel_taps())
        tb.beacon_demodulator = analog.quadrature_demod_cf(
            BEACON_DECODER_RATE / (2.0 * pi * BEACON_DEVIATION_HZ))
        tb.satellites_satellite_decoder_0 = satellites.core.gr_satellites_flowgraph(
            file=DECODER_YML, samp_rate=BEACON_DECODER_RATE, iq=False,
            grc_block=True, options=DECODER_OPTIONS)
        tb.zeromq_sub_msg_source_rxcmd = zeromq.sub_msg_source(
            doppler_addr, 100, False)

        tb.connect((tb.uhd_usrp_source_0, 0), (tb.rx_lpf, 0))
        tb.connect(
            (tb.rx_lpf, 0),
            (tb.beacon_channelizer, 0),
            (tb.beacon_demodulator, 0),
            (tb.satellites_satellite_decoder_0, 0),
        )
        tb.msg_connect(
            (tb.zeromq_sub_msg_source_rxcmd, 'out'),
            (tb.uhd_usrp_source_0, 'command'))

    tb.msg_connect(
        (tb.satellites_satellite_decoder_0, "out"),
        (tb.zeromq_pub_msg_sink_0, "in"))
    tb.msg_connect(
        (tb.satellites_satellite_decoder_0, "out"),
        (tb.satellites_hexdump_sink_0, "in"))


class mav_astrocast_headless(gr.top_block):

    def __init__(self, wavfile=None, zmq_addr=FRAME_ZMQ_ADDR,
                 doppler_addr=DOPPLER_ZMQ_ADDR):
        gr.top_block.__init__(self, "MAV_ASTROCAST", catch_exceptions=True)
        _build_core(self, wavfile, zmq_addr, doppler_addr)


def _make_qt_class():
    """Import Qt lazily so --headless never touches PyQt5/qtgui."""
    from PyQt5 import Qt, QtCore
    from gnuradio import qtgui
    from gnuradio.fft import window
    import sip

    class mav_astrocast(gr.top_block, Qt.QWidget):

        def __init__(self, wavfile=None, zmq_addr=FRAME_ZMQ_ADDR,
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

            _build_core(self, wavfile, zmq_addr, doppler_addr)
            self.wavfile = wavfile

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
                spectrum_tap = self.rx_lpf

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


def _run_headless(args):
    tb = mav_astrocast_headless(wavfile=args.wavfile, zmq_addr=args.zmq_addr,
                                doppler_addr=args.doppler_addr)

    def _quit(signum, frame):
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, _quit)
    signal.signal(signal.SIGTERM, _quit)

    if args.wavfile:
        time.sleep(args.wait_s)
        tb.run()
        time.sleep(0.5)  # let the ZMQ PUB flush before teardown
    else:
        tb.start()
        tb.wait()


def _run_gui(args):
    top_block_cls, Qt = _make_qt_class()
    qapp = Qt.QApplication(sys.argv)

    if args.wavfile:
        time.sleep(args.wait_s)
    tb = top_block_cls(wavfile=args.wavfile, zmq_addr=args.zmq_addr,
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
    parser.add_argument("--wavfile", help="48 kHz mono wav replay instead of the USRP")
    parser.add_argument("--zmq-addr", default=FRAME_ZMQ_ADDR,
                        help=f"frame PDU PUB bind address [default {FRAME_ZMQ_ADDR}]")
    parser.add_argument("--doppler-addr", default=DOPPLER_ZMQ_ADDR,
                        help=f"Doppler tune SUB address [default {DOPPLER_ZMQ_ADDR}]")
    parser.add_argument("--wait-s", type=float, default=1.0,
                        help="wav mode: delay before decode so ZMQ subscribers can join")
    parser.add_argument("--headless", action="store_true",
                        help="run without the Qt GUI (scripted replay / SSH)")
    args = parser.parse_args()

    if args.headless:
        _run_headless(args)
    else:
        _run_gui(args)


if __name__ == "__main__":
    main()
