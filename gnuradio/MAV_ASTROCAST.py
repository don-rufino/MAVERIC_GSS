#!/usr/bin/env python3
"""MAV_ASTROCAST — RX-only GNU Radio decoder for Astrocast 0.1 (NORAD 43798).

Headless top block supervised by the GSS RadioService (set
`platform.radio.script: gnuradio/MAV_ASTROCAST.py`). Decodes all three
downlink modes on 437.150 MHz via gr-satellites (ASTROCAST_DECODER.yml):
1k2 FSK FX.25 beacons (NRZ-I + NRZ failsafe) and 9k6 FSK CCSDS-RS
telemetry downloads. Deframed PDUs publish on the GSS RX frame bus.

Input modes:
  default          USRP B210 (same subdev/antenna/gain conventions as
                   MAV_DUO), parked-LO tuning from GSS_RX_FREQ_HZ /
                   GSS_RX_LO_OFFSET_HZ (RadioService injects both), and
                   Doppler tune messages consumed on tcp://127.0.0.1:52003
                   into the UHD command port.
  --wavfile PATH   Offline replay of a 48 kHz mono FM-demodulated wav
                   recording (e.g. satellite-recordings/astrocast.wav);
                   decodes faster than real time, then exits.
"""

import argparse
import os
import signal
import sys
import time

from gnuradio import blocks, gr, zeromq
from gnuradio import filter as gr_filter
from gnuradio.filter import firdes

import satellites.components.datasinks
import satellites.core


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DECODER_YML = os.path.join(SCRIPT_DIR, "ASTROCAST_DECODER.yml")

DEFAULT_RX_FREQ_HZ = 437.150e6
DEFAULT_RX_LO_OFFSET_HZ = 250e3
SAMP_RATE = 1_000_000
RX_DECIM = 5
RX_GAIN = 40
WAV_SAMP_RATE = 48_000

FRAME_ZMQ_ADDR = "tcp://127.0.0.1:52001"
DOPPLER_ZMQ_ADDR = "tcp://127.0.0.1:52003"


class mav_astrocast(gr.top_block):

    def __init__(self, wavfile=None, zmq_addr=FRAME_ZMQ_ADDR,
                 doppler_addr=DOPPLER_ZMQ_ADDR):
        gr.top_block.__init__(self, "MAV_ASTROCAST", catch_exceptions=True)

        self.zeromq_pub_msg_sink_0 = zeromq.pub_msg_sink(zmq_addr, 100, True)
        self.satellites_hexdump_sink_0 = satellites.components.datasinks.hexdump_sink(options="")

        if wavfile:
            self.blocks_wavfile_source_0 = blocks.wavfile_source(wavfile, False)
            self.satellites_satellite_decoder_0 = satellites.core.gr_satellites_flowgraph(
                file=DECODER_YML, samp_rate=WAV_SAMP_RATE, iq=False,
                grc_block=True, options="")
            self.connect(
                (self.blocks_wavfile_source_0, 0),
                (self.satellites_satellite_decoder_0, 0))
        else:
            from gnuradio import uhd

            rx_freq = float(os.environ.get("GSS_RX_FREQ_HZ", DEFAULT_RX_FREQ_HZ))
            rx_lo_offset = float(os.environ.get("GSS_RX_LO_OFFSET_HZ", DEFAULT_RX_LO_OFFSET_HZ))
            self.uhd_usrp_source_0 = uhd.usrp_source(
                ",".join(("", "")),
                uhd.stream_args(cpu_format="fc32", args='', channels=list(range(0, 1))),
            )
            self.uhd_usrp_source_0.set_subdev_spec('A:A', 0)
            self.uhd_usrp_source_0.set_samp_rate(SAMP_RATE)
            self.uhd_usrp_source_0.set_center_freq(
                uhd.tune_request(rx_freq, rx_lo_offset), 0)
            self.uhd_usrp_source_0.set_antenna("RX2", 0)
            self.uhd_usrp_source_0.set_gain(RX_GAIN, 0)
            print(f"MAV_ASTROCAST RX {rx_freq/1e6:.6f} MHz "
                  f"(LO parked {rx_lo_offset/1e3:+.0f} kHz), "
                  f"{SAMP_RATE//RX_DECIM} sps into decoder", flush=True)

            self.rx_lpf = gr_filter.fir_filter_ccf(
                RX_DECIM, firdes.low_pass(1, SAMP_RATE, 50e3, 10e3))
            self.satellites_satellite_decoder_0 = satellites.core.gr_satellites_flowgraph(
                file=DECODER_YML, samp_rate=SAMP_RATE // RX_DECIM, iq=True,
                grc_block=True, options="")
            self.zeromq_sub_msg_source_rxcmd = zeromq.sub_msg_source(
                doppler_addr, 100, False)

            self.connect((self.uhd_usrp_source_0, 0), (self.rx_lpf, 0))
            self.connect((self.rx_lpf, 0), (self.satellites_satellite_decoder_0, 0))
            self.msg_connect(
                (self.zeromq_sub_msg_source_rxcmd, 'out'),
                (self.uhd_usrp_source_0, 'command'))

        self.msg_connect(
            (self.satellites_satellite_decoder_0, 'out'),
            (self.zeromq_pub_msg_sink_0, 'in'))
        self.msg_connect(
            (self.satellites_satellite_decoder_0, 'out'),
            (self.satellites_hexdump_sink_0, 'in'))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wavfile", help="48 kHz mono wav replay instead of the USRP")
    parser.add_argument("--zmq-addr", default=FRAME_ZMQ_ADDR,
                        help=f"frame PDU PUB bind address [default {FRAME_ZMQ_ADDR}]")
    parser.add_argument("--doppler-addr", default=DOPPLER_ZMQ_ADDR,
                        help=f"Doppler tune SUB address [default {DOPPLER_ZMQ_ADDR}]")
    parser.add_argument("--wait-s", type=float, default=1.0,
                        help="wav mode: delay before decode so ZMQ subscribers can join")
    args = parser.parse_args()

    tb = mav_astrocast(wavfile=args.wavfile, zmq_addr=args.zmq_addr,
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


if __name__ == "__main__":
    main()
