"""Astrocast 0.1 packet operations (RX-only).

Two downlink shapes:

  FX.25 beacons (1k2 FSK)  — AX.25 UI frame; ASCII payload of two
      '*'-terminated sentences ($GPRMC dummy NMEA + $HK housekeeping)
      padded with spaces to a fixed 171-byte field.
  CCSDS-RS downloads (9k6) — 1115-byte Reed-Solomon frames; payload
      format is not public, logged raw as opaque telemetry.

The HK clock is a 48-bit hex tick count of 1/65536 s since
2016-01-01T00:00:00Z (verified against recordings: consecutive beacons
sit exactly 60 s apart and land in each recording's era).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from mav_gss_lib.platform import (
    MissionPacket,
    NormalizedPacket,
    PacketFlags,
)
from mav_gss_lib.platform.rx.frame_detect import normalize_frame


MISSION_ID = "astrocast"

_CLOCK_EPOCH = datetime(2016, 1, 1, tzinfo=timezone.utc)
_CLOCK_TICKS_PER_S = 65536

_HK_RE = re.compile(
    r"\$HK,"
    r"(0x[0-9A-Fa-f]+),"      # 48-bit clock, hex
    r"(-?[\d.]+),"            # voltage (V)
    r"(-?\d+),"               # current (mA)
    r"(-?\d+),"               # temperature (degC)
    r"(-?\d+),"               # RSSI (dBm)
    r"(-?\d+),"               # AFC offset (Hz)
    r"(0x[0-9A-Fa-f]+)\*"     # format flags byte
)
_GPRMC_RE = re.compile(r"\$GPRMC,([^*]*)\*")

_KIND_LABEL = {"beacon": "BCN", "telemetry": "TLM", "unknown": "UNK"}


@dataclass(frozen=True, slots=True)
class HkReading:
    clock_hex: str
    clock_utc: datetime | None
    voltage_v: float
    current_ma: int
    temp_c: int
    rssi_dbm: int
    afc_hz: int
    flags: str


@dataclass(frozen=True, slots=True)
class _TokenPacket:
    """WalkerPacket for the ascii_tokens layout (whitespace-split)."""

    args_raw: bytes
    header: dict[str, Any]


@dataclass(slots=True)
class AstrocastPayload:
    kind: str                                # "beacon" | "telemetry" | "unknown"
    fingerprint: str
    src: str = ""
    dst: str = ""
    hk: HkReading | None = None
    gps: dict[str, Any] | None = None
    walker_packet: _TokenPacket | None = None
    warnings: list[str] = field(default_factory=list)


def _frame_type_for(meta: dict[str, Any]) -> str:
    transmitter = str(meta.get("transmitter", ""))
    if "FX.25" in transmitter:
        return "FX.25"
    if "9k6" in transmitter:
        return "CCSDS-RS"
    return "UNKNOWN"


def _decode_callsign(address: bytes) -> str:
    call = "".join(chr((b >> 1) & 0x7F) for b in address[:6]).strip()
    ssid = (address[6] >> 1) & 0x0F if len(address) > 6 else 0
    return f"{call}-{ssid}" if ssid else call


def _clock_to_utc(clock_hex: str) -> datetime | None:
    try:
        ticks = int(clock_hex, 16)
    except ValueError:
        return None
    return _CLOCK_EPOCH + timedelta(seconds=ticks / _CLOCK_TICKS_PER_S)


def _parse_hk(text: str) -> HkReading | None:
    match = _HK_RE.search(text)
    if match is None:
        return None
    clock_hex, volts, current, temp, rssi, afc, flags = match.groups()
    try:
        return HkReading(
            clock_hex=clock_hex,
            clock_utc=_clock_to_utc(clock_hex),
            voltage_v=float(volts),
            current_ma=int(current),
            temp_c=int(temp),
            rssi_dbm=int(rssi),
            afc_hz=int(afc),
            flags=flags,
        )
    except ValueError:
        return None


def _parse_gprmc(text: str) -> dict[str, Any] | None:
    match = _GPRMC_RE.search(text)
    if match is None:
        return None
    fields = match.group(1).split(",")
    gps: dict[str, Any] = {"sentence": f"$GPRMC,{match.group(1)}*"}
    if len(fields) >= 6:
        gps["utc"] = fields[0]
        gps["status"] = fields[1]
        gps["lat"] = f"{fields[2]} {fields[3]}".strip()
        gps["lon"] = f"{fields[4]} {fields[5]}".strip()
    if len(fields) >= 9:
        gps["date"] = fields[8]
    return gps


def _hk_tokens(hk: HkReading) -> bytes:
    clock = (
        hk.clock_utc.isoformat(timespec="seconds")
        if hk.clock_utc is not None
        else hk.clock_hex
    )
    tokens = (
        clock,
        f"{hk.voltage_v}",
        f"{hk.current_ma}",
        f"{hk.temp_c}",
        f"{hk.rssi_dbm}",
        f"{hk.afc_hz}",
        hk.flags,
    )
    return " ".join(tokens).encode("ascii")


def _build_mission_facts(payload: AstrocastPayload) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "header": {
            "type": _KIND_LABEL[payload.kind],
            "src": payload.src,
            "dst": payload.dst,
        }
    }
    if payload.hk is not None:
        hk = payload.hk
        facts["beacon"] = {
            "clock_utc": (
                hk.clock_utc.isoformat(timespec="seconds")
                if hk.clock_utc is not None
                else hk.clock_hex
            ),
            "clock_hex": hk.clock_hex,
            "voltage_v": hk.voltage_v,
            "current_ma": hk.current_ma,
            "temp_c": hk.temp_c,
            "rssi_dbm": hk.rssi_dbm,
            "afc_hz": hk.afc_hz,
            "flags": hk.flags,
        }
    if payload.gps is not None:
        facts["gps"] = dict(payload.gps)
    return {"id": MISSION_ID, "cmd_id": "", "facts": facts}


class AstrocastPacketOps:
    """PacketOps for Astrocast 0.1 — RX-only, no verifier matching."""

    def normalize(self, meta: dict[str, Any], raw: bytes) -> NormalizedPacket:
        frame_type = _frame_type_for(meta)
        if frame_type == "FX.25":
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

        if normalized.frame_type == "CCSDS-RS":
            payload = AstrocastPayload(kind="telemetry", fingerprint=fingerprint)
        else:
            text = normalized.payload.decode("ascii", errors="replace").rstrip(" \x00")
            hk = _parse_hk(text)
            gps = _parse_gprmc(text)
            if hk is None and gps is None:
                payload = AstrocastPayload(
                    kind="unknown",
                    fingerprint=fingerprint,
                    src=src,
                    dst=dst,
                    warnings=["no $HK/$GPRMC sentence found"],
                )
            else:
                payload = AstrocastPayload(
                    kind="beacon",
                    fingerprint=fingerprint,
                    src=src,
                    dst=dst,
                    hk=hk,
                    gps=gps,
                )
                if hk is not None:
                    payload.walker_packet = _TokenPacket(
                        args_raw=_hk_tokens(hk),
                        header={"kind": payload.kind},
                    )

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
