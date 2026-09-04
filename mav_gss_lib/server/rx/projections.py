"""RX projection side effects derived from decoded packet records."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, MutableMapping

from mav_gss_lib.platform.log_records import parameter_records, rx_packet_record
from mav_gss_lib.platform.rx.events import collect_packet_events
from mav_gss_lib.platform.rx.pipeline import RxResult
from mav_gss_lib.platform.tx.verifiers import write_instances

from .events import rx_packet_event

if TYPE_CHECKING:
    from .service import RxService


@dataclass(slots=True)
class RxProjectionResult:
    rx_event: dict[str, Any]
    extra_events: list[dict[str, Any]] = field(default_factory=list)
    verifier_instances: list[Any] = field(default_factory=list)


@dataclass(slots=True)
class RxProjectionDeps:
    runtime: Any
    last_arrival_ms: MutableMapping[str, int]
    crc_window: Any
    dup_window: Any
    get_rx_log: Callable[[], Any | None]
    get_tx_log: Callable[[], Any | None]


class RxProjectionRunner:
    """Apply decoded-packet projections through explicit server sinks."""

    def __init__(self, deps: RxProjectionDeps) -> None:
        self.deps = deps

    def project(self, result: RxResult, *, version: str) -> RxProjectionResult:
        """Apply all non-decode projections for one decoded packet."""

        runtime = self.deps.runtime
        pkt = result.packet
        event_id = pkt.event_id
        now_ms = pkt.received_at_ms

        runtime.parameter_cache.apply(pkt.parameters)

        extra_events: list[dict[str, Any]] = []
        if result.container_id:
            self.deps.last_arrival_ms[result.container_id] = now_ms
            expected_period_ms = _expected_period_ms(runtime, result.container_id)
            extra_events.append({
                "type": "parameters_freshness",
                "container": result.container_id,
                "last_ms": now_ms,
                "expected_period_ms": expected_period_ms,
            })

        flags = pkt.flags
        if flags.integrity_ok is False:
            self.deps.crc_window.append(now_ms)
        if flags.is_duplicate:
            self.deps.dup_window.append(now_ms)

        verifier_instances = _apply_verifier_matches(self.deps, pkt, now_ms, event_id)
        _write_rx_log(self.deps, pkt, version, event_id)

        extra_events.extend(collect_packet_events(runtime.mission, pkt))
        return RxProjectionResult(
            rx_event=rx_packet_event(pkt, event_id),
            extra_events=extra_events,
            verifier_instances=verifier_instances,
        )


def project_decoded_packet(
    service: "RxService",
    result: RxResult,
    *,
    version: str,
) -> RxProjectionResult:
    """Backward-compatible helper for tests/older call sites."""

    deps = RxProjectionDeps(
        runtime=service.runtime,
        last_arrival_ms=service.last_arrival_ms,
        crc_window=service.crc_window,
        dup_window=service.dup_window,
        get_rx_log=lambda: service.log,
        get_tx_log=lambda: service.runtime.tx.log,
    )
    return RxProjectionRunner(deps).project(result, version=version)


def _expected_period_ms(runtime: Any, container_id: str) -> int:
    spec_root = getattr(runtime.mission, "spec_root", None)
    if spec_root is None:
        return 0
    container = spec_root.sequence_containers.get(container_id)
    if container is None:
        return 0
    return int(getattr(container, "expected_period_ms", 0) or 0)


def _apply_verifier_matches(deps: RxProjectionDeps, pkt: Any, now_ms: int, event_id: str) -> list[Any]:
    runtime = deps.runtime
    if not runtime.tx_verifiers_enabled:
        return []
    try:
        transitions = runtime.mission.packets.match_verifiers(
            pkt,
            runtime.platform.verifiers.open_instances(),
            now_ms=now_ms,
            rx_event_id=event_id,
        )
    except Exception as exc:
        logging.warning("match_verifiers failed: %s", exc)
        transitions = []

    rx_seq = getattr(pkt, "seq", 0)
    for instance_id, verifier_id, outcome in transitions:
        inst = next(
            (i for i in runtime.platform.verifiers.open_instances()
             if i.instance_id == instance_id),
            None,
        )
        runtime.platform.verifiers.apply(instance_id, verifier_id, outcome)
        tx_log = deps.get_tx_log()
        if inst and tx_log:
            try:
                tx_log.write_cmd_verifier({
                    "seq": rx_seq,
                    "cmd_event_id": inst.cmd_event_id,
                    "instance_id": inst.instance_id,
                    "stage": inst.stage,
                    "verifier_id": verifier_id,
                    "outcome": outcome.state,
                    "elapsed_ms": (outcome.matched_at_ms or now_ms) - inst.t0_ms,
                    "match_event_id": outcome.match_event_id,
                })
            except Exception as exc:
                logging.warning("cmd_verifier log failed: %s", exc)

    runtime.platform.verifiers.sweep(now_ms=now_ms)
    dirty = runtime.platform.verifiers.consume_dirty()
    if not dirty:
        return []
    try:
        write_instances(
            Path(runtime.log_dir) / ".pending_instances.jsonl",
            runtime.platform.verifiers.open_instances(),
        )
    except Exception as exc:
        logging.warning("pending_instances write failed: %s", exc)
    return dirty


def _write_rx_log(deps: RxProjectionDeps, pkt: Any, version: str, event_id: str) -> None:
    runtime = deps.runtime
    try:
        log = deps.get_rx_log()
        if not log:
            return
        record = rx_packet_record(
            runtime.mission, pkt, version,
            session_id=log.session_id,
            event_id=event_id,
            mission_id=runtime.mission_id,
            operator=runtime.operator,
            station=runtime.station,
        )
        param_records = list(parameter_records(
            pkt,
            session_id=log.session_id,
            rx_event_id=record["event_id"],
            version=version,
            mission_id=runtime.mission_id,
            operator=runtime.operator,
            station=runtime.station,
        ))
        log.write_packet(record, pkt, parameter_records=param_records)
    except Exception as exc:
        logging.warning("RX log write failed: %s", exc)
    _log_rx_tracking_sample(deps)


def _log_rx_tracking_sample(deps: RxProjectionDeps) -> None:
    """Best-effort: log a tracking_sample tagged source="rx_decode" at the
    moment a downlink packet decodes, mirroring
    TxService._log_tx_tracking_sample so a downlink gets the same
    near-1-Hz-fresh capture a TX attempt already does, independent of the
    background tick's own logging cadence (which may be off). Reads the
    last-published tick rather than recomputing, so a decode never triggers
    an extra Doppler-sink publish as a side effect."""
    runtime = deps.runtime
    broadcaster = getattr(runtime, "doppler_broadcaster", None)
    latest = getattr(broadcaster, "latest", None) if broadcaster is not None else None
    doppler = latest.get("doppler") if isinstance(latest, dict) else None
    if not doppler:
        return
    log = deps.get_rx_log()
    if log is None or not hasattr(log, "write_tracking_sample"):
        return
    radio = getattr(runtime, "radio", None)
    actual = getattr(radio, "latest_tune_result", None) if radio is not None else None
    try:
        log.write_tracking_sample(doppler, source="rx_decode", actual=actual)
    except Exception as exc:
        logging.warning("RX tracking-sample log failed: %s", exc)


__all__ = [
    "RxProjectionDeps",
    "RxProjectionResult",
    "RxProjectionRunner",
    "project_decoded_packet",
]
