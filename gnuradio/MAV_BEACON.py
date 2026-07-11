#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: MAVERIC PUBLIC BEACON DECODER
# Author: Irfan Annuar - USC ISI SERC
# Copyright: USC ISI SERC
# Description: RX-only decoder for the MAVERIC 9k6 FSK AX100 ASM+Golay downlink. Writes one timestamped hex line per decoded frame to log_path.
# GNU Radio version: 3.10.12.0

from gnuradio import gr
from gnuradio.filter import firdes
from gnuradio.fft import window
import sys
import signal
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
from gnuradio import soapy
import MAV_BEACON_epy_block_0 as epy_block_0  # embedded python block
import gpredict
import satellites.core
import threading


def snipfcn_chdir_to_script_dir(self):
    # Run from any directory: resolve MAVERIC_BEACON.yml and the hex log
    # relative to this script, not the operator's shell working directory.
    import os
    os.chdir(os.path.dirname(os.path.abspath(__file__)))


def snippets_init_before_blocks(tb):
    snipfcn_chdir_to_script_dir(tb)


class MAV_BEACON(gr.top_block):

    def __init__(self):
        gr.top_block.__init__(self, "MAVERIC PUBLIC BEACON DECODER", catch_exceptions=True)
        self.flowgraph_started = threading.Event()

        ##################################################
        # Variables
        ##################################################
        self.samp_rate = samp_rate = 250000
        self.log_path = log_path = "maveric_beacon.log"
        self.iq_file = iq_file = ""
        self.gain = gain = 40
        self.freq_offset = freq_offset = 0
        self.freq_corr = freq_corr = 0
        self.freq = freq = 437.575e6

        ##################################################
        # Blocks
        ##################################################
        snippets_init_before_blocks(self)
        self.soapy_rtlsdr_source_0 = None
        dev = 'driver=rtlsdr'
        stream_args = 'bufflen=16384'
        tune_args = ['']
        settings = ['']

        def _set_soapy_rtlsdr_source_0_gain_mode(channel, agc):
            self.soapy_rtlsdr_source_0.set_gain_mode(channel, agc)
            if not agc:
                  self.soapy_rtlsdr_source_0.set_gain(channel, self._soapy_rtlsdr_source_0_gain_value)
        self.set_soapy_rtlsdr_source_0_gain_mode = _set_soapy_rtlsdr_source_0_gain_mode

        def _set_soapy_rtlsdr_source_0_gain(channel, name, gain):
            self._soapy_rtlsdr_source_0_gain_value = gain
            if not self.soapy_rtlsdr_source_0.get_gain_mode(channel):
                self.soapy_rtlsdr_source_0.set_gain(channel, gain)
        self.set_soapy_rtlsdr_source_0_gain = _set_soapy_rtlsdr_source_0_gain

        def _set_soapy_rtlsdr_source_0_bias(bias):
            if 'biastee' in self._soapy_rtlsdr_source_0_setting_keys:
                self.soapy_rtlsdr_source_0.write_setting('biastee', bias)
        self.set_soapy_rtlsdr_source_0_bias = _set_soapy_rtlsdr_source_0_bias

        self.soapy_rtlsdr_source_0 = soapy.source(dev, "fc32", 1, '',
                                  stream_args, tune_args, settings)

        self._soapy_rtlsdr_source_0_setting_keys = [a.key for a in self.soapy_rtlsdr_source_0.get_setting_info()]

        self.soapy_rtlsdr_source_0.set_sample_rate(0, samp_rate)
        self.soapy_rtlsdr_source_0.set_frequency(0, (freq + freq_offset))
        self.soapy_rtlsdr_source_0.set_frequency_correction(0, freq_corr)
        self.set_soapy_rtlsdr_source_0_bias(bool(False))
        self._soapy_rtlsdr_source_0_gain_value = gain
        self.set_soapy_rtlsdr_source_0_gain_mode(0, bool(False))
        self.set_soapy_rtlsdr_source_0_gain(0, 'TUNER', gain)
        self.satellites_satellite_decoder_0 = satellites.core.gr_satellites_flowgraph(file = 'MAVERIC_BEACON.yml', samp_rate = samp_rate, grc_block = True, iq = True, options = "")
        self.gpredict_doppler_0 = gpredict.doppler('127.0.0.1', 7356, False)
        self.gpredict_MsgPairToVar_0 = gpredict.MsgPairToVar(self.set_freq)
        self.epy_block_0 = epy_block_0.blk(log_path=log_path)


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.gpredict_doppler_0, 'freq'), (self.gpredict_MsgPairToVar_0, 'inpair'))
        self.msg_connect((self.satellites_satellite_decoder_0, 'out'), (self.epy_block_0, 'pdu'))
        self.connect((self.soapy_rtlsdr_source_0, 0), (self.satellites_satellite_decoder_0, 0))


    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.soapy_rtlsdr_source_0.set_sample_rate(0, self.samp_rate)

    def get_log_path(self):
        return self.log_path

    def set_log_path(self, log_path):
        self.log_path = log_path
        self.epy_block_0.log_path = self.log_path

    def get_iq_file(self):
        return self.iq_file

    def set_iq_file(self, iq_file):
        self.iq_file = iq_file

    def get_gain(self):
        return self.gain

    def set_gain(self, gain):
        self.gain = gain
        self.set_soapy_rtlsdr_source_0_gain(0, 'TUNER', self.gain)

    def get_freq_offset(self):
        return self.freq_offset

    def set_freq_offset(self, freq_offset):
        self.freq_offset = freq_offset
        self.soapy_rtlsdr_source_0.set_frequency(0, (self.freq + self.freq_offset))

    def get_freq_corr(self):
        return self.freq_corr

    def set_freq_corr(self, freq_corr):
        self.freq_corr = freq_corr
        self.soapy_rtlsdr_source_0.set_frequency_correction(0, self.freq_corr)

    def get_freq(self):
        return self.freq

    def set_freq(self, freq):
        self.freq = freq
        self.soapy_rtlsdr_source_0.set_frequency(0, (self.freq + self.freq_offset))




def main(top_block_cls=MAV_BEACON, options=None):
    tb = top_block_cls()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    tb.start()
    tb.flowgraph_started.set()

    tb.wait()


if __name__ == '__main__':
    main()
