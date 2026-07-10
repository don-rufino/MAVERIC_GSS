"""CATSAT housekeeping beacon decoder.

Ports the community Kaitai definition (satnogs-decoders catsat.ksy) onto
the ascii_tokens walker: 18 beacon types across the GomSpace stack
(NanoMind OBC, BPX battery, P60/PDU/ACU power, AX100 radio HK, ADCS, and
the ASDR payload). Frame shape after the CSP header: protocol_version u1,
type u1, version u1, satid u2be, then the beacon body — all body integers
big-endian per the ksy meta.

The BEACONS field table below is machine-generated from the ksy (see the
codegen note in mission.yml); token order MUST match the container
entry_list order in mission.yml, which is generated from the same table.
GomSpace HK-table headers (checksum u2, timestamp u4, source u2) emit
only their timestamp, as an ISO token named by the ksy element id
(hk_<node>_<table>_<type>). Free-text fields emit lossless hex tokens
with the human text mirrored into mission facts.

Note: CATSAT's CSP header is little-endian on the wire. The ksy's
`destination` bit expression contains a shift typo (overlaps dst_port);
the shared Ax100RxPacketOps parses the header as a little-endian uint32
with the standard CSP v1 bitfield instead, which matches every other
field expression in the ksy.
"""

from __future__ import annotations

import re
import struct
from datetime import datetime, timezone
from typing import Any

from mav_gss_lib.missions.ax100_rx import HkDecode

FieldSpec = tuple[str, str, int, str]  # (name, struct fmt, count, kind)

_EH = struct.Struct(">HIH")  # checksum, timestamp, source — timestamp emitted


# Auto-generated from satnogs-decoders catsat.ksy — do not hand-edit field rows.
BEACONS: dict[int, tuple[str, tuple[FieldSpec, ...]]] = {
    0: ("motd", (
        ('hk_1_95', 'eh', 1, 'eh'),
        ('callsign', '8s', 1, 'str'),
        ('motd', '80s', 1, 'hexstr'),
    )),
    1: ("crit1", (
        ('hk_1_4_1', 'eh', 1, 'eh'),
        ('obc_temp_mcu', 'h', 1, 'int'),
        ('obc_boot_cnt', 'H', 1, 'int'),
        ('obc_clock', 'I', 1, 'long'),
        ('hk_1_91', 'eh', 1, 'eh'),
        ('bpx_vbatt', 'H', 1, 'int'),
        ('bpx_temp', 'h', 1, 'int'),
        ('bpx_boot_cnt', 'I', 1, 'long'),
        ('hk_5_4_1', 'eh', 1, 'eh'),
        ('ax100_temp_brd', 'h', 1, 'int'),
        ('ax100_boot_cnt', 'H', 1, 'int'),
        ('ax100_last_contact', 'I', 1, 'long'),
        ('hk_8_4_1', 'eh', 1, 'eh'),
        ('p60_boot_cnt', 'I', 1, 'long'),
        ('p60_batt_mode', 'B', 1, 'int'),
        ('p60_batt_v', 'H', 1, 'int'),
        ('p60_batt_c', 'h', 1, 'int'),
        ('hk_9_4', 'eh', 1, 'eh'),
        ('pdu_x2_cout', 'h', 9, 'int'),
    )),
    2: ("crit2", (
        ('hk_10_4_2', 'eh', 1, 'eh'),
        ('pdu_x3_cout', 'h', 9, 'int'),
        ('hk_11_4_2', 'eh', 1, 'eh'),
        ('acu_power', 'H', 6, 'int'),
        ('hk_4_4_2', 'eh', 1, 'eh'),
        ('adcs_boot_cnt', 'H', 1, 'int'),
        ('adcs_clock', 'I', 1, 'long'),
        ('hk_4_150_2', 'eh', 1, 'eh'),
        ('extgyro', 'f', 3, 'float'),
        ('gps_pos', 'f', 3, 'float'),
        ('gps_vel', 'f', 3, 'float'),
        ('hk_4_151_2', 'eh', 1, 'eh'),
        ('acs_mode', 'b', 1, 'int'),
        ('status_extmag', 'b', 1, 'int'),
        ('status_fss', 'b', 5, 'int'),
        ('status_extgyro', 'b', 1, 'int'),
        ('status_gps', 'b', 1, 'int'),
    )),
    3: ("obc", (
        ('hk_1_4_3', 'eh', 1, 'eh'),
        ('obc_fs_mnted', 'B', 1, 'int'),
        ('obc_temp_ram', 'h', 1, 'int'),
        ('obc_resetcause', 'I', 1, 'long'),
        ('obc_bootcause', 'I', 1, 'long'),
        ('obc_uptime', 'I', 1, 'long'),
        ('hk_1_91_3', 'eh', 1, 'eh'),
        ('batt_charge', 'H', 1, 'int'),
        ('batt_dcharge', 'H', 1, 'int'),
        ('batt_heater', 'H', 1, 'int'),
        ('batt_temp2', 'h', 1, 'int'),
        ('batt_temp3', 'h', 1, 'int'),
        ('batt_temp4', 'h', 1, 'int'),
        ('batt_bootcause', 'B', 1, 'int'),
        ('hk_1_94_3', 'eh', 1, 'eh'),
        ('sat_temps', 'f', 6, 'float'),
        ('hk_5_0_3', 'eh', 1, 'eh'),
        ('ax100_reboot_in', 'H', 1, 'int'),
        ('ax100_tx_inhibit', 'I', 1, 'long'),
        ('hk_5_1_3', 'eh', 1, 'eh'),
        ('ax100_rx_freq', 'I', 1, 'long'),
        ('ax100_rx_baud', 'I', 1, 'long'),
        ('hk_5_4_3', 'eh', 1, 'eh'),
        ('ax100_temp_pa', 'h', 1, 'int'),
        ('ax100_last_rssi', 'h', 1, 'int'),
        ('ax100_last_rferr', 'h', 1, 'int'),
        ('ax100_active_conf', 'B', 1, 'int'),
        ('ax100_bootcause', 'H', 1, 'int'),
        ('ax100_bgnd_rssi', 'h', 1, 'int'),
        ('ax100_tx_duty', 'B', 1, 'int'),
        ('hk_5_5_3', 'eh', 1, 'eh'),
        ('ax100_tx_freq', 'I', 1, 'long'),
        ('ax100_tx_baud', 'I', 1, 'long'),
    )),
    4: ("pdu1", (
        ('hk_8_4_4', 'eh', 1, 'eh'),
        ('p60_cout', 'h', 13, 'int'),
        ('p60_out_en', 'B', 13, 'int'),
        ('p60_temp', 'h', 2, 'int'),
        ('p60_bootcause', 'I', 1, 'long'),
        ('p60_uptime', 'I', 1, 'long'),
        ('p60_resetcause', 'H', 1, 'int'),
        ('p60_latchup', 'H', 13, 'int'),
        ('p60_vcc_c', 'h', 1, 'int'),
        ('p60_batt_v', 'H', 1, 'int'),
        ('p60_dearm_status', 'B', 1, 'int'),
        ('p60_wdt_cnt_gnd', 'I', 1, 'long'),
        ('p60_wdt_cnt_can', 'I', 1, 'long'),
        ('p60_wdt_cnt_left', 'I', 1, 'long'),
        ('p60_batt_chrg', 'h', 1, 'int'),
        ('p60_batt_dchrg', 'h', 1, 'int'),
        ('ant6_depl', 'b', 1, 'int'),
        ('ar6_depl', 'b', 1, 'int'),
        ('hk_9_4_4', 'eh', 1, 'eh'),
        ('pdu_x2_vout', 'h', 9, 'int'),
        ('pdu_x2_temp', 'h', 1, 'int'),
        ('pdu_x2_out_en', 'B', 9, 'int'),
        ('pdu_x2_bootcause', 'I', 1, 'long'),
        ('pdu_x2_boot_cnt', 'I', 1, 'long'),
        ('pdu_x2_uptime', 'I', 1, 'long'),
        ('pdu_x2_resetcause', 'H', 1, 'int'),
        ('pdu_x2_latchup', 'H', 9, 'int'),
    )),
    5: ("pdu2", (
        ('hk_10_4_5', 'eh', 1, 'eh'),
        ('pdu_x3_vout', 'h', 9, 'int'),
        ('pdu_x3_temp', 'h', 1, 'int'),
        ('pdu_x3_out_en', 'B', 9, 'int'),
        ('pdu_x3_bootcause', 'I', 1, 'long'),
        ('pdu_x3_boot_cnt', 'I', 1, 'long'),
        ('pdu_x3_uptime', 'I', 1, 'long'),
        ('pdu_x3_resetcause', 'H', 1, 'int'),
        ('pdu_x3_latchup', 'H', 9, 'int'),
        ('hk_11_4_5', 'eh', 1, 'eh'),
        ('acu_cin', 'h', 6, 'int'),
        ('acu_vin', 'H', 6, 'int'),
        ('acu_vbatt', 'H', 1, 'int'),
        ('acu_temp', 'h', 3, 'int'),
        ('acu_mppt_mode', 'B', 1, 'int'),
        ('acu_vboost', 'H', 6, 'int'),
        ('acu_bootcause', 'I', 1, 'long'),
        ('acu_boot_cnt', 'I', 1, 'long'),
        ('acu_uptime', 'I', 1, 'long'),
        ('acu_resetcause', 'H', 1, 'int'),
    )),
    6: ("dep", (
        ('hk_1_96_6', 'eh', 1, 'eh'),
        ('ant_1_brn', 'h', 1, 'int'),
        ('ant_2_brn', 'h', 1, 'int'),
        ('ant_3_brn', 'h', 1, 'int'),
        ('ant_4_brn', 'h', 1, 'int'),
        ('ant_1_rel', 'b', 1, 'int'),
        ('ant_2_rel', 'b', 1, 'int'),
        ('ant_3_rel', 'b', 1, 'int'),
        ('ant_4_rel', 'b', 1, 'int'),
        ('dsp_1_brn', 'h', 1, 'int'),
        ('dsp_2_brn', 'h', 1, 'int'),
        ('dsp_1_rel', 'b', 1, 'int'),
        ('dsp_2_rel', 'b', 1, 'int'),
    )),
    7: ("adcs0", (
        ('hk_4_150_7', 'eh', 1, 'eh'),
        ('extmag', 'f', 3, 'float'),
        ('torquer_duty', 'f', 3, 'float'),
        ('hk_4_151_7', 'eh', 1, 'eh'),
        ('bdot_rate', 'f', 2, 'float'),
        ('bdot_dmag', 'f', 3, 'float'),
        ('bdot_torquer', 'f', 3, 'float'),
        ('bdot_detumble', 'B', 1, 'int'),
        ('hk_4_154_7', 'eh', 1, 'eh'),
        ('ctrl_refq', 'f', 4, 'float'),
        ('ctrl_errq', 'f', 4, 'float'),
        ('ctrl_m', 'f', 3, 'float'),
        ('ctrl_mwspeed', 'f', 4, 'float'),
        ('ctrl_euleroff', 'f', 3, 'float'),
        ('ctrl_btorque', 'f', 3, 'float'),
    )),
    11: ("adcs1", (
        ('hk_4_150_11', 'eh', 1, 'eh'),
        ('extmag', 'f', 3, 'float'),
        ('extmag_temp', 'f', 1, 'float'),
        ('extmag_valid', 'B', 1, 'int'),
        ('suns', 'f', 6, 'float'),
        ('suns_valid', 'B', 1, 'int'),
        ('suns_temp', 'h', 6, 'int'),
        ('extgyro', 'f', 3, 'float'),
        ('extgyro_temp', 'f', 1, 'float'),
        ('extgyro_valid', 'B', 1, 'int'),
        ('fss', 'f', 16, 'float'),
        ('fss_temp', 'f', 1, 'float'),
        ('fss_valid', 'B', 5, 'int'),
        ('gps_pos', 'f', 3, 'float'),
        ('gps_vel', 'f', 3, 'float'),
        ('gps_epoch', 'I', 1, 'long'),
        ('gps_valid', 'B', 1, 'int'),
        ('gps_sat', 'B', 1, 'int'),
        ('gps_satsol', 'B', 1, 'int'),
        ('pps_unix', 'I', 1, 'long'),
    )),
    12: ("adcs2", (
        ('hk_4_150_12', 'eh', 1, 'eh'),
        ('wheel_torque', 'f', 4, 'float'),
        ('wheel_momentum', 'f', 4, 'float'),
        ('wheel_speed', 'f', 4, 'float'),
        ('wheel_enable', 'B', 4, 'int'),
        ('wheel_current', 'H', 4, 'int'),
        ('wheel_temp', 'h', 4, 'int'),
        ('torquer_duty', 'f', 3, 'float'),
        ('torquer_calib', 'f', 3, 'float'),
        ('hk_4_151_12', 'eh', 1, 'eh'),
        ('acs_mode', 'b', 1, 'int'),
        ('acs_dmode', 'b', 1, 'int'),
        ('ads_mode', 'b', 1, 'int'),
        ('ads_dmode', 'b', 1, 'int'),
        ('ephem_mode', 'b', 1, 'int'),
        ('ephem_dmode', 'b', 1, 'int'),
        ('spin_mode', 'b', 1, 'int'),
        ('status_mag', 'b', 1, 'int'),
        ('status_extmag', 'b', 1, 'int'),
        ('status_css', 'b', 1, 'int'),
        ('status_fss', 'b', 5, 'int'),
        ('status_gyro', 'b', 1, 'int'),
        ('status_extgyro', 'b', 1, 'int'),
        ('status_gps', 'b', 1, 'int'),
        ('status_bdot', 'b', 1, 'int'),
        ('status_ukf', 'b', 1, 'int'),
        ('status_etime', 'b', 1, 'int'),
        ('status_ephem', 'b', 1, 'int'),
        ('status_run', 'b', 1, 'int'),
        ('looptime', 'h', 1, 'int'),
        ('max_looptime', 'h', 1, 'int'),
        ('bdot_rate', 'f', 2, 'float'),
        ('bdot_dmag', 'f', 3, 'float'),
        ('bdot_torquer', 'f', 3, 'float'),
        ('bdot_detumble', 'B', 1, 'int'),
    )),
    13: ("adcs3", (
        ('hk_4_152_13', 'eh', 1, 'eh'),
        ('ukf_x', 'f', 13, 'float'),
        ('ukf_q', 'f', 4, 'float'),
        ('ukf_w', 'f', 3, 'float'),
        ('ukf_xpred', 'f', 13, 'float'),
        ('ukf_zpred', 'f', 12, 'float'),
    )),
    14: ("adcs4", (
        ('hk_4_152_14', 'eh', 1, 'eh'),
        ('ukf_z', 'f', 12, 'float'),
        ('ukf_enable', 'B', 12, 'int'),
        ('ukf_sunmax', 'f', 6, 'float'),
        ('ukf_in_eclipse', 'B', 1, 'int'),
        ('ukf_choice', 'B', 1, 'int'),
        ('ukf_ctrl_t', 'f', 3, 'float'),
        ('ukf_ctrl_m', 'f', 3, 'float'),
        ('ukf_rate', 'f', 3, 'float'),
    )),
    15: ("adcs5", (
        ('hk_4_153_15', 'eh', 1, 'eh'),
        ('ephem_jdat', 'd', 1, 'float'),
        ('ephem_reci', 'f', 3, 'float'),
        ('ephem_veci', 'f', 3, 'float'),
        ('ephem_sun_eci', 'f', 3, 'float'),
        ('ephem_quat_ie', 'f', 4, 'float'),
        ('ephem_quat_io', 'f', 4, 'float'),
        ('ephem_quat_il', 'f', 4, 'float'),
        ('ephem_rate_io', 'f', 3, 'float'),
        ('ephem_rate_il', 'f', 3, 'float'),
        ('ephem_t_eclipse', 'i', 1, 'int'),
        ('hk_4_156_15', 'eh', 1, 'eh'),
        ('ephem_time', 'I', 1, 'long'),
        ('ads_time', 'I', 1, 'long'),
        ('acs_time', 'I', 1, 'long'),
        ('sens_time', 'I', 1, 'long'),
    )),
    16: ("adcs6", (
        ('hk_4_1_16', 'eh', 1, 'eh'),
        ('adcs_swload_cnt1', 'H', 1, 'int'),
        ('hk_4_4_16', 'eh', 1, 'eh'),
        ('adcs_fs_mounted', 'B', 1, 'int'),
        ('adcs_temp_mcu', 'h', 1, 'int'),
        ('adcs_temp_ram', 'h', 1, 'int'),
        ('adcs_resetcause', 'I', 1, 'long'),
        ('adcs_bootcause', 'I', 1, 'long'),
        ('adcs_boot_cnt', 'H', 1, 'int'),
        ('adcs_clock', 'I', 1, 'long'),
        ('adcs_uptime', 'I', 1, 'long'),
    )),
    17: ("adcs7", (
        ('hk_4_154_17', 'eh', 1, 'eh'),
        ('ctrl_refq', 'f', 4, 'float'),
        ('ctrl_errq', 'f', 4, 'float'),
        ('ctrl_errrate', 'f', 3, 'float'),
        ('ctrl_m', 'f', 3, 'float'),
        ('ctrl_mwtorque', 'f', 4, 'float'),
        ('ctrl_mwspeed', 'f', 4, 'float'),
        ('ctrl_mwmoment', 'f', 4, 'float'),
        ('ctrl_refrate', 'f', 3, 'float'),
        ('ctrl_euleroff', 'f', 3, 'float'),
        ('ctrl_btorque', 'f', 3, 'float'),
        ('ctrl_bmoment', 'f', 3, 'float'),
    )),
    21: ("asdr1", (
        ('hk_14_0_21', 'eh', 1, 'eh'),
        ('core_loaded', 'B', 1, 'int'),
        ('hk_14_1_21', 'eh', 1, 'eh'),
        ('sector_history', 'H', 16, 'int'),
        ('mbytes_history', 'H', 16, 'int'),
        ('exposure', 'I', 1, 'long'),
        ('gain', 'f', 1, 'float'),
        ('hk_14_12_21', 'eh', 1, 'eh'),
        ('chan_ref_lock', 'B', 1, 'int'),
        ('hk_14_13_21', 'eh', 1, 'eh'),
        ('chan_temp', 'f', 1, 'float'),
        ('hk_14_16_21', 'eh', 1, 'eh'),
        ('chan_inited', 'B', 1, 'int'),
        ('hk_14_18_21', 'eh', 1, 'eh'),
        ('chan_written', 'f', 1, 'float'),
        ('chan_rec_status', 'B', 1, 'int'),
        ('chan_req_mbytes', 'i', 1, 'int'),
        ('chan_time', 'f', 1, 'float'),
    )),
    22: ("asdr2", (
        ('hk_14_29_22', 'eh', 1, 'eh'),
        ('chan_pps_present', 'B', 1, 'int'),
        ('chan_pps_count', 'i', 1, 'int'),
        ('hk_14_37_22', 'eh', 1, 'eh'),
        ('rec_inited', 'B', 1, 'int'),
        ('hk_14_38_22', 'eh', 1, 'eh'),
        ('rec_written', 'f', 1, 'float'),
        ('rec_rec_status', 'B', 1, 'int'),
        ('rec_req_mbytes', 'i', 1, 'int'),
        ('rec_time', 'f', 1, 'float'),
        ('hk_14_43_22', 'eh', 1, 'eh'),
        ('rec_temp', 'f', 1, 'float'),
        ('hk_14_52_22', 'eh', 1, 'eh'),
        ('trans_inited', 'B', 1, 'int'),
        ('trans_mbytes_sent', 'f', 1, 'float'),
        ('hk_14_53_22', 'eh', 1, 'eh'),
        ('trans_system_time', 'q', 1, 'long'),
        ('hk_14_33_22', 'eh', 1, 'eh'),
        ('mis1_temp', 'f', 1, 'float'),
        ('hk_14_34_22', 'eh', 1, 'eh'),
        ('mis1_fsk_incr', 'i', 1, 'int'),
        ('hk_14_35_22', 'eh', 1, 'eh'),
        ('mis1_system_time', 'q', 1, 'long'),
    )),
    93: ("bcn_inf", (
        ('hk_1_93_93', 'eh', 1, 'eh'),
        ('inf_blob', 'B', 42, 'hexblob'),
    )),
}


def _iso(unix_s: int) -> str:
    return datetime.fromtimestamp(unix_s, timezone.utc).isoformat(timespec="seconds")


def _text_token(text: str) -> str:
    cleaned = re.sub(r"\s+", "_", text.strip()).strip("_")
    return cleaned or "~"


def payload_size(type_id: int) -> int:
    """Beacon body bytes after the 5-byte sub-header."""
    total = 0
    for _name, fmt, count, kind in BEACONS[type_id][1]:
        if kind == "eh":
            total += _EH.size
        elif kind in ("str", "hexstr"):
            total += struct.calcsize(">" + fmt)
        else:
            total += struct.calcsize(">" + fmt) * count
    return total


def token_count(type_id: int) -> int:
    n = 0
    for _name, _fmt, count, kind in BEACONS[type_id][1]:
        n += 1 if kind in ("eh", "str", "hexstr", "hexblob") else count
    return n


def _facts_extras(name: str, values: dict[str, Any]) -> dict[str, Any]:
    if name == "motd":
        return {"callsign": values.get("callsign", ""), "motd": values.get("motd", "")}
    if name == "crit1":
        return {"vbat_mv": values.get("p60_batt_v"), "batt_ma": values.get("p60_batt_c")}
    if name == "obc":
        return {
            "rssi_dbm": values.get("ax100_last_rssi"),
            "rferr_hz": values.get("ax100_last_rferr"),
            "tx_duty": values.get("ax100_tx_duty"),
        }
    return {}


def decode_beacon(csp_header: dict[str, Any], payload: bytes) -> HkDecode | None:
    """Decode one CATSAT beacon (bytes after the CSP header).

    Returns None when the frame is not a recognizable beacon — the shared
    PacketOps then logs it raw as opaque telemetry.
    """
    if len(payload) < 5:
        return None
    type_id = payload[1]
    entry = BEACONS.get(type_id)
    if entry is None:
        return None
    name, fields = entry
    body = payload[5:]
    if len(body) < payload_size(type_id):
        return None

    tokens: list[str] = []
    values: dict[str, Any] = {}
    o = 0
    try:
        for fname, fmt, count, kind in fields:
            if kind == "eh":
                _crc, ts, _source = _EH.unpack_from(body, o)
                o += _EH.size
                tokens.append(_iso(ts))
                values[fname] = ts
            elif kind in ("str", "hexstr"):
                size = struct.calcsize(">" + fmt)
                raw = body[o:o + size]
                o += size
                text = raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")
                values[fname] = text
                tokens.append(f"0x{raw.hex()}" if kind == "hexstr" else _text_token(text))
            elif kind == "hexblob":
                size = struct.calcsize(">" + fmt) * count
                raw = body[o:o + size]
                o += size
                values[fname] = raw.hex()
                tokens.append(f"0x{raw.hex()}")
            else:
                vals = struct.unpack_from(">" + fmt * count, body, o)
                o += struct.calcsize(">" + fmt) * count
                if count == 1:
                    values[fname] = vals[0]
                    tokens.append(repr(vals[0]))
                else:
                    for i, v in enumerate(vals, start=1):
                        values[f"{fname}{i}"] = v
                        tokens.append(repr(v))
    except (struct.error, ValueError, OverflowError, OSError):
        return None

    warnings: tuple[str, ...] = ()
    extra = len(body) - o
    if extra > 0:
        warnings = (f"{extra} trailing bytes after the {name} beacon block",)

    facts: dict[str, Any] = {"kind": name, "satid": int.from_bytes(payload[3:5], "big")}
    facts.update(_facts_extras(name, values))
    return HkDecode(
        container_kind=name,
        tokens=" ".join(tokens).encode("ascii"),
        facts=facts,
        warnings=warnings,
    )
