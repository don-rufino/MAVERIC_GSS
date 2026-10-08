"""SCION-X packet operations (RX-only).

Frames come from MAV_DUO's SCION-X burst decoder (gnuradio/scionx_burst.py):
the received part of the 274-byte AX.25 UI frame, starting at
``meta["burst"]["start_byte"]`` (late key-up usually costs the first ~50-100
bytes), plus the decode outcome. A burst that looked like SCION-X but did not
decode arrives with an empty payload so the pass still shows it (with its
signal measurement). Stock gr-satellites AX.25 frames, if any, are complete
CRC-checked frames from byte 0.

Telemetry values come from the local field map (see telemetry.py); they are
handed to the platform as ready ParamUpdates because a frame missing its
start cannot be walked positionally by a declarative container.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from mav_gss_lib.platform import MissionPacket, NormalizedPacket, PacketFlags
from mav_gss_lib.platform.contract.parameters import ParamUpdate

from mav_gss_lib.missions.scionx import telemetry

MISSION_ID = "scionx"
FRAME_LABEL = "AX.25 NRZ"
HEADER_BYTES = 16

_VERIFIED_LABEL = {"crc_pass": "CRC OK", "unverified": "UNVERIFIED", "undecoded": "NO DECODE"}


@dataclass(slots=True)
class ScionxPayload:
    kind: str                         # "beacon" | "burst" (undecoded)
    fingerprint: str
    status: str                       # "crc_pass" | "unverified" | "undecoded"
    start_byte: int = 0
    frame: bytes = b""
    src: str = ""
    dst: str = ""
    burst: dict[str, Any] = field(default_factory=dict)
    param_updates: tuple[ParamUpdate, ...] = ()
    fields_received: int = 0
    fields_total: int = 0
    implausible: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ScionxNormalized(NormalizedPacket):
    """NormalizedPacket plus the burst decoder's outcome for parse()."""
    burst: dict[str, Any] = field(default_factory=dict)


def _callsign(address: bytes) -> str:
    call = "".join(chr((b >> 1) & 0x7F) for b in address[:6]).strip()
    ssid = (address[6] >> 1) & 0x0F if len(address) > 6 else 0
    return f"{call}-{ssid}" if ssid else call


def _burst_info(meta: dict[str, Any]) -> dict[str, Any] | None:
    info = meta.get("burst")
    return info if isinstance(info, dict) else None


class ScionxPacketOps:
    """PacketOps for SCION-X — RX-only, no verifier matching."""

    def normalize(self, meta: dict[str, Any], raw: bytes) -> NormalizedPacket:
        info = _burst_info(meta)
        warnings: list[str] = []
        if info is None:
            # stock gr-satellites AX.25 (complete, CRC already checked);
            # its PDUs omit the FCS
            info = {"status": "crc_pass", "start_byte": 0, "crc": "pass (gr-satellites)"}
        info = dict(info)
        if "gss_burst_end" in meta:
            info["burst_end"] = meta["gss_burst_end"]
        status = str(info.get("status", "unverified"))
        start = int(info.get("start_byte", 0) or 0)
        if status == "unverified":
            reason = ("starts at frame byte %d, no CRC to check" % start if start
                      else f"CRC {info.get('crc', 'unverifiable')}")
            warnings.append(f"UNVERIFIED: {reason}")
        if info.get("ambiguous"):
            warnings.append("AMBIGUOUS: stuffing choices tie; values may be one bit off")
        if status == "undecoded":
            warnings.append("NO DECODE: SCION-X-like burst, frame not recovered (see IQ recording)")
        return ScionxNormalized(raw=raw, payload=raw, frame_type=FRAME_LABEL,
                                warnings=warnings, burst=info)

    def parse(self, normalized: NormalizedPacket) -> MissionPacket:
        raw = bytes(normalized.raw)
        info = getattr(normalized, "burst", None) or {"status": "unverified"}
        status = str(info.get("status", "unverified"))
        start = int(info.get("start_byte", 0) or 0)
        fingerprint = hashlib.sha1(raw).hexdigest() if raw else f"burst-{info.get('burst_end', id(info))}"
        src = dst = ""
        if raw and start == 0 and len(raw) >= HEADER_BYTES:
            dst, src = _callsign(raw[0:7]), _callsign(raw[7:14])
        payload = ScionxPayload(
            kind="beacon" if raw else "burst", fingerprint=fingerprint, status=status,
            start_byte=start, frame=raw, src=src, dst=dst, burst=dict(info))
        warnings: list[str] = []          # normalize() warnings are merged by the pipeline
        if raw:
            tlm = telemetry.decode(raw, start)
            payload.param_updates = tuple(tlm.params)
            payload.fields_received, payload.fields_total = tlm.fields_received, tlm.fields_total
            payload.implausible = tlm.implausible
            if tlm.implausible:
                shown = ", ".join(tlm.implausible[:6]) + (" …" if len(tlm.implausible) > 6 else "")
                warnings.append(f"IMPLAUSIBLE ({len(tlm.implausible)}): {shown}")
            if not tlm.fields_total:
                warnings.append("no local beacon field map (beacon_fields.json); values not extracted")
        return MissionPacket(payload=payload, warnings=warnings, mission=_facts(payload))

    def classify(self, packet: MissionPacket) -> PacketFlags:
        payload = packet.payload
        return PacketFlags(
            duplicate_key=payload.fingerprint,
            is_unknown=payload.kind != "beacon",
            is_uplink_echo=False,
            integrity_ok=True if payload.status == "crc_pass" else None,
        )

    def match_verifiers(self, envelope, open_instances, *, now_ms, rx_event_id=""):
        return []


def _facts(payload: ScionxPayload) -> dict[str, Any]:
    end = payload.start_byte + len(payload.frame) - 1 if payload.frame else None
    header: dict[str, Any] = {
        "type": "BCN" if payload.kind == "beacon" else "BURST",
        "check": _VERIFIED_LABEL.get(payload.status, payload.status.upper()),
        "bytes": f"{payload.start_byte}-{end}" if end is not None else "",
        "src": payload.src,
        "dst": payload.dst,
    }
    if payload.fields_total:
        header["fields"] = f"{payload.fields_received}/{payload.fields_total}"
    decode = {k: payload.burst[k] for k in (
        "crc", "anchor", "ambiguous", "kept_stuffing_sites", "polarity", "snr_db",
        "bit_rate", "transitions", "marginal_bits", "burst_s") if k in payload.burst}
    facts: dict[str, Any] = {"header": header}
    if decode:
        facts["decode"] = decode
    return {"id": MISSION_ID, "cmd_id": "", "facts": facts}
