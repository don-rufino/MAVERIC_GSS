"""Runtime tracking service.

The service reads the live platform config, computes authoritative tracking
state, and publishes Doppler corrections through an intentionally small sink
interface. Engage/disengage swaps a real ZmqDopplerSink in place of the null
sink so GNU Radio integration can hook in without changing tracking math or UI
contracts.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Callable, Protocol

from mav_gss_lib.platform.tracking import (
    DopplerCorrection,
    TrackingError,
    build_satellite,
    normalize_tracking_config,
    pass_detail,
    to_plain,
    tracking_state,
    upcoming_passes,
)
from mav_gss_lib.config import get_tracking_control
from mav_gss_lib.platform.tracking.models import DopplerMode
from mav_gss_lib.platform.tracking.propagation import look_angles_at, doppler_correction

if TYPE_CHECKING:
    from mav_gss_lib.server.state import WebRuntime


class DopplerSink(Protocol):
    def publish(self, correction: DopplerCorrection) -> None: ...
    def close(self) -> None: ...


class NullDopplerSink:
    def publish(self, correction: DopplerCorrection) -> None:
        return None

    def close(self) -> None:
        return None


SinkFactory = Callable[..., DopplerSink]


def _default_sink_factory(
    *,
    rx_addr: str,
    tx_addr: str,
    rx_lo_offset_hz: float = 0.0,
    tx_lo_offset_hz: float = 0.0,
) -> DopplerSink:
    # Lazy import keeps pyzmq + pmt out of the import chain when tracking is
    # imported in a context that never engages (e.g., unit tests).
    from mav_gss_lib.server.tracking.sink_zmq import ZmqDopplerSink
    return ZmqDopplerSink(
        rx_addr=rx_addr,
        tx_addr=tx_addr,
        rx_lo_offset_hz=rx_lo_offset_hz,
        tx_lo_offset_hz=tx_lo_offset_hz,
    )


class TrackingService:
    def __init__(
        self,
        runtime: "WebRuntime",
        sink: DopplerSink | None = None,
        *,
        sink_factory: SinkFactory | None = None,
    ) -> None:
        self.runtime = runtime
        self._sink: DopplerSink = sink or NullDopplerSink()
        self._doppler_mode: DopplerMode = "disconnected"
        self._sink_lock = threading.Lock()
        self._sink_factory: SinkFactory = sink_factory or _default_sink_factory
        self._last_error: str = ""
        self._last_tick_ms: int = 0

    @property
    def doppler_mode(self) -> DopplerMode:
        return self._doppler_mode

    @property
    def last_error(self) -> str:
        return self._last_error

    def set_doppler_connected(self, connected: bool) -> DopplerMode:
        return self.engage() if connected else self.disengage()

    def engage(self) -> DopplerMode:
        # Idempotent: re-engaging while already connected is a no-op.
        # A second bind() on the same port would EADDRINUSE and tear down
        # the working engagement.
        with self._sink_lock:
            if self._doppler_mode == "connected":
                return self._doppler_mode
            prev_mode = self._doppler_mode
        control = self._control_config()
        sink = self._sink_factory(
            rx_addr=control["rx_zmq_addr"],
            tx_addr=control["tx_zmq_addr"],
            rx_lo_offset_hz=control["rx_lo_offset_hz"],
            tx_lo_offset_hz=control["tx_lo_offset_hz"],
        )
        with self._sink_lock:
            self._sink = sink
            self._doppler_mode = "connected"
            self._last_error = ""
        self._write_tracking_event(
            "connect",
            mode=self._doppler_mode,
            prev_mode=prev_mode,
            rx_zmq_addr=control["rx_zmq_addr"],
            tx_zmq_addr=control["tx_zmq_addr"],
        )
        return self._doppler_mode

    def disengage(self) -> DopplerMode:
        previous: DopplerSink | None = None
        prev_mode: DopplerMode = "disconnected"
        with self._sink_lock:
            if self._doppler_mode == "disconnected":
                return self._doppler_mode
            prev_mode = self._doppler_mode
            previous = self._sink
            self._sink = NullDopplerSink()
            self._doppler_mode = "disconnected"
        if previous is not None:
            try:
                previous.publish(self._nominal_park_correction())
            finally:
                previous.close()
        self._write_tracking_event(
            "disconnect",
            mode=self._doppler_mode,
            prev_mode=prev_mode,
        )
        return self._doppler_mode

    def _nominal_park_correction(self) -> DopplerCorrection:
        # Built from the live tracking config so disengage parks the USRP at
        # the operator-configured nominal frequencies, not the last
        # Doppler-shifted tune. Mode is "disconnected" so subscribers can tell
        # this apart from a live correction.
        config = self.config_model()
        return DopplerCorrection(
            ts_ms=_now_ms(),
            station_id=config.selected_station.id,
            satellite=config.tle.name,
            mode="disconnected",
            range_rate_mps=0.0,
            rx_hz=config.frequencies.rx_hz,
            rx_shift_hz=0.0,
            rx_tune_hz=config.frequencies.rx_hz,
            tx_hz=config.frequencies.tx_hz,
            tx_shift_hz=0.0,
            tx_tune_hz=config.frequencies.tx_hz,
        )

    def _write_tracking_event(
        self,
        action: str,
        *,
        mode: str = "",
        prev_mode: str = "",
        rx_zmq_addr: str = "",
        tx_zmq_addr: str = "",
        detail: str = "",
    ) -> None:
        """Append one tracking lifecycle event to the unified session log.

        Best-effort: a missing or unwritable session log must not abort the
        engage/disengage operation. The selected station id is captured from
        live config so post-pass review can correlate the engagement with the
        ground site that was active at the time.
        """
        log = getattr(getattr(self.runtime, "rx", None), "log", None)
        if log is None:
            log = getattr(getattr(self.runtime, "tx", None), "log", None)
        if log is None or not hasattr(log, "write_tracking_event"):
            return
        station_id = ""
        try:
            station_id = str(self.config_model().selected_station.id)
        except Exception:
            station_id = ""
        try:
            log.write_tracking_event(
                action,
                mode=mode,
                prev_mode=prev_mode,
                station_id=station_id,
                rx_zmq_addr=rx_zmq_addr,
                tx_zmq_addr=tx_zmq_addr,
                detail=detail,
            )
        except Exception:
            logging.exception("tracking lifecycle log failed")

    def _control_config(self) -> dict:
        with self.runtime.cfg_lock:
            return get_tracking_control(self.runtime.platform_cfg or {})

    def config_model(self):
        with self.runtime.cfg_lock:
            raw = copy.deepcopy((self.runtime.platform_cfg or {}).get("tracking"))
        return normalize_tracking_config(raw)

    def config(self) -> dict:
        return to_plain(self.config_model())

    def state(self, *, time_ms: int | None = None, pass_count: int = 10) -> dict:
        ts_ms = _now_ms() if time_ms is None else int(time_ms)
        config = self.config_model()
        state = tracking_state(
            config,
            time_ms=ts_ms,
            doppler_mode=self._doppler_mode,
            pass_count=pass_count,
        )
        if self._doppler_mode == "connected":
            self._publish(state.doppler)
        return to_plain(state)

    def passes(self, *, from_ms: int | None = None, count: int = 10) -> dict:
        ts_ms = _now_ms() if from_ms is None else int(from_ms)
        config = self.config_model()
        satellite = build_satellite(config)
        passes = upcoming_passes(satellite, config.selected_station, ts_ms, count=count)
        return {
            "ts_ms": ts_ms,
            "station_id": config.selected_station.id,
            "satellite": config.tle.name,
            "passes": [asdict(item) for item in passes],
        }

    def pass_by_id(self, pass_id: str, *, from_ms: int | None = None) -> dict | None:
        ts_ms = _now_ms() if from_ms is None else int(from_ms)
        config = self.config_model()
        satellite = build_satellite(config)
        passes = upcoming_passes(satellite, config.selected_station, ts_ms, count=20)
        match = next((item for item in passes if item.id == pass_id), None)
        if match is None:
            return None
        return to_plain(pass_detail(satellite, config.selected_station, match))

    def doppler(self, *, time_ms: int | None = None) -> dict:
        ts_ms = _now_ms() if time_ms is None else int(time_ms)
        config = self.config_model()
        satellite = build_satellite(config)
        look = look_angles_at(satellite, config.selected_station, ts_ms)
        correction = doppler_correction(
            time_ms=ts_ms,
            station=config.selected_station,
            satellite_name=config.tle.name,
            mode=self._doppler_mode,
            look=look,
            rx_hz=config.frequencies.rx_hz,
            tx_hz=config.frequencies.tx_hz,
        )
        if self._doppler_mode == "connected":
            self._publish(correction)
        self._last_tick_ms = ts_ms
        return asdict(correction)

    def status(self) -> dict:
        return {
            "mode": self._doppler_mode,
            "last_error": self._last_error,
            "last_tick_ms": self._last_tick_ms,
        }

    def _publish(self, correction: DopplerCorrection) -> None:
        with self._sink_lock:
            sink = self._sink
        sink.publish(correction)


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = ["DopplerSink", "NullDopplerSink", "TrackingService"]
