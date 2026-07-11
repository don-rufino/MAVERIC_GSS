"""AISTECHSAT-2 housekeeping telemetry decoder.

Ports the gr-satellites `satellites.telemetry.lume` construct stack
(shared by LUME-1 and AISTECHSAT-2). After the CSP header the frame is a
5-byte CCSDS TM transfer-frame header, 6-byte Space Packet primary header,
7-byte ECSS PUS TM secondary header, and a 6-byte day/milliseconds time
field; a u16 payload id then selects one of five big-endian housekeeping
tables (OBC / EPS / TTC+GSSB / AOCS / TEMPS) closed by the space-packet
PEC and TM tail. Unknown payload ids stay opaque telemetry.

`FIELDS` is the machine-readable port of the five construct tables: wire
order, struct format, element count, token rendering, and the mission.yml
parameter type. The mission.yml parameters and container entry lists are
generated from this table — token order MUST keep matching it (guarded by
`test_aistechsat2_yml_containers_match_field_table`). LinearAdapter(10)
temperatures render as one-decimal floats; u8 status arrays are lossless
hex; the OBC software-version string is whitespace-sanitized into a single
token.
"""

from __future__ import annotations

import re
import struct
from datetime import datetime, timezone

from mav_gss_lib.missions.ax100_rx import HkDecode


_TM_HEADER_SIZE = 5     # CCSDS TM transfer-frame header (bitfields, not consumed)
_SP_HEADER_SIZE = 6     # Space Packet primary header
_PUS_HEADER_SIZE = 7    # ECSS PUS TM secondary header
_TIME_FIELD = struct.Struct(">HI")   # day, milliseconds of day
_PAYLOAD_ID = struct.Struct(">H")
_TAIL_SIZE = 8          # space-packet PEC (u16) + TM tail (3 x u16)

_TIME_OFFSET = _TM_HEADER_SIZE + _SP_HEADER_SIZE + _PUS_HEADER_SIZE       # 18
_PAYLOAD_ID_OFFSET = _TIME_OFFSET + _TIME_FIELD.size                      # 24
_DATA_OFFSET = _PAYLOAD_ID_OFFSET + _PAYLOAD_ID.size                      # 26

# (name, struct format, count, render, mission.yml type)
# render: "int" plain integer | "d10" i16/10 one-decimal degC | "f" float32
#         | "hex" u8-array lossless hex | "str" sanitized ascii | "iso" unix
#         | "skip" consume without a token
_OBC_FIELDS = (
    ("boot_cause", "I", 1, "int", "count_l"),
    ("boot_count", "H", 1, "int", "count_i"),
    ("clock", "I", 1, "iso", "utc_token"),
    ("curr_flash", "H", 1, "int", "ma_i"),
    ("fs_mounted", "B", 1, "int", "mode_i"),
    ("ram_image", "b", 1, "int", "mode_i"),
    ("temp", "h", 2, "d10", "degc_f"),
    ("ticks", "I", 1, "int", "count_l"),
    ("mag", "f", 3, "f", "float_f"),
    ("memfree", "I", 1, "int", "bytes_l"),
    ("bufferfree", "I", 1, "int", "bytes_l"),
    ("uptime", "I", 1, "int", "sec_l"),
    ("gyro", "f", 3, "f", "float_f"),
    ("gyro_temp", "f", 1, "f", "degc_f"),
    ("flash_total", "Q", 1, "int", "bytes_l"),
    ("flash_used", "Q", 1, "int", "bytes_l"),
    ("flash_free", "Q", 1, "int", "bytes_l"),
    ("gpio_test", "B", 1, "int", "mode_i"),
    ("gpio_sw", "B", 1, "int", "mode_i"),
    ("gpio_pwr", "B", 1, "int", "mode_i"),
    ("om_state", "B", 1, "int", "mode_i"),
    ("om_sw_version", "32s", 1, "str", "str_token"),
    ("op_tr_conn", "B", 1, "int", "mode_i"),
    ("op_tr_conn_active", "B", 1, "int", "mode_i"),
)

_EPS_FIELDS = (
    ("output_off_delta", "H", 8, "int", "sec_i"),
    ("output_on_delta", "H", 8, "int", "sec_i"),
    ("wdt_csp_pings_left", "B", 2, "int", "count_i"),
    ("bootcause", "B", 1, "int", "mode_i"),
    ("cursun", "H", 1, "int", "ma_i"),
    ("curin", "H", 3, "int", "ma_i"),
    ("curout", "H", 6, "int", "ma_i"),
    ("cursys", "H", 1, "int", "ma_i"),
    ("temp", "H", 6, "int", "degc_i"),
    ("battmode", "B", 1, "int", "mode_i"),
    ("pptmode", "B", 1, "int", "mode_i"),
    ("counter_boot", "I", 1, "int", "count_l"),
    ("latchup", "H", 6, "int", "count_i"),
    ("counter_wdt_csp", "I", 2, "int", "count_l"),
    ("counter_wdt_gnd", "I", 1, "int", "count_l"),
    ("counter_wdt_i2c", "I", 1, "int", "count_l"),
    ("output", "B", 8, "hex", "hex_token"),
    ("wdt_gnd_time_left", "I", 1, "int", "sec_l"),
    ("wdt_i2c_time_left", "I", 1, "int", "sec_l"),
    ("vbatt", "H", 1, "int", "mv_i"),
    ("vboost", "H", 3, "int", "mv_i"),
    ("wdtcspc", "B", 2, "int", "count_i"),
)

_TTC_FIELDS = (
    ("gssb1_reboots", "B", 1, "int", "count_i"),
    ("gssb1_state", "B", 1, "int", "mode_i"),
    ("gssb1_antenna", "B", 1, "int", "mode_i"),
    ("gssb1_attempts", "H", 1, "int", "count_i"),
    ("gssb2_reboots", "B", 1, "int", "count_i"),
    ("gssb2_state", "B", 1, "int", "mode_i"),
    ("gssb2_antenna", "B", 1, "int", "mode_i"),
    ("gssb2_attempts", "H", 1, "int", "count_i"),
    ("gssb3_reboots", "B", 1, "int", "count_i"),
    ("gssb3_state", "B", 1, "int", "mode_i"),
    ("gssb3_antenna", "B", 1, "int", "mode_i"),
    ("gssb3_attempts", "H", 1, "int", "count_i"),
    ("gssb4_reboots", "B", 1, "int", "count_i"),
    ("gssb4_state", "B", 1, "int", "mode_i"),
    ("gssb4_antenna", "B", 1, "int", "mode_i"),
    ("gssb4_attempts", "H", 1, "int", "count_i"),
    ("temp_brd", "h", 1, "d10", "degc_f"),
    ("last_rferr", "h", 1, "int", "hz_i"),
    ("last_rssi", "h", 1, "int", "dbm_i"),
    ("tot_rx_bytes", "I", 1, "int", "bytes_l"),
    ("rx_bytes", "I", 1, "int", "bytes_l"),
    ("tot_rx_count", "I", 1, "int", "count_l"),
    ("rx_count", "I", 1, "int", "count_l"),
    ("tot_tx_bytes", "I", 1, "int", "bytes_l"),
    ("tx_bytes", "I", 1, "int", "bytes_l"),
    ("tot_tx_count", "I", 1, "int", "count_l"),
    ("tx_count", "I", 1, "int", "count_l"),
    ("temp_pa", "h", 1, "d10", "degc_f"),
    ("boot_cause", "I", 1, "int", "count_l"),
    ("bgnd_rssi", "h", 1, "int", "dbm_i"),
    ("active_conf", "B", 1, "int", "mode_i"),
    ("boot_count", "H", 1, "int", "count_i"),
    ("last_contact", "I", 1, "iso", "utc_token"),
    ("tx_duty", "B", 1, "int", "pct_i"),
)

_AOCS_FIELDS = (
    ("extmag_valid", "B", 1, "int", "mode_i"),
    ("extmag", "f", 3, "f", "float_f"),
    ("gps_pos_dev", "f", 3, "f", "float_f"),
    ("gps_pos", "f", 3, "f", "float_f"),
    ("gps_valid", "B", 1, "int", "mode_i"),
    ("gyro_valid", "B", 1, "int", "mode_i"),
    ("gyro", "f", 3, "f", "float_f"),
    ("mag", "f", 3, "f", "float_f"),
    ("mag_valid", "B", 1, "int", "mode_i"),
    ("status_run", "b", 1, "int", "mode_i"),
    ("acs_mode", "b", 1, "int", "mode_i"),
    ("ads_mode", "b", 1, "int", "mode_i"),
    ("ephem_mode", "b", 1, "int", "mode_i"),
    ("bdot_detumb", "B", 1, "int", "mode_i"),
    ("boot_cause", "I", 1, "int", "count_l"),
    ("boot_count", "H", 1, "int", "count_i"),
    ("cur_gssb", "H", 2, "int", "ma_i"),
    ("cur_pwm", "H", 1, "int", "ma_i"),
    ("cur_gps", "H", 1, "int", "ma_i"),
    ("cur_wde", "H", 1, "int", "ma_i"),
)

_TEMPS_FIELDS = (
    ("aocs_suns", "f", 5, "f", "degc_f"),
    ("spare1", "f", 1, "skip", None),
    ("aocs_extmag", "f", 1, "f", "degc_f"),
    ("aocs_fss", "f", 5, "f", "degc_f"),
    ("spare2", "f", 3, "skip", None),
    ("aocs_gyro", "f", 1, "f", "degc_f"),
    ("aocs", "h", 2, "d10", "degc_f"),
    ("eps", "h", 6, "int", "degc_i"),
    ("obc", "h", 2, "d10", "degc_f"),
    ("obc_gyro", "f", 1, "f", "degc_f"),
    ("ttc_brd", "h", 1, "d10", "degc_f"),
    ("ttc_pa", "h", 1, "d10", "degc_f"),
)

PAYLOADS = {
    1: ("obc", _OBC_FIELDS),
    2: ("eps", _EPS_FIELDS),
    3: ("ttc", _TTC_FIELDS),
    4: ("aocs", _AOCS_FIELDS),
    5: ("temps", _TEMPS_FIELDS),
}


def table_size(fields) -> int:
    return sum(struct.calcsize(">" + fmt) * count if fmt != "32s" else 32
               for _, fmt, count, _, _ in fields)


def token_count(payload_id: int) -> int:
    _, fields = PAYLOADS[payload_id]
    total = 2   # pus_day, pus_ms
    for _, _, count, render, _ in fields:
        if render == "skip":
            continue
        total += 1 if render in ("hex", "str") else count
    return total


def _iso(unix_s: int) -> str:
    return datetime.fromtimestamp(unix_s, timezone.utc).isoformat(timespec="seconds")


def _sanitize(raw: bytes) -> str:
    text = raw.decode("ascii", "replace").replace("�", "").strip("\x00").strip()
    return re.sub(r"\s+", "_", text) or "-"


_FACT_KEYS = {
    ("obc", "boot_count"): "boot_count",
    ("obc", "uptime"): "uptime_s",
    ("eps", "vbatt"): "vbat_mv",
    ("eps", "battmode"): "batt_mode",
    ("ttc", "last_rssi"): "rssi_dbm",
    ("ttc", "last_rferr"): "rferr_hz",
    ("aocs", "acs_mode"): "acs_mode",
    ("aocs", "ads_mode"): "ads_mode",
    ("temps", "ttc_pa"): "ttc_pa_degc",
}


def decode_beacon(csp_header: dict, payload: bytes) -> HkDecode | None:
    """Decode an AISTECHSAT-2 PUS housekeeping frame (bytes after CSP).

    Returns None when the payload id is unknown or the frame is short —
    the shared PacketOps then logs the frame raw as opaque telemetry
    (Aistech's undocumented "custom telemetry" frames stay opaque).
    """
    if len(payload) < _DATA_OFFSET + _TAIL_SIZE:
        return None
    (payload_id,) = _PAYLOAD_ID.unpack_from(payload, _PAYLOAD_ID_OFFSET)
    entry = PAYLOADS.get(payload_id)
    if entry is None:
        return None
    kind, fields = entry
    expected = _DATA_OFFSET + table_size(fields) + _TAIL_SIZE
    if len(payload) < expected:
        return None

    day, ms = _TIME_FIELD.unpack_from(payload, _TIME_OFFSET)
    tokens: list[str] = [str(day), str(ms)]
    facts: dict = {"kind": kind}
    offset = _DATA_OFFSET
    for name, fmt, count, render, _type in fields:
        width = struct.calcsize(">" + fmt)
        if render == "skip":
            offset += width * count
            continue
        if render == "hex":
            raw = payload[offset:offset + width * count]
            offset += width * count
            tokens.append(f"0x{raw.hex()}")
            continue
        if render == "str":
            tokens.append(_sanitize(payload[offset:offset + width]))
            offset += width
            continue
        values = struct.unpack_from(">" + fmt * count, payload, offset)
        offset += width * count
        for value in values:
            if render == "d10":
                tokens.append(f"{value / 10:.1f}")
            elif render == "f":
                tokens.append(f"{value:.6g}")
            elif render == "iso":
                tokens.append(_iso(value))
            else:
                tokens.append(str(value))
        fact_key = _FACT_KEYS.get((kind, name))
        if fact_key is not None:
            fact_value = values[0]
            facts[fact_key] = round(fact_value / 10, 1) if render == "d10" else fact_value

    extra = len(payload) - expected
    return HkDecode(
        container_kind=kind,
        tokens=" ".join(tokens).encode("ascii"),
        facts=facts,
        warnings=(f"{extra} trailing bytes after the TM tail",) if extra > 0 else (),
    )
