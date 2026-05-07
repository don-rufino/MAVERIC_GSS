"""
mav_gss_lib.platform.framing.csp_v1 -- CSP v1 Header & KISS Framing

CSP v1 header parse/build for the Cubesat Space Protocol.
KISS framing for CSP transport.
CSPConfig for TX-direction CSP wrapping with optional CRC-32C.
CSPv1Framer wraps CSPConfig in the platform Framer contract.
"""

from __future__ import annotations

from typing import Any

from mav_gss_lib.platform.framing.protocol import Framer
from mav_gss_lib.platform.framing.crc import crc32c


# =============================================================================
#  KISS FRAMING
# =============================================================================

FEND  = 0xC0
FESC  = 0xDB
TFEND = 0xDC
TFESC = 0xDD


def kiss_wrap(raw_cmd: bytes) -> bytes:
    """KISS-wrap a raw command payload.
    Output: C0 00 [kiss-escaped data] C0
    DB must be escaped before C0 to avoid double-escaping."""
    escaped = raw_cmd.replace(b'\xDB', b'\xDB\xDD').replace(b'\xC0', b'\xDB\xDC')
    return b'\xC0\x00' + escaped + b'\xC0'


# =============================================================================
#  CSP V1 HEADER
#
#  32-bit big-endian word:
#    [31:30] priority  [29:25] source  [24:20] destination
#    [19:14] dest_port [13:8]  src_port [7:0] flags
# =============================================================================

def try_parse_csp_v1(payload: bytes) -> tuple[dict[str, Any] | None, bool]:
    """Parse first 4 bytes as CSP v1 header (RX direction).
    Returns (parsed_dict, is_plausible) or (None, False)."""
    if len(payload) < 4:
        return None, False

    h = int.from_bytes(payload[0:4], "big")
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


class CSPConfig:
    """Configurable CSP v1 header for uplink (TX direction).

    When enabled, wrap() prepends the 4-byte CSP header and appends
    a 4-byte CRC-32C (Castagnoli) over the entire CSP packet."""

    def __init__(self) -> None:
        self.enabled = True
        self.prio    = 2
        self.src     = 0
        self.dest    = 8
        self.dport   = 0
        self.sport   = 24
        self.flags   = 0x00
        self.csp_crc = True

    def build_header(self) -> bytes:
        """Pack CSP fields into 4-byte big-endian header."""
        h = ((self.prio  & 0x03) << 30 |
             (self.src   & 0x1F) << 25 |
             (self.dest  & 0x1F) << 20 |
             (self.dport & 0x3F) << 14 |
             (self.sport & 0x3F) << 8  |
             (self.flags & 0xFF))
        return h.to_bytes(4, 'big')

    def overhead(self) -> int:
        """Number of bytes the CSP header + optional CRC-32C add to a payload."""
        if not self.enabled:
            return 0
        return 8 if self.csp_crc else 4

    def wrap(self, payload: bytes) -> bytes:
        """Prepend CSP header and optionally append CRC-32C.

        Output: [CSP header 4B] [payload] [CRC-32C 4B BE] (if csp_crc)
                [CSP header 4B] [payload]                  (if not csp_crc)"""
        if self.enabled:
            packet = self.build_header() + payload
            if self.csp_crc:
                checksum = crc32c(packet).to_bytes(4, 'big')
                return packet + checksum
            return packet
        return payload


class CSPv1Framer(Framer):
    """`Framer` adapter wrapping a `CSPConfig` snapshot."""

    frame_label = "CSP-v1"

    __slots__ = ("config",)

    def __init__(self, config: CSPConfig) -> None:
        self.config = config

    def frame(self, payload: bytes) -> bytes:
        return self.config.wrap(payload)

    def overhead(self) -> int:
        return self.config.overhead()

    def max_payload(self) -> int | None:
        return None

    def log_fields(self) -> dict[str, Any]:
        if not self.config.enabled:
            return {}
        c = self.config
        return {"facts": {"protocol": {"csp_header": {
            "prio": int(c.prio), "src": int(c.src), "dest": int(c.dest),
            "dport": int(c.dport), "sport": int(c.sport),
            "flags": int(c.flags), "csp_crc": bool(c.csp_crc),
        }}}}

    def log_line(self) -> str | None:
        if not self.config.enabled:
            return None
        c = self.config
        return (f"  CSP        Prio:{c.prio} Src:{c.src}({c.sport}) "
                f"Dest:{c.dest}({c.dport}) Flags:0x{c.flags:02X}")
