"""ROADS housekeeping beacon decoder.

Decodes the on-air ROADS full-table housekeeping beacon (type 0x66),
reverse-engineered from the first frame received 2026-07-11 (ROADS 2,
CSP CRC-32C verified; that frame is the golden fixture in
tests/test_missions_ax100_rx.py). UND's published "IARU Telemetry
Decoding Format" (aero.und.edu, fetched 2026-07-11) describes the field
set but accounts an 8-byte checksum/timestamp/source wrapper per
element; on the wire the wrapper amortizes per sample GROUP, so the
whole 42-field table rides one 156-byte AX100 Mode 5 frame:

    5-byte header: protocol_version=1, beacon type=0x66, version, satid u16
    6 wrapped groups, each ">HIH" (table checksum, unix timestamp,
    source node) followed by that group's big-endian values:
        obc main 15B | obc deploy 4B | gnss 12B | eps 32B | uhf 18B | vhf 18B
    CSP CRC-32C (big-endian, over everything before it) when the CSP
    header carries flags bit 0 — as observed on air.

The radio groups are 18 bytes, not the document's 20: the tail resolves
as boot_count u16 + boot_cause u32 (the u32-count split yields absurd
counts, and this split gave both radios the same 0x100 cause on the
observed frame). Sample timestamps re-emit once per domain — the obc
deploy group shares obc's — and the radio reboot causes emit as
lossless hex.

Token order MUST match the beacon_hk container entry list in
mission.yml (guarded by test_roads_yml_containers_match_field_table).
"""

from __future__ import annotations

import struct
from datetime import datetime, timezone

from mav_gss_lib.missions.ax100_rx import HkDecode
from mav_gss_lib.platform.framing.crc import crc32c


_HEADER = struct.Struct(">BBBH")        # protocol_version, type, version, satid
_GROUP_HEADER = struct.Struct(">HIH")   # table checksum, unix timestamp, source node

PROTOCOL_VERSION = 1
FULL_HK_BEACON_TYPE = 0x66
CSP_FLAG_CRC32 = 0x01

# (domain, ((parameter key, value format, render), ...)) per wrapped sample
# group, in wire order. obc arrives as two groups: main sample + deploy flags.
_GROUPS = (
    ("obc", (
        ("ram_image", "B", "int"),
        ("temp_mcu", "h", "int"),
        ("temp_ram", "h", "int"),
        ("resetcause", "I", "int"),
        ("obc_bootcause", "I", "int"),
        ("bootcount", "H", "int"),
    )),
    ("obc", (
        ("depl_isis_a", "B", "int"),
        ("depl_a_isis_a", "B", "int"),
        ("depl_isis_b", "B", "int"),
        ("depl_a_isis_b", "B", "int"),
    )),
    ("gnss", (
        ("error_word", "I", "int"),
        ("nr_stats", "I", "int"),
        ("rxstat", "I", "int"),
    )),
    ("eps", (
        ("vboost1", "H", "int"),
        ("vboost2", "H", "int"),
        ("vboost3", "H", "int"),
        ("vbatt", "H", "int"),
        ("curout1", "H", "int"),
        ("curout2", "H", "int"),
        ("curout3", "H", "int"),
        ("curout4", "H", "int"),
        ("curout5", "H", "int"),
        ("curout6", "H", "int"),
        ("curin1", "H", "int"),
        ("curin2", "H", "int"),
        ("curin3", "H", "int"),
        ("cursun", "H", "int"),
        ("cursys", "H", "int"),
        ("battmode", "B", "int"),
        ("eps_bootcause", "B", "int"),
    )),
    ("uhf", (
        ("uhf_temp_brd", "H", "int"),
        ("uhf_temp_pa", "H", "int"),
        ("uhf_tx_count", "I", "int"),
        ("uhf_rx_count", "I", "int"),
        ("uhf_boot_count", "H", "int"),
        ("uhf_boot_cause", "I", "hex"),
    )),
    ("vhf", (
        ("vhf_temp_brd", "H", "int"),
        ("vhf_temp_pa", "H", "int"),
        ("vhf_tx_count", "I", "int"),
        ("vhf_rx_count", "I", "int"),
        ("vhf_boot_count", "H", "int"),
        ("vhf_boot_cause", "I", "hex"),
    )),
)

MODE5_INNER_CAP = 223   # one RS(255,223) codeword — max inner CSP packet
_CRC_SIZE = 4


def _group_values_size(fields) -> int:
    return sum(struct.calcsize(">" + fmt) for _, fmt, _ in fields)


BEACON_SIZE = _HEADER.size + sum(
    _GROUP_HEADER.size + _group_values_size(fields) for _, fields in _GROUPS
)                                                                     # 152
TOKEN_COUNT = (sum(len(fields) for _, fields in _GROUPS)
               + len({domain for domain, _ in _GROUPS}))              # 47


def token_names() -> tuple[str, ...]:
    """Container entry names in emission order (domain ts, then values)."""
    names: list[str] = []
    domain = None
    for group_domain, fields in _GROUPS:
        if group_domain != domain:
            domain = group_domain
            names.append(f"{group_domain}_ts")
        names.extend(key for key, _fmt, _render in fields)
    return tuple(names)


def _iso(unix_s: int) -> str:
    return datetime.fromtimestamp(unix_s, timezone.utc).isoformat(timespec="seconds")


def decode_beacon(csp_header: dict, payload: bytes) -> HkDecode | None:
    """Decode a ROADS type-0x66 beacon payload (bytes after the CSP header).

    Returns None when the frame is not a full-table HK beacon (wrong
    protocol / type / length) or its CSP CRC-32C fails — the shared
    PacketOps then logs the frame raw as opaque telemetry.
    """
    body = payload
    if csp_header.get("flags", 0) & CSP_FLAG_CRC32:
        if len(body) <= _CRC_SIZE:
            return None
        received = int.from_bytes(body[-_CRC_SIZE:], "big")
        body = body[:-_CRC_SIZE]
        if crc32c(body) != received:
            return None
    if len(body) != BEACON_SIZE:
        return None
    protocol_version, beacon_type, beacon_version, satid = _HEADER.unpack_from(body, 0)
    if protocol_version != PROTOCOL_VERSION or beacon_type != FULL_HK_BEACON_TYPE:
        return None

    tokens: list[str] = []
    values: dict[str, int] = {}
    offset = _HEADER.size
    domain = None
    for group_domain, fields in _GROUPS:
        _checksum, sample_ts, _source = _GROUP_HEADER.unpack_from(body, offset)
        offset += _GROUP_HEADER.size
        if group_domain != domain:
            domain = group_domain
            tokens.append(_iso(sample_ts))
        for key, fmt, render in fields:
            (value,) = struct.unpack_from(">" + fmt, body, offset)
            offset += struct.calcsize(">" + fmt)
            tokens.append(f"0x{value:08x}" if render == "hex" else str(value))
            values[key] = value

    facts = {
        "kind": "hk",
        "satid": satid,
        "beacon_type": beacon_type,
        "beacon_version": beacon_version,
        "vbat_mv": values["vbatt"],
        "batt_mode": values["battmode"],
    }
    return HkDecode(
        container_kind="hk",
        tokens=" ".join(tokens).encode("ascii"),
        facts=facts,
    )
