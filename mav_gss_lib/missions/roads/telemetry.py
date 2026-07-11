"""ROADS housekeeping beacon decoder.

Ports UND's published "IARU Telemetry Decoding Format" (aero.und.edu,
fetched 2026-07-11) onto the ascii_tokens walker. The document lays out
the full logical HK table: a 5-byte header (protocol_version, beacon
type, beacon version, satid) followed by 42 elements. Each element is a
GomSpace parameter-table sample — checksum u16 + unix timestamp u32 +
source node u16 + value — which is why the doc's per-field "size" column
reads 8 + the value width. All integers are big-endian (GomSpace
network-order convention; the doc states none).

The full table is 444 bytes, which cannot ride a single AX100 Mode 5
frame: the data field is one RS(255,223) codeword, so the inner CSP
packet caps at 223 bytes. On air the table must therefore arrive as
smaller typed beacons. Absent a published type map, the decoder matches a
frame by exact length against every contiguous run of whole subsystem
blocks that fits the cap (plus the full table, for offline reassembled
use). A 68-byte block run is length-ambiguous between the identically
shaped uhf and vhf blocks and is assumed uhf with a warning. Each block's
first element re-emits its sample timestamp as a token; the "x32" radio
reboot causes emit as lossless hex.

Token order MUST match the per-run container entry lists in mission.yml
(generated from this table, guarded by
test_roads_yml_containers_match_runs).
"""

from __future__ import annotations

import struct
from datetime import datetime, timezone

from mav_gss_lib.missions.ax100_rx import HkDecode


_HEADER = struct.Struct(">BBBH")          # protocol_version, type, version, satid
_ELEMENT_HEADER = struct.Struct(">HIH")   # table checksum, unix timestamp, source node

PROTOCOL_VERSION = 1

# (domain, parameter key, value format, render) in wire order.
_FIELDS = (
    ("obc", "ram_image", "B", "int"),
    ("obc", "temp_mcu", "h", "int"),
    ("obc", "temp_ram", "h", "int"),
    ("obc", "resetcause", "I", "int"),
    ("obc", "obc_bootcause", "I", "int"),
    ("obc", "bootcount", "H", "int"),
    ("obc", "depl_isis_a", "B", "int"),
    ("obc", "depl_a_isis_a", "B", "int"),
    ("obc", "depl_isis_b", "B", "int"),
    ("obc", "depl_a_isis_b", "B", "int"),
    ("gnss", "error_word", "I", "int"),
    ("gnss", "nr_stats", "I", "int"),
    ("gnss", "rxstat", "I", "int"),
    ("eps", "vboost1", "H", "int"),
    ("eps", "vboost2", "H", "int"),
    ("eps", "vboost3", "H", "int"),
    ("eps", "vbatt", "H", "int"),
    ("eps", "curout1", "H", "int"),
    ("eps", "curout2", "H", "int"),
    ("eps", "curout3", "H", "int"),
    ("eps", "curout4", "H", "int"),
    ("eps", "curout5", "H", "int"),
    ("eps", "curout6", "H", "int"),
    ("eps", "curin1", "H", "int"),
    ("eps", "curin2", "H", "int"),
    ("eps", "curin3", "H", "int"),
    ("eps", "cursun", "H", "int"),
    ("eps", "cursys", "H", "int"),
    ("eps", "battmode", "B", "int"),
    ("eps", "eps_bootcause", "B", "int"),
    ("uhf", "uhf_temp_brd", "H", "int"),
    ("uhf", "uhf_temp_pa", "H", "int"),
    ("uhf", "uhf_tx_count", "I", "int"),
    ("uhf", "uhf_rx_count", "I", "int"),
    ("uhf", "uhf_boot_count", "I", "int"),
    ("uhf", "uhf_boot_cause", "I", "hex"),
    ("vhf", "vhf_temp_brd", "H", "int"),
    ("vhf", "vhf_temp_pa", "H", "int"),
    ("vhf", "vhf_tx_count", "I", "int"),
    ("vhf", "vhf_rx_count", "I", "int"),
    ("vhf", "vhf_boot_count", "I", "int"),
    ("vhf", "vhf_boot_cause", "I", "hex"),
)

_BLOCK_ORDER = ("obc", "gnss", "eps", "uhf", "vhf")
_BLOCK_FIELDS = {
    block: tuple(f for f in _FIELDS if f[0] == block) for block in _BLOCK_ORDER
}

MODE5_INNER_CAP = 223   # one RS(255,223) codeword — max inner CSP packet


def _fields_size(fields) -> int:
    return sum(_ELEMENT_HEADER.size + struct.calcsize(">" + fmt)
               for _, _, fmt, _ in fields)


def _run_fields(run: tuple[str, ...]):
    return tuple(f for block in run for f in _BLOCK_FIELDS[block])


def run_kind(run: tuple[str, ...]) -> str:
    return "hk" if run == _BLOCK_ORDER else "hk_" + "_".join(run)


def run_size(run: tuple[str, ...]) -> int:
    return _HEADER.size + _fields_size(_run_fields(run))


def run_token_count(run: tuple[str, ...]) -> int:
    return len(_run_fields(run)) + len(run)


def _contiguous_runs():
    runs = [_BLOCK_ORDER]   # full table first: exact 444 wins over any slice
    for start in range(len(_BLOCK_ORDER)):
        for stop in range(start + 1, len(_BLOCK_ORDER) + 1):
            run = _BLOCK_ORDER[start:stop]
            if run != _BLOCK_ORDER and run_size(run) <= MODE5_INNER_CAP:
                runs.append(run)
    return tuple(runs)


# Full table plus every contiguous block run that fits one Mode 5 frame.
# uhf precedes vhf, so a length-ambiguous 68-byte block resolves to uhf.
SUPPORTED_RUNS = _contiguous_runs()

BEACON_SIZE = run_size(_BLOCK_ORDER)                 # 444
TOKEN_COUNT = run_token_count(_BLOCK_ORDER)          # 47


def _iso(unix_s: int) -> str:
    return datetime.fromtimestamp(unix_s, timezone.utc).isoformat(timespec="seconds")


def decode_beacon(csp_header: dict, payload: bytes) -> HkDecode | None:
    """Decode a ROADS beacon payload (bytes after the CSP header).

    Returns None when the payload length matches no supported block run
    or the protocol version is wrong — the shared PacketOps then logs the
    frame raw as opaque telemetry.
    """
    if len(payload) < _HEADER.size:
        return None
    protocol_version, beacon_type, beacon_version, satid = _HEADER.unpack_from(payload, 0)
    if protocol_version != PROTOCOL_VERSION:
        return None
    run = next((r for r in SUPPORTED_RUNS if run_size(r) == len(payload)), None)
    if run is None:
        return None

    warnings: list[str] = []
    if run == ("uhf",):
        warnings.append("68-byte block is length-ambiguous (uhf/vhf) — assumed uhf")

    tokens: list[str] = []
    values: dict[str, int] = {}
    offset = _HEADER.size
    domain = None
    for field_domain, key, fmt, render in _run_fields(run):
        _checksum, sample_ts, _source = _ELEMENT_HEADER.unpack_from(payload, offset)
        offset += _ELEMENT_HEADER.size
        if field_domain != domain:
            domain = field_domain
            tokens.append(_iso(sample_ts))
        (value,) = struct.unpack_from(">" + fmt, payload, offset)
        offset += struct.calcsize(">" + fmt)
        tokens.append(f"0x{value:08x}" if render == "hex" else str(value))
        values[key] = value

    facts = {
        "kind": run_kind(run),
        "blocks": "+".join(run),
        "satid": satid,
        "beacon_type": beacon_type,
        "beacon_version": beacon_version,
    }
    if "vbatt" in values:
        facts["vbat_mv"] = values["vbatt"]
        facts["batt_mode"] = values["battmode"]
    return HkDecode(
        container_kind=run_kind(run),
        tokens=" ".join(tokens).encode("ascii"),
        facts=facts,
        warnings=tuple(warnings),
    )
