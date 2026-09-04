"""Unified JSONL record builders for RX, TX, parameter, and alarm events.

The writer classes are format-agnostic. Platform code builds event records
here, then ``mav_gss_lib.logging`` persists them to shared JSONL session files.
"""

from __future__ import annotations

import copy
from typing import Any, Iterator

from ._log_envelope import new_event_id, ts_iso
from .contract.mission import MissionSpec
from .contract.packets import PacketEnvelope


def _deep_merge(dst: dict, src: dict) -> dict:
    """Generic recursive merge: src wins; nested dicts merge instead of replace.

    Mutates dst and returns it. Caller is responsible for passing a
    deep-copied dst if they want input isolation.
    """
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def rx_packet_record(
    mission: MissionSpec,
    packet: PacketEnvelope,
    version: str,
    *,
    session_id: str,
    event_id: str | None = None,
    mission_id: str = "",
    operator: str = "",
    station: str = "",
) -> dict[str, Any]:
    """Build one inbound packet event record."""
    event_id = event_id or new_event_id()
    return {
        "event_id": event_id,
        "event_kind": "rx_packet",
        "session_id": session_id,
        "ts_ms": packet.received_at_ms,
        "ts_iso": ts_iso(packet.received_at_ms),
        "seq": packet.seq,
        "v": version,
        "mission_id": mission_id or mission.id,
        "operator": operator,
        "station": station,
        "frame_label": packet.frame_type,
        "transport_meta": str(packet.transport_meta.get("transmitter", "")),
        "inner_hex": packet.raw.hex(),
        "inner_len": len(packet.raw),
        "duplicate": packet.flags.is_duplicate,
        "uplink_echo": packet.flags.is_uplink_echo,
        "unknown": packet.flags.is_unknown,
        "warnings": list(packet.warnings),
        "mission": dict(packet.mission or {}),
    }


def parameter_records(
    packet: PacketEnvelope,
    *,
    session_id: str,
    rx_event_id: str,
    version: str,
    mission_id: str,
    operator: str = "",
    station: str = "",
) -> Iterator[dict[str, Any]]:
    """Yield one parameter event record per ``ParamUpdate`` on *packet*."""
    for u in packet.parameters:
        yield {
            "event_id": new_event_id(),
            "event_kind": "parameter",
            "session_id": session_id,
            "ts_ms": u.ts_ms or packet.received_at_ms,
            "ts_iso": ts_iso(u.ts_ms or packet.received_at_ms),
            "seq": packet.seq,
            "v": version,
            "mission_id": mission_id,
            "operator": operator,
            "station": station,
            "rx_event_id": rx_event_id,
            "name": u.name,
            "value": u.value,
            "unit": u.unit,
            "display_only": u.display_only,
        }


def tx_command_record(
    n: int,
    cmd_id: str,
    mission_facts: dict,
    parameters: list[dict],
    raw_cmd: bytes,
    wire: bytes,
    *,
    session_id: str,
    ts_ms: int,
    version: str,
    mission_id: str = "",
    operator: str = "",
    station: str = "",
    frame_label: str = "",
    log_fields: dict | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build one outbound command event record.

    Generic. The logger does NOT know about CSP, MAVERIC, headers, or
    protocol field names. mission_facts arrives canonical from the
    command codec; log_fields arrives pre-nested from the framer chain
    (e.g. {"facts": {"protocol": {"csp_header": ...}}}) and deep-merges
    into mission["facts"]. Single platform-level rule applied here:
    drop ts_ms from TX parameter rows (operator inputs have no
    meaningful timestamp).

    Both mission_facts and log_fields are deep-copied before the merge
    so caller-side state cannot be mutated.

    Framer log_fields contribute ONLY to mission_block["facts"]. Any
    keys outside "facts" are dropped.
    """
    cleaned_params = [
        {k: v for k, v in p.items() if k != "ts_ms"}
        for p in (parameters or ())
    ]
    facts: dict = copy.deepcopy(mission_facts or {})
    if isinstance(log_fields, dict):
        framer_facts = copy.deepcopy(log_fields.get("facts") or {})
        if framer_facts:
            _deep_merge(facts, framer_facts)
    mission_block: dict = {
        "id": mission_id,
        "cmd_id": cmd_id,
        "facts": facts,
        "parameters": cleaned_params,
    }

    return {
        "event_id": event_id or new_event_id(),
        "event_kind": "tx_command",
        "session_id": session_id,
        "ts_ms": ts_ms,
        "ts_iso": ts_iso(ts_ms),
        "seq": n,
        "v": version,
        "mission_id": mission_id,
        "operator": operator,
        "station": station,
        "frame_label": frame_label,
        "inner_hex": raw_cmd.hex(),
        "inner_len": len(raw_cmd),
        "wire_hex": wire.hex(),
        "wire_len": len(wire),
        "warnings": [],
        "mission": mission_block,
    }


def radio_event_record(
    action: str,
    *,
    session_id: str,
    ts_ms: int,
    version: str,
    mission_id: str = "",
    operator: str = "",
    station: str = "",
    state: str = "",
    pid: int | None = None,
    exit_code: int | None = None,
    command: list[str] | None = None,
    script: str = "",
    cwd: str = "",
    detail: str = "",
    expected: bool | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build one GNU Radio supervisor lifecycle event record."""
    return {
        "event_id": event_id or new_event_id(),
        "event_kind": "radio",
        "session_id": session_id,
        "ts_ms": ts_ms,
        "ts_iso": ts_iso(ts_ms),
        "seq": 0,
        "v": version,
        "mission_id": mission_id,
        "operator": operator,
        "station": station,
        "radio": {
            "action": action,
            "state": state,
            "pid": pid,
            "exit_code": exit_code,
            "command": list(command or ()),
            "script": script,
            "cwd": cwd,
            "detail": detail,
            "expected": expected,
        },
    }


def tracking_event_record(
    action: str,
    *,
    session_id: str,
    ts_ms: int,
    version: str,
    mission_id: str = "",
    operator: str = "",
    station: str = "",
    mode: str = "",
    prev_mode: str = "",
    station_id: str = "",
    rx_zmq_addr: str = "",
    tx_zmq_addr: str = "",
    detail: str = "",
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build one tracking-subsystem lifecycle event record.

    Used to audit operator-initiated Doppler engagements (connect / disconnect)
    so post-pass review can correlate uplink/downlink frequency corrections
    with the moment the tuner sink was bound or torn down.
    """
    return {
        "event_id": event_id or new_event_id(),
        "event_kind": "tracking",
        "session_id": session_id,
        "ts_ms": ts_ms,
        "ts_iso": ts_iso(ts_ms),
        "seq": 0,
        "v": version,
        "mission_id": mission_id,
        "operator": operator,
        "station": station,
        "tracking": {
            "action": action,
            "mode": mode,
            "prev_mode": prev_mode,
            "station_id": station_id,
            "rx_zmq_addr": rx_zmq_addr,
            "tx_zmq_addr": tx_zmq_addr,
            "detail": detail,
        },
    }


def tracking_sample_record(
    doppler: dict[str, Any],
    *,
    session_id: str,
    version: str,
    source: str,
    mission_id: str = "",
    operator: str = "",
    station: str = "",
    event_id: str | None = None,
    actual: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one tracking-sample record: az/el + requested Doppler-corrected
    RX/TX frequencies at one instant.

    *doppler* is the dict TrackingService.doppler() returns — every
    DopplerCorrection field (ts_ms, mode, range_rate_mps, rx/tx_hz,
    rx/tx_shift_hz, rx/tx_tune_hz) plus elevation_deg/azimuth_deg/range_km/
    altitude_km. range_km is the topocentric line-of-sight distance from
    the station (what matters for Doppler/pointing); altitude_km is the
    satellite's height above the ground track (the subsatellite point) —
    the two are different quantities and only converge near zenith.
    *source* distinguishes a background tick sample ("tick") from one taken
    at the exact moment of a downlink decode ("rx_decode") or a TX attempt
    ("tx_attempt"), so post-pass review can tell which rows are guaranteed
    to bracket an actual RX/TX event.

    *actual*, when given, is RadioService.latest_tune_result: a UHD
    read-back (get_center_freq()) of what the radio is actually tuned to,
    independent of the request above. This can confirm or refute a
    requested-vs-applied mismatch inside the flowgraph's own tuning
    arithmetic; it cannot detect a reference-oscillator (TCXO) drift common
    to both the request and the read-back — see
    docs/step4_actual_tune_readback.pdf for the full distinction. None
    (the default) when the flowgraph hasn't reported one yet.
    """
    ts_ms = int(doppler.get("ts_ms", 0))
    actual = actual or {}
    return {
        "event_id": event_id or new_event_id(),
        "event_kind": "tracking_sample",
        "session_id": session_id,
        "ts_ms": ts_ms,
        "ts_iso": ts_iso(ts_ms),
        "seq": 0,
        "v": version,
        "mission_id": mission_id,
        "operator": operator,
        "station": station,
        "tracking_sample": {
            "source": source,
            "mode": doppler.get("mode", ""),
            "station_id": doppler.get("station_id", ""),
            "satellite": doppler.get("satellite", ""),
            "elevation_deg": doppler.get("elevation_deg"),
            "azimuth_deg": doppler.get("azimuth_deg"),
            "range_km": doppler.get("range_km"),
            "altitude_km": doppler.get("altitude_km"),
            "range_rate_mps": doppler.get("range_rate_mps"),
            "rx_hz": doppler.get("rx_hz"),
            "rx_shift_hz": doppler.get("rx_shift_hz"),
            "rx_tune_hz": doppler.get("rx_tune_hz"),
            "tx_hz": doppler.get("tx_hz"),
            "tx_shift_hz": doppler.get("tx_shift_hz"),
            "tx_tune_hz": doppler.get("tx_tune_hz"),
            "rx_actual_hz": actual.get("rx_actual_hz"),
            "tx_actual_hz": actual.get("tx_actual_hz"),
        },
    }
