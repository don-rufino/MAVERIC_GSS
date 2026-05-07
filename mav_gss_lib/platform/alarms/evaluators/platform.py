"""Platform-level alarm evaluator — silence / ZMQ / CRC / DUP.

Pure: takes a snapshot of inputs (the producer trims windows before
calling) and returns Verdicts. The producer (RxService) tracks the
underlying CRC/DUP event timestamps in deques.

Author:  Irfan Annuar - USC ISI SERC
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mav_gss_lib.platform.alarms.contract import AlarmSource, Severity
from mav_gss_lib.platform.alarms.registry import Verdict


SILENCE_WARNING_S = 1800
SILENCE_CRITICAL_S = 3600
CRC_WINDOW_MS = 60_000
CRC_WARNING_THRESHOLD = 3
CRC_CRITICAL_THRESHOLD = 10
DUP_WINDOW_MS = 60_000
DUP_WARNING_THRESHOLD = 5


@dataclass(frozen=True, slots=True)
class PlatformAlarmInputs:
    silence_s: float
    zmq_state: str  # "OK" | "RETRY" | "DOWN"
    rx_zmq_expected: bool = True
    crc_event_ms: tuple[int, ...] = field(default_factory=tuple)
    dup_event_ms: tuple[int, ...] = field(default_factory=tuple)
    radio_enabled: bool = False
    radio_autostart: bool = False
    radio_state: str = ""  # "running" | "stopping" | "stopped" | "crashed"


def evaluate_platform(inputs: PlatformAlarmInputs, now_ms: int) -> list[Verdict]:
    return [
        _silence(inputs.silence_s),
        _zmq(inputs.zmq_state, expected=inputs.rx_zmq_expected),
        _crc(inputs.crc_event_ms, now_ms),
        _dup(inputs.dup_event_ms, now_ms),
        _radio(inputs.radio_enabled, inputs.radio_autostart, inputs.radio_state),
    ]


def _silence(silence_s: float) -> Verdict:
    if silence_s >= SILENCE_CRITICAL_S:
        sev, detail = Severity.CRITICAL, f"no packet for {int(silence_s)}s"
    elif silence_s >= SILENCE_WARNING_S:
        sev, detail = Severity.WARNING, f"no packet for {int(silence_s)}s"
    else:
        sev, detail = None, ""
    return Verdict(id="platform.silence", source=AlarmSource.PLATFORM,
                   label="SILENCE", severity=sev, detail=detail)


def _zmq(state: str, *, expected: bool = True) -> Verdict:
    state = (state or "").upper()
    if not expected:
        sev, detail = None, ""
    elif state == "DOWN":
        sev, detail = Severity.CRITICAL, "ZMQ socket disconnected"
    elif state == "RETRY":
        sev, detail = Severity.WARNING, "ZMQ socket reconnecting"
    else:
        sev, detail = None, ""
    return Verdict(id="platform.zmq", source=AlarmSource.PLATFORM, label="ZMQ",
                   severity=sev, detail=detail)


def _crc(event_ms: tuple[int, ...], now_ms: int) -> Verdict:
    cutoff = now_ms - CRC_WINDOW_MS
    count = sum(1 for t in event_ms if t > cutoff)
    if count >= CRC_CRITICAL_THRESHOLD:
        sev = Severity.CRITICAL
    elif count >= CRC_WARNING_THRESHOLD:
        sev = Severity.WARNING
    else:
        sev = None
    return Verdict(id="platform.crc", source=AlarmSource.PLATFORM, label="CRC",
                   severity=sev, detail=f"{count} errors in 60s" if sev else "",
                   context={"count": count, "window_ms": CRC_WINDOW_MS})


def _dup(event_ms: tuple[int, ...], now_ms: int) -> Verdict:
    cutoff = now_ms - DUP_WINDOW_MS
    count = sum(1 for t in event_ms if t > cutoff)
    sev = Severity.WATCH if count >= DUP_WARNING_THRESHOLD else None
    return Verdict(id="platform.dup", source=AlarmSource.PLATFORM, label="DUP",
                   severity=sev, detail=f"{count} duplicates in 60s" if sev else "",
                   context={"count": count, "window_ms": DUP_WINDOW_MS})


def _radio(enabled: bool, autostart: bool, state: str) -> Verdict:
    state = (state or "").lower()
    if not enabled or state == "running":
        sev, detail = None, ""
    elif state == "crashed":
        sev, detail = Severity.CRITICAL, "GNU Radio process crashed"
    elif autostart and state == "stopping":
        sev, detail = Severity.WARNING, "GNU Radio process stopping"
    elif autostart:
        sev, detail = Severity.WARNING, "GNU Radio process stopped"
    else:
        sev, detail = None, ""
    return Verdict(
        id="platform.radio",
        source=AlarmSource.PLATFORM,
        label="RADIO",
        severity=sev,
        detail=detail,
        context={"state": state, "enabled": enabled, "autostart": autostart},
    )


__all__ = [
    "CRC_CRITICAL_THRESHOLD", "CRC_WARNING_THRESHOLD", "CRC_WINDOW_MS",
    "DUP_WARNING_THRESHOLD", "DUP_WINDOW_MS", "PlatformAlarmInputs",
    "SILENCE_CRITICAL_S", "SILENCE_WARNING_S", "evaluate_platform",
]
