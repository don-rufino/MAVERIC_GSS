"""Suomi 100 housekeeping beacon decoder.

Ports the gr-satellites `satellites.telemetry.suomi100` construct layout
(GomSpace NanoMind-family EPS / COM / OBC blocks) onto the ascii_tokens
walker: two beacon types selected by the first payload byte after the CSP
header. All integers are big-endian; EPS electrical values follow the
GomSpace P31u convention (mV / mA / degC raw), COM carries AX100 radio
health (RSSI / frequency error), and the u8-array status fields (`out_val`,
`pwr`) are emitted as lossless hex strings rather than guessed bitmasks.

Token order MUST match the `beacon0` / `beacon1` container entry lists in
mission.yml.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timezone

from mav_gss_lib.missions.ax100_rx import HkDecode


_B0_EPS = struct.Struct(">I3HH7H3HHH6hB")
_B0_COM = struct.Struct(">I5h")
_B0_OBC = struct.Struct(">I6H2h")
_B1_EPS = struct.Struct(">8I2BB6H8BB")
_B1_COM = struct.Struct(">IB4IHIIIBII")
_B1_OBC = struct.Struct(">I6BHBHII")

BEACON0_SIZE = 1 + _B0_EPS.size + _B0_COM.size + _B0_OBC.size   # 84
BEACON1_SIZE = 1 + _B1_EPS.size + _B1_COM.size + _B1_OBC.size   # 124


@dataclass(frozen=True, slots=True)
class _Sections:
    eps: tuple
    com: tuple
    obc: tuple


def _iso(unix_s: int) -> str:
    return datetime.fromtimestamp(unix_s, timezone.utc).isoformat(timespec="seconds")


def _split(payload: bytes, eps: struct.Struct, com: struct.Struct,
           obc: struct.Struct) -> _Sections:
    o = 1
    e = eps.unpack_from(payload, o); o += eps.size
    c = com.unpack_from(payload, o); o += com.size
    b = obc.unpack_from(payload, o)
    return _Sections(eps=e, com=c, obc=b)


def _beacon0(payload: bytes) -> HkDecode:
    s = _split(payload, _B0_EPS, _B0_COM, _B0_OBC)
    eps_ts, pv1, pv2, pv3, vbat = s.eps[0:5]
    out_cur = s.eps[5:12]
    pv_cur = s.eps[12:15]
    batt_in, batt_out = s.eps[15:17]
    temps = s.eps[17:23]
    batt_mode = s.eps[23]
    com_ts, ct1, ct2, rssi, rferr, rssi_bgnd = s.com
    obc_ts = s.obc[0]
    obc_cur = s.obc[1:7]
    ot1, ot2 = s.obc[7:9]

    tokens = [
        _iso(eps_ts), pv1, pv2, pv3, vbat, *out_cur, *pv_cur,
        batt_in, batt_out, *temps, batt_mode,
        _iso(com_ts), ct1, ct2, rssi, rferr, rssi_bgnd,
        _iso(obc_ts), *obc_cur, ot1, ot2,
    ]
    facts = {"kind": "hk", "vbat_mv": vbat, "rssi_dbm": rssi, "rferr_hz": rferr}
    return HkDecode(
        container_kind="beacon0",
        tokens=" ".join(str(t) for t in tokens).encode("ascii"),
        facts=facts,
        warnings=_trailing_warning(payload, BEACON0_SIZE),
    )


def _beacon1(payload: bytes) -> HkDecode:
    s = _split(payload, _B1_EPS, _B1_COM, _B1_OBC)
    (eps_ts, wdt_i2c, wdt_gnd, eps_boots, wdt_i2c_count, wdt_gnd_count,
     csp_count_a, csp_count_b) = s.eps[0:8]
    wdt_csp_a, wdt_csp_b, eps_boot_cause = s.eps[8:11]
    latchup = s.eps[11:17]
    out_val = bytes(s.eps[17:25]).hex()
    ppt_mode = s.eps[25]
    (com_ts, tx_duty, total_tx_count, total_rx_count, total_tx_bytes,
     total_rx_bytes, com_boots, com_boot_cause, tx_bytes, rx_bytes,
     com_config, tx_count, rx_count) = s.com
    obc_ts = s.obc[0]
    pwr = bytes(s.obc[1:7]).hex()
    sw_count, filesystem, obc_boots, obc_boot_cause, obc_clock = s.obc[7:12]

    tokens = [
        _iso(eps_ts), wdt_i2c, wdt_gnd, eps_boots, wdt_i2c_count,
        wdt_gnd_count, csp_count_a, csp_count_b, wdt_csp_a, wdt_csp_b,
        eps_boot_cause, *latchup, f"0x{out_val}", ppt_mode,
        _iso(com_ts), tx_duty, total_tx_count, total_rx_count,
        total_tx_bytes, total_rx_bytes, com_boots, com_boot_cause,
        tx_bytes, rx_bytes, com_config, tx_count, rx_count,
        _iso(obc_ts), f"0x{pwr}", sw_count, filesystem, obc_boots,
        obc_boot_cause, _iso(obc_clock),
    ]
    facts = {
        "kind": "counters",
        "eps_boots": eps_boots,
        "tx_count": tx_count,
        "rx_count": rx_count,
    }
    return HkDecode(
        container_kind="beacon1",
        tokens=" ".join(str(t) for t in tokens).encode("ascii"),
        facts=facts,
        warnings=_trailing_warning(payload, BEACON1_SIZE),
    )


def _trailing_warning(payload: bytes, expected: int) -> tuple[str, ...]:
    extra = len(payload) - expected
    if extra > 0:
        return (f"{extra} trailing bytes after the beacon block",)
    return ()


def decode_beacon(csp_header: dict, payload: bytes) -> HkDecode | None:
    """Decode a Suomi 100 beacon payload (bytes after the CSP header).

    Returns None when the payload is not a recognizable beacon — the
    shared PacketOps then logs the frame raw as opaque telemetry.
    """
    if not payload:
        return None
    try:
        if payload[0] == 0x00 and len(payload) >= BEACON0_SIZE:
            return _beacon0(payload)
        if payload[0] == 0x01 and len(payload) >= BEACON1_SIZE:
            return _beacon1(payload)
    except (struct.error, ValueError, OverflowError, OSError):
        return None
    return None
