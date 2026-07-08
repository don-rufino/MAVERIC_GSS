import time
import unittest

import pmt
import zmq

from mav_gss_lib.platform.tracking.models import DopplerCorrection
from mav_gss_lib.server.tracking.sink_zmq import ZmqDopplerSink


def _correction(rx_tune: float, tx_tune: float) -> DopplerCorrection:
    return DopplerCorrection(
        ts_ms=1_700_000_000_000,
        station_id="usc",
        satellite="MAVERIC",
        mode="connected",
        range_rate_mps=-1234.5,
        rx_hz=437_575_000.0,
        rx_shift_hz=-200.0,
        rx_tune_hz=rx_tune,
        tx_hz=437_575_000.0,
        tx_shift_hz=210.0,
        tx_tune_hz=tx_tune,
    )


class ZmqDopplerSinkTests(unittest.TestCase):
    def test_publishes_fixed_lo_manual_tune_to_each_port(self) -> None:
        ctx = zmq.Context.instance()
        rx_sub = ctx.socket(zmq.SUB)
        tx_sub = ctx.socket(zmq.SUB)
        rx_sub.setsockopt(zmq.SUBSCRIBE, b"")
        tx_sub.setsockopt(zmq.SUBSCRIBE, b"")

        sink = ZmqDopplerSink(
            rx_addr="tcp://127.0.0.1:0",
            tx_addr="tcp://127.0.0.1:0",
            rx_lo_offset_hz=250_000.0,
            tx_lo_offset_hz=-400_000.0,
        )
        try:
            rx_sub.connect(sink.rx_endpoint)
            tx_sub.connect(sink.tx_endpoint)
            time.sleep(0.2)  # PUB/SUB slow joiner

            sink.publish(_correction(rx_tune=437_599_800.0, tx_tune=437_600_210.0))

            rx_sub.RCVTIMEO = 1500
            tx_sub.RCVTIMEO = 1500
            rx_msg = pmt.deserialize_str(rx_sub.recv())
            tx_msg = pmt.deserialize_str(tx_sub.recv())

            def field(msg, key):
                return pmt.to_double(pmt.dict_ref(msg, pmt.intern(key), pmt.PMT_NIL))

            # LO parked at nominal + offset, independent of the doppler tune.
            self.assertAlmostEqual(field(rx_msg, "lo_freq"), 437_825_000.0)
            self.assertAlmostEqual(field(tx_msg, "lo_freq"), 437_175_000.0)
            # DSP shift carries all of the doppler. UHD manual-policy NCO
            # conventions: RX center = lo - dsp, TX center = lo + dsp.
            self.assertAlmostEqual(field(rx_msg, "dsp_freq"), 225_200.0)
            self.assertAlmostEqual(field(tx_msg, "dsp_freq"), 425_210.0)
            self.assertAlmostEqual(
                field(rx_msg, "lo_freq") - field(rx_msg, "dsp_freq"), 437_599_800.0
            )
            self.assertAlmostEqual(
                field(tx_msg, "lo_freq") + field(tx_msg, "dsp_freq"), 437_600_210.0
            )
            # Bare "freq" must be absent: it would override the manual policy.
            self.assertTrue(
                pmt.equal(
                    pmt.dict_ref(rx_msg, pmt.intern("freq"), pmt.PMT_NIL), pmt.PMT_NIL
                )
            )
        finally:
            sink.close()
            rx_sub.close()
            tx_sub.close()

    def test_close_is_idempotent(self) -> None:
        sink = ZmqDopplerSink(
            rx_addr="tcp://127.0.0.1:0",
            tx_addr="tcp://127.0.0.1:0",
        )
        sink.close()
        sink.close()  # must not raise

    def test_publish_after_close_is_noop(self) -> None:
        sink = ZmqDopplerSink(
            rx_addr="tcp://127.0.0.1:0",
            tx_addr="tcp://127.0.0.1:0",
        )
        sink.close()
        sink.publish(_correction(rx_tune=1.0, tx_tune=2.0))  # must not raise


if __name__ == "__main__":
    unittest.main()
