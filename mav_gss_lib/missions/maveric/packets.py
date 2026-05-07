"""DeclarativePacketsAdapter + MaverMissionPayload.

mission_payload is a frozen dataclass carrying the codec's WalkerPacket
header (resolved names, not ids) plus CSP V1 header info parsed from
the inner-payload front matter. The renderer reads attributes directly;
envelope.telemetry carries all per-domain fragment data.

Author:  Irfan Annuar - USC ISI SERC
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from mav_gss_lib.missions.maveric.codec import MaverPacketCodec
from mav_gss_lib.platform import (
    MissionPacket,
    NormalizedPacket,
    PacketFlags,
    PacketOps,
)
from mav_gss_lib.platform.framing.crc import verify_csp_crc32
from mav_gss_lib.platform.framing.csp_v1 import try_parse_csp_v1
from mav_gss_lib.platform.rx.frame_detect import detect_frame_type, normalize_frame
from mav_gss_lib.platform.spec import Mission, WalkerPacket
from mav_gss_lib.platform.spec.errors import CrcMismatch
from mav_gss_lib.platform.tx.verifiers import VerifierOutcome


_CSP_V1_HEADER_LEN = 4


@dataclass(frozen=True, slots=True)
class MaverMissionPayload:
    """Canonical mission_payload — readable by both the declarative walker
    (.walker_packet) and the rewritten renderer (.header / .args_raw /
    .valid_crc / .csp_header / .csp_crc32 / .csp_crc32_valid / .stripped_hdr)."""

    walker_packet: WalkerPacket | None
    header: dict[str, Any] | None     # codec header: {cmd_id, src, dest, echo, ptype} as resolved NAMES (str | int)
    args_raw: bytes
    valid_crc: bool
    csp_header: dict[str, Any] | None  # CSP V1 header dict from try_parse_csp_v1, None if not plausible
    csp_plausible: bool
    csp_crc32: int | None              # trailing CSP CRC32 from codec header (if present)
    csp_crc32_valid: bool | None       # verify_csp_crc32 result; None if no CRC32 trailer
    stripped_hdr: str | None


class MavericMissionFacts(TypedDict):
    id: Literal["maveric"]
    facts: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DeclarativePacketsAdapter(PacketOps):
    codec: MaverPacketCodec
    mission: Mission                  # for match_verifiers + future schema lookups

    def normalize(self, meta: dict[str, Any], raw: bytes) -> NormalizedPacket:
        # Transport-strip is platform-provided. Mirrors what rx/parser.py did.
        frame_type = detect_frame_type(meta)
        payload, stripped_hdr, warnings = normalize_frame(frame_type, raw)
        return NormalizedPacket(
            raw=raw,
            payload=payload,
            frame_type=frame_type,
            stripped_header=stripped_hdr,
            warnings=list(warnings),
        )

    def parse(self, normalized: NormalizedPacket) -> MissionPacket:
        # Parse CSP V1 header from the FIRST 4 bytes of the inner payload;
        # codec.unwrap expects CSP-stripped bytes.
        inner = normalized.payload
        csp_header, csp_plausible = try_parse_csp_v1(inner)
        cmd_bytes = inner[_CSP_V1_HEADER_LEN:] if len(inner) > _CSP_V1_HEADER_LEN else b""

        try:
            wp: WalkerPacket | None = self.codec.unwrap(cmd_bytes)
            header: dict[str, Any] | None = dict(wp.header)
            args_raw: bytes = wp.args_raw
            valid_crc = True
        except CrcMismatch:
            wp = None
            header = None
            args_raw = b""
            valid_crc = False

        # CSP CRC32 (trailing) — codec stashed the int into header on success.
        csp_crc32 = header.get("csp_crc32") if header else None
        csp_crc32_valid: bool | None = None
        if csp_crc32 is not None:
            ok, _, _ = verify_csp_crc32(inner)
            csp_crc32_valid = ok

        payload = MaverMissionPayload(
            walker_packet=wp,
            header=header,
            args_raw=args_raw,
            valid_crc=valid_crc,
            csp_header=csp_header,
            csp_plausible=csp_plausible,
            csp_crc32=csp_crc32,
            csp_crc32_valid=csp_crc32_valid,
            stripped_hdr=normalized.stripped_header,
        )
        mission = build_mission_facts(payload)
        return MissionPacket(
            payload=payload,
            warnings=list(normalized.warnings),
            mission=mission,
        )

    def classify(self, packet: MissionPacket) -> PacketFlags:
        payload: MaverMissionPayload = packet.payload
        integrity = _overall_integrity_ok(packet.mission)
        return PacketFlags(
            duplicate_key=_duplicate_fingerprint(payload),
            is_unknown=(payload.header is None),
            is_uplink_echo=_is_uplink_echo(payload, self.codec),
            integrity_ok=integrity,
        )

    def match_verifiers(self, envelope, open_instances, *, now_ms, rx_event_id=""):
        return match_verifiers(
            envelope, open_instances, now_ms=now_ms, rx_event_id=rx_event_id,
        )


def _duplicate_fingerprint(payload: MaverMissionPayload) -> str | None:
    h = payload.header
    if h is None:
        return None
    return f"{h.get('cmd_id')}|{h.get('src')}|{h.get('dest')}|{payload.args_raw.hex()}"


def _is_uplink_echo(payload: MaverMissionPayload, codec: MaverPacketCodec) -> bool:
    """True iff the packet's src equals the configured GS node name.

    Uses codec.gs_node_name (public). When src is an unknown id, codec
    leaves it as int — the comparison fails, returning False (correct
    behavior: unknown src is not a known echo)."""
    h = payload.header
    if h is None or codec.gs_node_name is None:
        return False
    return h.get("src") == codec.gs_node_name


def build_mission_facts(payload: MaverMissionPayload) -> MavericMissionFacts:
    cmd_id = (payload.header or {}).get("cmd_id", "")
    header = _mission_header(payload)
    integrity = _mission_integrity(payload)
    protocol: dict[str, Any] = {
        "args_hex": payload.args_raw.hex(),
        "csp_plausible": payload.csp_plausible,
        "stripped_header": payload.stripped_hdr,
    }
    if payload.csp_header is not None:
        protocol["csp_header"] = dict(payload.csp_header)
    return {
        "id": "maveric",
        "cmd_id": cmd_id,
        "facts": {
            "header": header,
            "protocol": protocol,
            "integrity": integrity,
        },
    }


def _mission_header(payload: MaverMissionPayload) -> dict[str, Any]:
    h = payload.header or {}
    return {
        "src": h.get("src", ""),
        "dest": h.get("dest", ""),
        "echo": h.get("echo", ""),
        "ptype": h.get("ptype", ""),
    }


def _mission_integrity(payload: MaverMissionPayload) -> dict[str, Any]:
    csp_ok = payload.csp_crc32_valid
    overall_ok = bool(payload.valid_crc) and csp_ok is not False
    return {
        "overall_ok": overall_ok,
        "body_crc_ok": bool(payload.valid_crc),
        "csp_crc32": payload.csp_crc32,
        "csp_crc32_ok": csp_ok,
    }


def _overall_integrity_ok(mission: dict[str, Any]) -> bool | None:
    facts = mission.get("facts")
    if not isinstance(facts, dict):
        return None
    integrity = facts.get("integrity")
    if not isinstance(integrity, dict):
        return None
    overall = integrity.get("overall_ok")
    return overall if isinstance(overall, bool) else None


def match_verifiers(envelope, open_instances, *, now_ms: int, rx_event_id: str = "") -> list:
    """Wire-side verifier matcher. Reads canonical name-shape header.

    No NodeTable dependency; codec resolved names at unwrap time. When
    codec couldn't resolve (header carries int instead of str), early
    return — unknown src/ptype don't match any verifier_id."""
    mp = getattr(envelope, "mission_payload", None)
    if mp is None or getattr(mp, "header", None) is None:
        return []
    h = mp.header
    cmd_id = h.get("cmd_id")
    src_name = h.get("src")
    ptype_name = h.get("ptype")
    if not (isinstance(cmd_id, str) and isinstance(src_name, str) and isinstance(ptype_name, str)):
        return []
    src_lower = src_name.lower()
    # ACK / RES / FILE / NACK responses must come back from the original
    # command's destination, so we filter by the full (cmd_id, dest) key
    # to keep a same-cmd_id different-dest sibling from stealing the
    # transition. TLM is cmd_id-keyed (`tlm_<cmd_id>`) and may originate
    # from any source, so it filters by cmd_id only.
    cmd_id_match = [
        i for i in open_instances
        if i.correlation_key and i.correlation_key[0] == cmd_id
    ]
    if ptype_name == "TLM":
        candidates = cmd_id_match
    else:
        candidates = [
            i for i in cmd_id_match
            if len(i.correlation_key) >= 2 and i.correlation_key[1] == src_name
        ]
    candidates.sort(key=lambda i: i.t0_ms, reverse=True)
    if not candidates:
        return []
    if ptype_name == "ACK":
        expected = f"{src_lower}_ack"
    elif ptype_name == "RES":
        expected = f"res_from_{src_lower}"
    elif ptype_name == "FILE":
        expected = f"file_from_{src_lower}"
    elif ptype_name == "NACK":
        expected = f"nack_{src_lower}"
    elif ptype_name == "TLM":
        expected = f"tlm_{cmd_id}"
    else:
        return []
    for inst in candidates:
        if not any(v.verifier_id == expected for v in inst.verifier_set.verifiers):
            continue
        existing = inst.outcomes.get(expected)
        if existing is not None and existing.state != "pending":
            continue
        return [(
            inst.instance_id,
            expected,
            VerifierOutcome.passed(matched_at_ms=now_ms, match_event_id=rx_event_id),
        )]
    return []


__all__ = [
    "DeclarativePacketsAdapter",
    "MavericMissionFacts",
    "MaverMissionPayload",
    "build_mission_facts",
    "match_verifiers",
]
