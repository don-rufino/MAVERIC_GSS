"""Live RX websocket event serializers."""

from __future__ import annotations

from typing import Any

from mav_gss_lib.platform.contract.packets import PacketEnvelope
from mav_gss_lib.platform.json_safety import json_safe


def packet_summary(packet: PacketEnvelope, event_id: str | None = None) -> dict[str, Any]:
    flags = packet.flags
    transport_meta = dict(packet.transport_meta)
    signal = transport_meta.pop("signal", None)
    summary = {
        "event_id": event_id or packet.event_id,
        "num": packet.seq,
        "frame": packet.frame_type,
        "size": len(packet.raw),
        "raw_hex": packet.raw.hex(),
        "received_at_ms": packet.received_at_ms,
        "transport_meta": transport_meta,
        "mission": dict(packet.mission),
        "warnings": list(packet.warnings),
        "is_echo": flags.is_uplink_echo,
        "is_dup": flags.is_duplicate,
        "is_unknown": flags.is_unknown,
        "flags": {
            "duplicate": flags.is_duplicate,
            "unknown": flags.is_unknown,
            "uplink_echo": flags.is_uplink_echo,
            "integrity_ok": flags.integrity_ok,
        },
    }
    if isinstance(signal, dict):
        summary["signal"] = signal
    return summary


def parameter_values(packet: PacketEnvelope) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for param in packet.parameters:
        item = {
            "name": param.name,
            "v": json_safe(param.value),
            "t": param.ts_ms,
        }
        if param.unit:
            item["unit"] = param.unit
        if param.display_only:
            item["display_only"] = True
        values.append(item)
    return values


def rx_packet_event(
    packet: PacketEnvelope,
    event_id: str | None = None,
    *,
    replay: bool = False,
) -> dict[str, Any]:
    event = {
        "type": "rx_packet",
        "packet": packet_summary(packet, event_id),
        "parameters": parameter_values(packet),
    }
    if replay:
        event["replay"] = True
    return event


def rx_batch_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "rx_batch",
        "events": events,
    }


__all__ = ["packet_summary", "parameter_values", "rx_batch_event", "rx_packet_event"]
