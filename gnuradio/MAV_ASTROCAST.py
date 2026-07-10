#!/usr/bin/env python3
"""MAV_ASTROCAST — RX-only GNU Radio decoder for Astrocast 0.1 (NORAD 43798).

Qt GUI top block (MAV_DUO-style spectrum/waterfall + RX gain slider +
achieved-frequency readout) supervised by the GSS RadioService (set
`platform.radio.script: gnuradio/MAV_ASTROCAST.py`). Decodes the 1k2 FSK
FX.25 beacon on 437.150 MHz via gr-satellites
(ASTROCAST_DECODER.yml), running both NRZ-I and legacy NRZ failsafe
deframers. Seven overlapping real-demodulation bins provide immediate
carrier-offset coverage; a narrow AFC decoder remains in parallel for weak
signals. Deframed PDUs publish on the GSS RX frame bus.

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
import os
import signal
import sys
import threading
import time

import numpy as np
import pmt

from gnuradio import analog, blocks, gr, zeromq
from gnuradio import filter as gr_filter
from gnuradio.filter import firdes

import satellites
import satellites.components.datasinks
import satellites.core

from astrocast_1k2_afc import AveragedFftPower, estimate_fsk_center


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DECODER_YML = os.path.join(SCRIPT_DIR, "ASTROCAST_DECODER.yml")

DEFAULT_RX_FREQ_HZ = 437.150e6
DEFAULT_RX_LO_OFFSET_HZ = 250e3
SAMP_RATE = 1_000_000
RX_DECIM = 5
RX_GAIN = 40
WAV_SAMP_RATE = 48_000
DECODER_OPTIONS = "--clk_limit 0.008"
BEACON_DEVIATION_HZ = 1_200.0

DEFAULT_AFC_SEARCH_HZ = 20_000.0
DEFAULT_AFC_BIAS_HZ = 0.0
BEACON_CHANNEL_CUTOFF_HZ = 4_000.0
BEACON_CHANNEL_TRANSITION_HZ = 2_000.0
BEACON_CHANNEL_DECIM = 10
BEACON_BIN_CENTERS_HZ = (
    -18_000.0, -12_000.0, -6_000.0, 0.0, 6_000.0, 12_000.0, 18_000.0,
)
AFC_TRACK_HALF_WIDTH_HZ = 3_000.0
AFC_SMOOTHING = 0.35
AFC_MIN_UPDATE_HZ = 25.0
AFC_CONFIRMATIONS = 3
AFC_CONFIRM_TOLERANCE_HZ = 250.0
AFC_TRACK_JUMP_HZ = 500.0
PDU_DEDUP_TTL_S = 0.5

FRAME_ZMQ_ADDR = "tcp://127.0.0.1:52001"
DOPPLER_ZMQ_ADDR = "tcp://127.0.0.1:52003"


class _BeaconAfcSink(gr.sync_block):
    """Estimate the two-FSK midpoint and steer a translating FIR filter."""

    def __init__(self, channelizer, sample_rate, initial_bias_hz, search_hz):
        gr.sync_block.__init__(
            self,
            name="astrocast_1k2_afc",
            in_sig=[np.complex64],
            out_sig=None,
        )
        self._channelizer = channelizer
        self._sample_rate = float(sample_rate)
        self._initial_bias_hz = float(initial_bias_hz)
        self._search_hz = abs(float(search_hz))
        self._averager = AveragedFftPower(fft_size=8192, averages=6)
        self._correction_hz = self._initial_bias_hz
        self._locked = False
        self._pending_hz = None
        self._pending_count = 0
        self._state_lock = threading.Lock()

    @property
    def correction_hz(self):
        with self._state_lock:
            return self._correction_hz

    @property
    def locked(self):
        with self._state_lock:
            return self._locked

    def _apply_estimate(self, power):
        with self._state_lock:
            current = self._correction_hz
            locked = self._locked
        estimate = estimate_fsk_center(
            power,
            self._sample_rate,
            search_center_hz=current if locked else self._initial_bias_hz,
            search_half_width_hz=(
                AFC_TRACK_HALF_WIDTH_HZ if locked else self._search_hz
            ),
        )
        # Connecting Doppler after radio start can move the residual carrier
        # well outside the narrow tracking window. Search broadly again, but
        # still require consecutive confirmations before replacing the lock.
        if estimate is None and locked:
            estimate = estimate_fsk_center(
                power,
                self._sample_rate,
                search_center_hz=self._initial_bias_hz,
                search_half_width_hz=self._search_hz,
            )
        if estimate is None:
            with self._state_lock:
                self._pending_hz = None
                self._pending_count = 0
            return

        if locked and abs(estimate.center_hz - current) <= AFC_TRACK_JUMP_HZ:
            with self._state_lock:
                self._pending_hz = None
                self._pending_count = 0
            target = (
                (1.0 - AFC_SMOOTHING) * current
                + AFC_SMOOTHING * estimate.center_hz
            )
            if abs(target - current) < AFC_MIN_UPDATE_HZ:
                return
            action = "update"
        else:
            with self._state_lock:
                if (
                    self._pending_hz is None
                    or abs(estimate.center_hz - self._pending_hz)
                    > AFC_CONFIRM_TOLERANCE_HZ
                ):
                    self._pending_hz = estimate.center_hz
                    self._pending_count = 1
                else:
                    count = self._pending_count
                    self._pending_hz = (
                        self._pending_hz * count + estimate.center_hz
                    ) / (count + 1)
                    self._pending_count = count + 1
                if self._pending_count < AFC_CONFIRMATIONS:
                    return
                target = float(self._pending_hz)
                self._pending_hz = None
                self._pending_count = 0
            action = "relock" if locked else "lock"

        self._channelizer.set_center_freq(float(target))
        with self._state_lock:
            self._correction_hz = float(target)
            self._locked = True
        print(
            f"MAV_ASTROCAST AFC {action} {target/1e3:+.3f} kHz "
            f"(two-tone SNR {estimate.tone_snr_db:.1f} dB)",
            flush=True,
        )

    def work(self, input_items, output_items):
        samples = input_items[0]
        for power in self._averager.feed(samples):
            self._apply_estimate(power)
        return len(samples)


class _PduDeduplicator(gr.basic_block):
    """Merge parallel decoder outputs without publishing the same frame twice."""

    def __init__(self, ttl_s=PDU_DEDUP_TTL_S):
        gr.basic_block.__init__(self, name="astrocast_pdu_deduplicator",
                                in_sig=None, out_sig=None)
        self._ttl_s = float(ttl_s)
        self._seen = {}
        self._lock = threading.Lock()
        self.message_port_register_in(pmt.intern("in"))
        self.message_port_register_out(pmt.intern("out"))
        self.set_msg_handler(pmt.intern("in"), self._handle)

    def _accept_payload(self, payload, *, now=None):
        """Return True once per payload within the short parallel-path window."""
        timestamp = time.monotonic() if now is None else float(now)
        key = bytes(payload)
        with self._lock:
            cutoff = timestamp - self._ttl_s
            self._seen = {
                candidate: seen_at
                for candidate, seen_at in self._seen.items()
                if seen_at > cutoff
            }
            previous = self._seen.get(key)
            if previous is not None and timestamp - previous < self._ttl_s:
                return False
            self._seen[key] = timestamp
            return True

    @staticmethod
    def _payload_bytes(msg):
        if not pmt.is_pair(msg):
            raise ValueError("PDU is not a metadata/payload pair")
        payload = pmt.cdr(msg)
        if not pmt.is_u8vector(payload):
            raise ValueError("PDU payload is not a u8vector")
        return bytes(pmt.u8vector_elements(payload))

    def _handle(self, msg):
        try:
            if not self._accept_payload(self._payload_bytes(msg)):
                return
        except Exception:
            # Unknown message shapes should remain observable rather than being
            # lost because a diagnostics helper could not construct a key.
            pass
        self.message_port_pub(pmt.intern("out"), msg)


def _build_core(tb, wavfile, zmq_addr, doppler_addr, *, afc_enabled=True,
                afc_search_hz=DEFAULT_AFC_SEARCH_HZ,
                afc_bias_hz=DEFAULT_AFC_BIAS_HZ):
    """Construct the shared DSP chain (sources, decoder, ZMQ/hexdump sinks)
    on `tb`. GUI-agnostic: both the Qt and headless top blocks call this."""
    tb.zeromq_pub_msg_sink_0 = zeromq.pub_msg_sink(zmq_addr, 100, True)
    tb.satellites_hexdump_sink_0 = satellites.components.datasinks.hexdump_sink(options="")
    tb.beacon_output_taggers = []
    frame_sources = []

    def add_decoder_output(decoder, path, bin_hz=None):
        meta = pmt.make_dict()
        meta = pmt.dict_add(meta, pmt.intern("rx_path"), pmt.intern(path))
        if bin_hz is not None:
            meta = pmt.dict_add(
                meta, pmt.intern("rx_bin_hz"), pmt.from_double(float(bin_hz)))
        tagger = satellites.pdu_add_meta(meta)
        tb.beacon_output_taggers.append(tagger)
        tb.msg_connect((decoder, "out"), (tagger, "in"))
        frame_sources.append(tagger)

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
        add_decoder_output(tb.satellites_satellite_decoder_0, "wav_replay")
    else:
        from gnuradio import uhd

        tb.rx_freq = float(os.environ.get("GSS_RX_FREQ_HZ", DEFAULT_RX_FREQ_HZ))
        tb.rx_lo_offset = float(os.environ.get("GSS_RX_LO_OFFSET_HZ", DEFAULT_RX_LO_OFFSET_HZ))
        # Log the tuning intent BEFORE touching the USRP so the Radio logs
        # show the target frequency even if the device open fails.
        print(f"MAV_ASTROCAST RX {tb.rx_freq/1e6:.6f} MHz "
              f"(LO parked {tb.rx_lo_offset/1e3:+.0f} kHz), "
              f"{SAMP_RATE//RX_DECIM} sps acquisition channel", flush=True)
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

        tb.rx_lpf = gr_filter.fir_filter_ccf(
            RX_DECIM, firdes.low_pass(1, SAMP_RATE, 50e3, 10e3))
        acquisition_rate = SAMP_RATE // RX_DECIM
        decoder_rate = acquisition_rate // BEACON_CHANNEL_DECIM
        tb.beacon_channelizer = gr_filter.freq_xlating_fir_filter_ccf(
            BEACON_CHANNEL_DECIM,
            firdes.low_pass(
                1,
                acquisition_rate,
                BEACON_CHANNEL_CUTOFF_HZ,
                BEACON_CHANNEL_TRANSITION_HZ,
            ),
            float(afc_bias_hz),
            acquisition_rate,
        )
        tb.satellites_satellite_decoder_0 = satellites.core.gr_satellites_flowgraph(
            file=DECODER_YML, samp_rate=decoder_rate, iq=True,
            grc_block=True, options=DECODER_OPTIONS)
        tb.zeromq_sub_msg_source_rxcmd = zeromq.sub_msg_source(
            doppler_addr, 100, False)

        tb.connect((tb.uhd_usrp_source_0, 0), (tb.rx_lpf, 0))
        tb.connect(
            (tb.rx_lpf, 0),
            (tb.beacon_channelizer, 0),
            (tb.satellites_satellite_decoder_0, 0),
        )
        if afc_enabled:
            tb.beacon_afc = _BeaconAfcSink(
                tb.beacon_channelizer,
                acquisition_rate,
                initial_bias_hz=afc_bias_hz,
                search_hz=afc_search_hz,
            )
            tb.connect((tb.rx_lpf, 0), (tb.beacon_afc, 0))
            print(
                f"MAV_ASTROCAST 1k2 AFC search "
                f"{float(afc_bias_hz)/1e3:+.1f} +/- {abs(float(afc_search_hz))/1e3:.1f} kHz",
                flush=True,
            )
        else:
            print(
                f"MAV_ASTROCAST 1k2 AFC disabled; fixed correction "
                f"{float(afc_bias_hz)/1e3:+.3f} kHz",
                flush=True,
            )
        add_decoder_output(
            tb.satellites_satellite_decoder_0, "narrow_afc")

        # Immediate first-frame coverage. Each fixed branch sees at most
        # +/-3 kHz residual carrier error, then quadrature-demodulates before
        # gr-satellites so its IQ-only +/-1.8 kHz filter is bypassed. The
        # narrow AFC path above remains in parallel for very weak signals.
        bin_taps = firdes.low_pass(
            1,
            acquisition_rate,
            BEACON_CHANNEL_CUTOFF_HZ,
            BEACON_CHANNEL_TRANSITION_HZ,
        )
        tb.beacon_bin_channelizers = []
        tb.beacon_bin_demodulators = []
        tb.beacon_bin_decoders = []
        for bin_hz in BEACON_BIN_CENTERS_HZ:
            channelizer = gr_filter.freq_xlating_fir_filter_ccf(
                BEACON_CHANNEL_DECIM,
                bin_taps,
                bin_hz,
                acquisition_rate,
            )
            demodulator = analog.quadrature_demod_cf(
                decoder_rate / (2.0 * np.pi * BEACON_DEVIATION_HZ))
            decoder = satellites.core.gr_satellites_flowgraph(
                file=DECODER_YML,
                samp_rate=decoder_rate,
                iq=False,
                grc_block=True,
                options=DECODER_OPTIONS,
            )
            tb.beacon_bin_channelizers.append(channelizer)
            tb.beacon_bin_demodulators.append(demodulator)
            tb.beacon_bin_decoders.append(decoder)
            tb.connect(
                (tb.rx_lpf, 0),
                (channelizer, 0),
                (demodulator, 0),
                (decoder, 0),
            )
            add_decoder_output(decoder, "wide_bin", bin_hz)
        print(
            "MAV_ASTROCAST 1k2 immediate bins "
            + ", ".join(f"{value/1e3:+.0f}" for value in BEACON_BIN_CENTERS_HZ)
            + " kHz",
            flush=True,
        )
        tb.msg_connect(
            (tb.zeromq_sub_msg_source_rxcmd, 'out'),
            (tb.uhd_usrp_source_0, 'command'))

    tb.pdu_deduplicator = _PduDeduplicator()
    for source in frame_sources:
        tb.msg_connect((source, "out"), (tb.pdu_deduplicator, "in"))
    tb.msg_connect(
        (tb.pdu_deduplicator, "out"),
        (tb.zeromq_pub_msg_sink_0, "in"))
    tb.msg_connect(
        (tb.pdu_deduplicator, "out"),
        (tb.satellites_hexdump_sink_0, "in"))


class mav_astrocast_headless(gr.top_block):

    def __init__(self, wavfile=None, zmq_addr=FRAME_ZMQ_ADDR,
                 doppler_addr=DOPPLER_ZMQ_ADDR, *, afc_enabled=True,
                 afc_search_hz=DEFAULT_AFC_SEARCH_HZ,
                 afc_bias_hz=DEFAULT_AFC_BIAS_HZ):
        gr.top_block.__init__(self, "MAV_ASTROCAST", catch_exceptions=True)
        _build_core(
            self,
            wavfile,
            zmq_addr,
            doppler_addr,
            afc_enabled=afc_enabled,
            afc_search_hz=afc_search_hz,
            afc_bias_hz=afc_bias_hz,
        )


def _make_qt_class():
    """Import Qt lazily so --headless never touches PyQt5/qtgui."""
    from PyQt5 import Qt, QtCore
    from gnuradio import qtgui
    from gnuradio.fft import window
    import sip

    class mav_astrocast(gr.top_block, Qt.QWidget):

        def __init__(self, wavfile=None, zmq_addr=FRAME_ZMQ_ADDR,
                     doppler_addr=DOPPLER_ZMQ_ADDR, *, afc_enabled=True,
                     afc_search_hz=DEFAULT_AFC_SEARCH_HZ,
                     afc_bias_hz=DEFAULT_AFC_BIAS_HZ):
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

            _build_core(
                self,
                wavfile,
                zmq_addr,
                doppler_addr,
                afc_enabled=afc_enabled,
                afc_search_hz=afc_search_hz,
                afc_bias_hz=afc_bias_hz,
            )
            self.wavfile = wavfile

            if wavfile:
                spectrum_fc = 0
                spectrum_bw = WAV_SAMP_RATE
                self.qtgui_freq_sink_x_0 = qtgui.freq_sink_f(
                    2048, window.WIN_BLACKMAN_hARRIS, spectrum_fc, spectrum_bw,
                    "", 1, None)
                spectrum_tap = self.blocks_wav_throttle
            else:
                spectrum_fc = self.rx_freq
                spectrum_bw = SAMP_RATE // RX_DECIM
                self.qtgui_freq_sink_x_0 = qtgui.freq_sink_c(
                    2048, window.WIN_BLACKMAN_hARRIS, spectrum_fc, spectrum_bw,
                    "", 1, None)
                spectrum_tap = self.rx_lpf

            self.qtgui_freq_sink_x_0.set_update_time(0.05)
            self.qtgui_freq_sink_x_0.set_y_axis((-100), 0)
            self.qtgui_freq_sink_x_0.set_y_label('RX Spectrum', 'dB')
            self.qtgui_freq_sink_x_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
            self.qtgui_freq_sink_x_0.enable_autoscale(True)
            self.qtgui_freq_sink_x_0.enable_grid(True)
            self.qtgui_freq_sink_x_0.set_fft_average(0.2)
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
                    self._rx_actual_freq_label.setText(f"{float(val)/1e6:.6f} MHz")
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
                                doppler_addr=args.doppler_addr,
                                afc_enabled=not args.disable_afc,
                                afc_search_hz=args.afc_search_hz,
                                afc_bias_hz=args.afc_bias_hz)

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
                       doppler_addr=args.doppler_addr,
                       afc_enabled=not args.disable_afc,
                       afc_search_hz=args.afc_search_hz,
                       afc_bias_hz=args.afc_bias_hz)
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
    parser.add_argument("--afc-search-hz", type=float, default=DEFAULT_AFC_SEARCH_HZ,
                        help="1k2 AFC half-width around the initial bias")
    parser.add_argument("--afc-bias-hz", type=float, default=DEFAULT_AFC_BIAS_HZ,
                        help="initial/fixed 1k2 baseband frequency correction")
    parser.add_argument("--disable-afc", action="store_true",
                        help="disable automatic acquisition; use --afc-bias-hz only")
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
