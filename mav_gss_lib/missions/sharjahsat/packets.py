"""Sharjahsat-1 packet operations (RX-only).

One downlink shape: 9k6 FSK AX.25 G3RUH UI frames whose info field is an
ESER-prefixed header (identifier + tm_id + declared length + u32 packet
counter) followed by either a 246-byte housekeeping block (tm_id 'P',
decoded by :mod:`telemetry`) or base64 image chunk data (tm_id 'A',
logged raw as an opaque product).
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import Any

from mav_gss_lib.platform import (
    MissionPacket,
    NormalizedPacket,
    PacketFlags,
)
from mav_gss_lib.platform.rx.frame_detect import detect_frame_type, normalize_frame

from mav_gss_lib.missions.sharjahsat.telemetry import TELEMETRY_SIZE, parse_telemetry


MISSION_ID = "sharjahsat"

_ESER_MAGIC = b"ESER"
_TM_TELEMETRY = 0x50
_TM_IMAGE = 0x41
_HEADER_SIZE = 10

_KIND_LABEL = {"telemetry": "TLM", "image": "IMG", "unknown": "UNK"}


@dataclass(frozen=True, slots=True)
class _TokenPacket:
    """WalkerPacket for the ascii_tokens layout (whitespace-split)."""

    args_raw: bytes
    header: dict[str, Any]


@dataclass(slots=True)
class SharjahsatPayload:
    kind: str                                # "telemetry" | "image" | "unknown"
    fingerprint: str
    src: str = ""
    dst: str = ""
    counter: int = 0
    sections: dict[str, Any] | None = None
    image_length: int = 0
    walker_packet: _TokenPacket | None = None
    warnings: list[str] = field(default_factory=list)


def _decode_callsign(address: bytes) -> str:
    call = "".join(chr((b >> 1) & 0x7F) for b in address[:6]).strip()
    ssid = (address[6] >> 1) & 0x0F if len(address) > 6 else 0
    return f"{call}-{ssid}" if ssid else call


def _parse_info(info: bytes, fingerprint: str, src: str, dst: str) -> SharjahsatPayload:
    if len(info) < _HEADER_SIZE:
        return SharjahsatPayload(
            kind="unknown", fingerprint=fingerprint, src=src, dst=dst,
            warnings=[f"info field too short for ESER header ({len(info)} bytes)"],
        )
    if info[0:4] != _ESER_MAGIC:
        return SharjahsatPayload(
            kind="unknown", fingerprint=fingerprint, src=src, dst=dst,
            warnings=["missing ESER identifier"],
        )

    tm_id = info[4]
    declared_length = info[5]
    counter = struct.unpack_from("<I", info, 6)[0]
    data = info[_HEADER_SIZE:]
    warnings: list[str] = []
    if declared_length != len(data):
        warnings.append(
            f"declared data length {declared_length} != {len(data)} bytes on wire"
        )

    if tm_id == _TM_TELEMETRY:
        if len(data) < TELEMETRY_SIZE:
            return SharjahsatPayload(
                kind="unknown", fingerprint=fingerprint, src=src, dst=dst,
                counter=counter,
                warnings=warnings + [
                    f"truncated telemetry block ({len(data)}/{TELEMETRY_SIZE} bytes)"
                ],
            )
        decoded = parse_telemetry(data[:TELEMETRY_SIZE])
        return SharjahsatPayload(
            kind="telemetry", fingerprint=fingerprint, src=src, dst=dst,
            counter=counter,
            sections=decoded.sections,
            walker_packet=_TokenPacket(
                args_raw=decoded.tokens,
                header={"kind": "telemetry"},
            ),
            warnings=warnings,
        )

    if tm_id == _TM_IMAGE:
        return SharjahsatPayload(
            kind="image", fingerprint=fingerprint, src=src, dst=dst,
            counter=counter, image_length=len(data), warnings=warnings,
        )

    return SharjahsatPayload(
        kind="unknown", fingerprint=fingerprint, src=src, dst=dst,
        counter=counter,
        warnings=warnings + [f"unknown tm_id 0x{tm_id:02x}"],
    )


def _build_mission_facts(payload: SharjahsatPayload) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "header": {
            "type": _KIND_LABEL[payload.kind],
            "src": payload.src,
            "dst": payload.dst,
            "counter": payload.counter,
        }
    }
    if payload.sections is not None:
        facts.update(payload.sections)
    if payload.kind == "image":
        facts["image"] = {"length": payload.image_length}
    return {"id": MISSION_ID, "cmd_id": "", "facts": facts}


class SharjahsatPacketOps:
    """PacketOps for Sharjahsat-1 — RX-only, no verifier matching."""

    def normalize(self, meta: dict[str, Any], raw: bytes) -> NormalizedPacket:
        frame_type = detect_frame_type(meta)
        if frame_type == "AX.25":
            payload, stripped_header, warnings = normalize_frame("AX.25", raw)
            return NormalizedPacket(
                raw=raw,
                payload=payload,
                frame_type=frame_type,
                stripped_header=stripped_header,
                warnings=list(warnings),
            )
        return NormalizedPacket(raw=raw, payload=raw, frame_type=frame_type)

    def parse(self, normalized: NormalizedPacket) -> MissionPacket:
        fingerprint = hashlib.sha1(normalized.raw).hexdigest()
        src = dst = ""
        if normalized.stripped_header:
            header_bytes = bytes.fromhex(normalized.stripped_header)
            if len(header_bytes) >= 14:
                dst = _decode_callsign(header_bytes[0:7])
                src = _decode_callsign(header_bytes[7:14])

        payload = _parse_info(normalized.payload, fingerprint, src, dst)
        warnings = list(normalized.warnings) + list(payload.warnings)
        return MissionPacket(
            payload=payload,
            warnings=warnings,
            mission=_build_mission_facts(payload),
        )

    def classify(self, packet: MissionPacket) -> PacketFlags:
        payload = packet.payload
        return PacketFlags(
            duplicate_key=payload.fingerprint,
            is_unknown=payload.kind == "unknown",
            is_uplink_echo=False,
        )

    def match_verifiers(self, envelope, open_instances, *, now_ms, rx_event_id=""):
        return []
