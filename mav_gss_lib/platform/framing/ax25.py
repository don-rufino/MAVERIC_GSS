"""
mav_gss_lib.platform.framing.ax25 -- AX.25 Protocol Support (AX100 Mode 6)

Three complementary pieces:
  1. AX25Config   -- TX-direction AX.25 UI frame header wrapper
  2. Encoder      -- HDLC framer + G3RUH scrambler + NRZI encoder + bit packing
  3. Ax25Framer   -- Framer-contract adapter combining (1)+(2) for chain use

AX25Config.wrap() produces an AX.25 packet (header + payload).
build_ax25_gfsk_frame() takes that packet and produces the over-the-air
encoded bitstream ready for GFSK modulation.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

from typing import Any

from mav_gss_lib.platform.framing.protocol import Framer


# =============================================================================
#  AX.25 UI FRAME HEADER
# =============================================================================

class AX25Config:
    """Configurable AX.25 header for uplink (TX direction).

    Wraps a payload with a 16-byte AX.25 UI frame header so the PDU
    is ready for an HDLC framer with no custom GRC blocks needed.
    """

    HEADER_LEN = 16  # 7 dest + 7 src + 1 control + 1 PID

    def __init__(self) -> None:
        self.enabled   = True
        self.dest_call = "NOCALL"
        self.dest_ssid = 0
        self.src_call  = "NOCALL"
        self.src_ssid  = 0

    @staticmethod
    def _encode_callsign(call: str, ssid: int, last: bool = False) -> bytes:
        """Encode callsign + SSID into 7 AX.25 address bytes.

        Each character is shifted left 1 bit. Callsign is space-padded
        to 6 characters. SSID byte: 0b0RR_SSSS_E (E=1 if last address).

        *ssid* accepts either a 0-15 SSID value (standard) or a raw
        SSID byte (> 0x0F, e.g. 0x60 from GomSpace AX100 config).
        The extension bit is always managed automatically."""
        call = call.upper().ljust(6)[:6]
        addr = bytearray(ord(c) << 1 for c in call)
        if ssid > 0x0F:
            ssid_byte = ssid & 0xFE
            if last:
                ssid_byte |= 0x01
        else:
            ssid_byte = 0x60 | ((ssid & 0x0F) << 1)
            if last:
                ssid_byte |= 0x01
        addr.append(ssid_byte)
        return bytes(addr)

    def overhead(self) -> int:
        """Number of bytes the AX.25 header adds to a payload."""
        return self.HEADER_LEN if self.enabled else 0

    def wrap(self, payload: bytes) -> bytes:
        """Prepend 16-byte AX.25 UI frame header if enabled.

        Output: [dest 7B][src 7B][0x03][0xF0][payload]"""
        if self.enabled:
            header = (
                self._encode_callsign(self.dest_call, self.dest_ssid, last=False)
                + self._encode_callsign(self.src_call, self.src_ssid, last=True)
                + b'\x03\xF0'
            )
            return header + payload
        return payload


def _decode_callsign(addr: bytes) -> dict[str, Any]:
    """Decode one 7-byte AX.25 address field into callsign metadata."""
    if len(addr) != 7:
        raise ValueError(f"AX.25 address must be 7 bytes, got {len(addr)}")
    call = ''.join(chr((b >> 1) & 0x7F) for b in addr[:6]).rstrip()
    ssid_byte = addr[6]
    return {
        "callsign": call,
        "ssid": (ssid_byte >> 1) & 0x0F,
        "last": bool(ssid_byte & 0x01),
        "reserved": (ssid_byte >> 5) & 0x03,
        "raw_ssid_byte": f"0x{ssid_byte:02X}",
    }


def ax25_decode_header(frame: bytes) -> dict[str, Any]:
    """Decode a 16-byte AX.25 UI frame header into structured fields.

    Expects [dest 7B][src 7B][control 1B][pid 1B] and returns decoded
    destination/source callsigns plus control/PID metadata.
    """
    if len(frame) < AX25Config.HEADER_LEN:
        raise ValueError(
            f"AX.25 UI frame header requires {AX25Config.HEADER_LEN} bytes, got {len(frame)}"
        )
    header = frame[:AX25Config.HEADER_LEN]
    dest = _decode_callsign(header[:7])
    src = _decode_callsign(header[7:14])
    return {
        "dest": dest,
        "src": src,
        "control": header[14],
        "pid": header[15],
        "control_hex": f"0x{header[14]:02X}",
        "pid_hex": f"0x{header[15]:02X}",
        "raw_header": header.hex(" "),
    }


# =============================================================================
#  AX.25 OVER-THE-AIR ENCODER
#
#  Replicates the GNU Radio AX.25 TX chain:
#    HDLC framer -> G3RUH scrambler -> NRZI encoder -> bit packing
# =============================================================================

# -- Tunables ----------------------------------------------------------------

PREAMBLE_FLAGS  = 20       # Number of 0x7E flag bytes before frame
POSTAMBLE_FLAGS = 20       # Number of 0x7E flag bytes after frame

G3RUH_MASK      = 0x21    # Feedback tap mask (x^17 + x^12 + 1)
G3RUH_REG_LEN   = 16      # Shift register length (17 effective bits: 0-16)
G3RUH_SEED      = 0x00000 # Initial register state

NRZI_INIT       = 0       # NRZI encoder initial output state


# -- CRC-16-CCITT (HDLC FCS) -------------------------------------------------

def _crc_ccitt(data: bytes) -> int:
    """CRC-16-CCITT as used in HDLC/X.25 FCS.

    Init: 0xFFFF, reflected polynomial 0x8408, final XOR 0xFFFF.
    Returns 16-bit CRC value."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc ^ 0xFFFF


# -- HDLC Framing -------------------------------------------------------------

_FLAG_BITS = [0, 1, 1, 1, 1, 1, 1, 0]   # 0x7E LSB first


def _bytes_to_bits_lsb(data: bytes) -> list[int]:
    """Convert bytes to bit list, LSB first per byte."""
    bits: list[int] = []
    for byte in data:
        for i in range(8):
            bits.append((byte >> i) & 1)
    return bits


def _bit_stuff(bits: list[int]) -> list[int]:
    """Insert a 0 bit after every run of five consecutive 1s."""
    out: list[int] = []
    ones = 0
    for b in bits:
        out.append(b)
        if b == 1:
            ones += 1
            if ones == 5:
                out.append(0)
                ones = 0
        else:
            ones = 0
    return out


def _hdlc_frame(payload: bytes) -> list[int]:
    """HDLC-frame a payload, matching gr-satellites hdlc_framer.

    Returns a list of bits (0/1 ints): preamble flags + bit-stuffed
    (payload + FCS) + postamble flags."""
    fcs = _crc_ccitt(payload)
    frame_data = payload + fcs.to_bytes(2, 'little')

    data_bits = _bytes_to_bits_lsb(frame_data)
    stuffed = _bit_stuff(data_bits)

    preamble = _FLAG_BITS * PREAMBLE_FLAGS
    postamble = _FLAG_BITS * POSTAMBLE_FLAGS
    return preamble + stuffed + postamble


# -- Top-Level Builder ---------------------------------------------------------

def build_ax25_gfsk_frame(ax25_packet: bytes) -> bytes:
    """Build complete AX.25 over-the-air frame from an AX.25-wrapped CSP packet.

    Replicates the GNU Radio AX.25 TX chain in a single fused pass:
        HDLC framer -> G3RUH scrambler -> NRZI encoder -> MSB bit packing

    G3RUH scrambler: polynomial x^17 + x^12 + 1 (GNU Radio Fibonacci LFSR).
    NRZI encoder: 0 -> toggle, 1 -> hold.
    Bit packing: MSB first, 8 bits per byte (matches pack_k_bits_bb(8)).

    Input:  AX.25 packet (header + CSP payload) from AX25Config.wrap().
    Output: Frame bytes ready for GFSK mod with do_unpack=True."""
    bits = _hdlc_frame(ax25_packet)

    reg = G3RUH_SEED; mask = G3RUH_MASK; reg_len = G3RUH_REG_LEN
    nrzi_state = NRZI_INIT
    out = bytearray(); byte_acc = 0; bit_count = 0

    for b in bits:
        # G3RUH scrambler
        output = reg & 1
        newbit = ((reg & mask).bit_count() & 1) ^ (b & 1)
        reg = (reg >> 1) | (newbit << reg_len)
        # NRZI encoder
        if output == 0:
            nrzi_state ^= 1
        # MSB-first bit packing
        byte_acc = (byte_acc << 1) | nrzi_state
        bit_count += 1
        if bit_count == 8:
            out.append(byte_acc); byte_acc = 0; bit_count = 0

    return bytes(out)


class Ax25Framer(Framer):
    """`Framer` adapter — UI header wrap then HDLC/G3RUH/NRZI bitstream."""

    frame_label = "AX.25"

    __slots__ = ("config",)

    def __init__(self, config: AX25Config) -> None:
        self.config = config

    def frame(self, payload: bytes) -> bytes:
        return build_ax25_gfsk_frame(self.config.wrap(payload))

    def overhead(self) -> int:
        return self.config.overhead()

    def max_payload(self) -> int | None:
        return None

    def log_fields(self) -> dict[str, Any]:
        if not self.config.enabled:
            return {}
        c = self.config
        # TODO: AX.25 e2e test
        return {"facts": {"protocol": {"ax25_header": {
            "src_call": c.src_call, "src_ssid": int(c.src_ssid),
            "dest_call": c.dest_call, "dest_ssid": int(c.dest_ssid),
        }}}}

    def log_line(self) -> str | None:
        if not self.config.enabled:
            return None
        c = self.config
        return f"  AX.25      Src:{c.src_call}-{c.src_ssid}  Dest:{c.dest_call}-{c.dest_ssid}"
