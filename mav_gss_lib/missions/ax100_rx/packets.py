"""PacketOps shared by the AX100 Mode 5 RX-only mission family.

The gr-satellites AX100 ASM+Golay deframer strips the outer 312-byte frame
and publishes the inner CSP packet (4-byte CSP v1 header + payload), so
`normalize` is a pass-through. `parse` always extracts the CSP header into
mission facts; payload decoding is delegated to an optional per-mission
housekeeping decoder. Frames that arrive from a non-AX100 decoder branch
(the parallel AX.25 G3RUH chains) are flagged unknown — on a Mode 5 mission
they are other-satellite traffic or noise.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable

from mav_gss_lib.platform import (
    MissionPacket,
    NormalizedPacket,
    PacketFlags,
)
from mav_gss_lib.platform.framing.csp_v1 import try_parse_csp_v1
from mav_gss_lib.platform.rx.frame_detect import detect_frame_type


def _try_parse_csp_v1_le(payload: bytes) -> tuple[dict[str, Any] | None, bool]:
    """CSP v1 header packed as a little-endian uint32 (standard bitfield).

    Some libcsp builds on little-endian MCUs transmit the header without
    byte-swapping to network order — CATSAT does (per the community
    catsat.ksy field expressions)."""
    if len(payload) < 4:
        return None, False
    h = int.from_bytes(payload[0:4], "little")
    csp = {
        "prio":  (h >> 30) & 0x03,
        "src":   (h >> 25) & 0x1F,
        "dest":  (h >> 20) & 0x1F,
        "dport": (h >> 14) & 0x3F,
        "sport": (h >> 8)  & 0x3F,
        "flags": h & 0xFF,
    }
    plausible = csp["src"] <= 20 and csp["dest"] <= 20
    return csp, plausible


_KIND_LABEL = {"beacon": "BCN", "telemetry": "TLM", "unknown": "UNK"}


@dataclass(frozen=True, slots=True)
class TokenPacket:
    """WalkerPacket for the ascii_tokens layout (whitespace-split)."""

    args_raw: bytes
    header: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HkDecode:
    """Successful mission-specific housekeeping decode of a CSP payload."""

    container_kind: str          # walker restriction key, e.g. "beacon0"
    tokens: bytes                # whitespace-separated ascii_tokens payload
    facts: dict[str, Any]        # rendered under mission.facts.beacon
    warnings: tuple[str, ...] = ()


# hk_decoder(csp_header, payload_after_header) -> HkDecode | None
HkDecoder = Callable[[dict[str, Any], bytes], "HkDecode | None"]


@dataclass(slots=True)
class Ax100Payload:
    kind: str                            # "beacon" | "telemetry" | "unknown"
    fingerprint: str
    csp: dict[str, Any] | None = None
    hk: HkDecode | None = None
    walker_packet: TokenPacket | None = None
    warnings: list[str] = field(default_factory=list)


class Ax100RxPacketOps:
    """PacketOps for one RX-only AX100 Mode 5 target — no verifier matching."""

    def __init__(
        self,
        mission_id: str,
        hk_decoder: HkDecoder | None = None,
        *,
        csp_endianness: str = "big",
    ) -> None:
        self.mission_id = mission_id
        self.hk_decoder = hk_decoder
        self._parse_csp = (
            _try_parse_csp_v1_le if csp_endianness == "little" else try_parse_csp_v1
        )

    def normalize(self, meta: dict[str, Any], raw: bytes) -> NormalizedPacket:
        return NormalizedPacket(
            raw=raw,
            payload=raw,
            frame_type=detect_frame_type(meta),
        )

    def parse(self, normalized: NormalizedPacket) -> MissionPacket:
        fingerprint = hashlib.sha1(normalized.raw).hexdigest()
        payload = Ax100Payload(kind="unknown", fingerprint=fingerprint)

        if normalized.frame_type != "ASM+GOLAY":
            payload.warnings.append(
                f"{normalized.frame_type} frame on an AX100 Mode 5 mission"
            )
        else:
            csp, plausible = self._parse_csp(normalized.payload)
            if csp is None:
                payload.warnings.append("frame shorter than a CSP v1 header")
            else:
                payload.csp = csp
                payload.kind = "telemetry"
                if not plausible:
                    payload.warnings.append("implausible CSP src/dest")
                if self.hk_decoder is not None:
                    hk = self.hk_decoder(csp, normalized.payload[4:])
                    if hk is not None:
                        payload.kind = "beacon"
                        payload.hk = hk
                        payload.warnings.extend(hk.warnings)
                        payload.walker_packet = TokenPacket(
                            args_raw=hk.tokens,
                            header={"kind": hk.container_kind},
                        )

        return MissionPacket(
            payload=payload,
            warnings=list(normalized.warnings) + payload.warnings,
            mission=self._mission_facts(payload, normalized),
        )

    def _mission_facts(
        self, payload: Ax100Payload, normalized: NormalizedPacket
    ) -> dict[str, Any]:
        header: dict[str, Any] = {
            "type": _KIND_LABEL[payload.kind],
            "len": len(normalized.payload),
        }
        facts: dict[str, Any] = {"header": header}
        if payload.csp is not None:
            header.update(
                src=payload.csp["src"],
                dst=payload.csp["dest"],
                dport=payload.csp["dport"],
                sport=payload.csp["sport"],
            )
            facts["protocol"] = {"csp_header": dict(payload.csp)}
        if payload.hk is not None:
            facts["beacon"] = dict(payload.hk.facts)
        return {"id": self.mission_id, "cmd_id": "", "facts": facts}

    def classify(self, packet: MissionPacket) -> PacketFlags:
        payload = packet.payload
        return PacketFlags(
            duplicate_key=payload.fingerprint,
            is_unknown=payload.kind == "unknown",
            is_uplink_echo=False,
        )

    def match_verifiers(self, envelope, open_instances, *, now_ms, rx_event_id=""):
        return []
