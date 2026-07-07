"""ZMQ PUB sink delivering DopplerCorrection center frequencies to the GNU
Radio flowgraph. RX and TX are published on separate PUB sockets so each
maps cleanly to the matching uhd_usrp_{source,sink}.command port in MAV_DUO.

Corrections are sent as manual-policy tune commands (lo_freq + dsp_freq)
rather than bare freq retunes: the RF LO stays parked at nominal + lo_offset
for the whole engagement while only the DSP NCO tracks Doppler. Re-tuning
the AD9361 synthesizers once per second re-runs PLL lock + calibration and,
with the RX/TX LOs only kHz apart, the two synths cross-couple into beat
spurs and injection pulling inside the receive passband. The parked-LO
offsets also keep the TX LO feedthrough and the RX DC spike outside the
decoder band. lo_offset values must match MAV_DUO.py's initial tunes."""

from __future__ import annotations

import threading

import pmt
import zmq

from mav_gss_lib.platform.tracking.models import DopplerCorrection


class ZmqDopplerSink:
    def __init__(
        self,
        *,
        rx_addr: str,
        tx_addr: str,
        rx_lo_offset_hz: float = 0.0,
        tx_lo_offset_hz: float = 0.0,
    ) -> None:
        self._rx_lo_offset_hz = float(rx_lo_offset_hz)
        self._tx_lo_offset_hz = float(tx_lo_offset_hz)
        self._lock = threading.Lock()
        self._closed = False
        self._ctx = zmq.Context.instance()
        self._rx = self._ctx.socket(zmq.PUB)
        self._rx.setsockopt(zmq.LINGER, 0)
        self._rx.bind(rx_addr)
        try:
            self._tx = self._ctx.socket(zmq.PUB)
            self._tx.setsockopt(zmq.LINGER, 0)
            self._tx.bind(tx_addr)
        except Exception:
            self._rx.close(linger=0)
            raise

    @property
    def rx_endpoint(self) -> str:
        return self._rx.getsockopt(zmq.LAST_ENDPOINT).decode()

    @property
    def tx_endpoint(self) -> str:
        return self._tx.getsockopt(zmq.LAST_ENDPOINT).decode()

    def publish(self, correction: DopplerCorrection) -> None:
        rx_payload = _tune_message(
            lo_hz=correction.rx_hz + self._rx_lo_offset_hz,
            target_hz=correction.rx_tune_hz,
        )
        tx_payload = _tune_message(
            lo_hz=correction.tx_hz + self._tx_lo_offset_hz,
            target_hz=correction.tx_tune_hz,
        )
        with self._lock:
            if self._closed:
                return
            self._rx.send(rx_payload, flags=zmq.NOBLOCK)
            self._tx.send(tx_payload, flags=zmq.NOBLOCK)

    def close(self) -> None:
        # 250 ms LINGER lets the final park-at-nominal publish flush before the
        # PUB sockets shut down; live-tracking publishes already use NOBLOCK so
        # they cannot stall here.
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._rx.close(linger=250)
            finally:
                self._tx.close(linger=250)


def _tune_message(*, lo_hz: float, target_hz: float) -> bytes:
    # Manual-policy tune: gr-uhd combines lo_freq + dsp_freq from one dict
    # into a single tune_request with both policies MANUAL, so the RF
    # synthesizer holds lo_hz while the DSP NCO places target_hz at baseband
    # center. UHD's dsp_freq convention is (target - LO) for both RX and TX.
    # Explicit chan=0 so the command is unambiguous against any future
    # multi-channel build of MAV_DUO.
    msg = pmt.make_dict()
    msg = pmt.dict_add(msg, pmt.intern("lo_freq"), pmt.from_double(float(lo_hz)))
    msg = pmt.dict_add(
        msg, pmt.intern("dsp_freq"), pmt.from_double(float(target_hz) - float(lo_hz))
    )
    msg = pmt.dict_add(msg, pmt.intern("chan"), pmt.from_long(0))
    return pmt.serialize_str(msg)


__all__ = ["ZmqDopplerSink"]
