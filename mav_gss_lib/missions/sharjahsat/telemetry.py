"""Sharjahsat-1 ESER beacon telemetry decode.

Field layout and calibrations follow the community Kaitai definition
(satnogs-decoders ``sharjahsat1.ksy``). Two deviations are validated
against a live frame received by GS-1 on 2026-07-10 05:37:23 UTC:

  - The interface-board RTC registers arrive ss/mm/hh dow/dd/mo/yy
    (BCD), not the hh..dow order the .ksy labels claim — read this way
    the RTC agrees with the OBC unix clock to the second.
  - Panel thermistors outside +/-150 degC are excluded from the min/max
    parameters (several B-side sensors sit saturated near +230 degC).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

TELEMETRY_SIZE = 246

_PANEL_TEMP_VALID_C = 150.0
_SUN_DIODE_MA = 100.0
_VBCR_SCALE = (
    0.0322581, 0.0322581, 0.0099706, 0.0322581, 0.0322581,
    0.0322581, 0.0322581, 0.0322581, 0.0322581,
)


@dataclass(frozen=True, slots=True)
class DecodedTelemetry:
    """Mission-facts sections plus the ascii_tokens walker line."""

    sections: dict[str, Any]
    tokens: bytes


class _Reader:
    __slots__ = ("data", "offset")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def u1(self) -> int:
        value = self.data[self.offset]
        self.offset += 1
        return value

    def u2(self) -> int:
        value = struct.unpack_from("<H", self.data, self.offset)[0]
        self.offset += 2
        return value

    def s2(self) -> int:
        value = struct.unpack_from("<h", self.data, self.offset)[0]
        self.offset += 2
        return value

    def u4(self) -> int:
        value = struct.unpack_from("<I", self.data, self.offset)[0]
        self.offset += 4
        return value

    def raw(self, size: int) -> bytes:
        value = self.data[self.offset:self.offset + size]
        self.offset += size
        return value


def _num(value: float, decimals: int) -> tuple[float, str]:
    token = f"{value:.{decimals}f}"
    return float(token), token


def _volts(value: float) -> tuple[float, str]:
    return _num(value, 3)


def _milliamps(value: float) -> tuple[float, str]:
    return _num(value, 1)


def _degrees_c(value: float) -> tuple[float, str]:
    return _num(value, 2)


def _obc_time_token(unix_s: int) -> str:
    try:
        stamp = datetime.fromtimestamp(unix_s, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return str(unix_s)
    return stamp.isoformat(timespec="seconds")


def _bcd(value: int) -> int | None:
    high, low = value >> 4, value & 0x0F
    if high > 9 or low > 9:
        return None
    return high * 10 + low


def _rtc_utc(registers: bytes) -> str | None:
    ss, mm, hh, _dow, dd, mo, yy = (_bcd(b) for b in registers)
    fields = (ss, mm, hh, dd, mo, yy)
    if any(v is None for v in fields):
        return None
    if not (ss < 60 and mm < 60 and hh < 24 and 1 <= dd <= 31 and 1 <= mo <= 12):
        return None
    return f"20{yy:02d}-{mo:02d}-{dd:02d}T{hh:02d}:{mm:02d}:{ss:02d}+00:00"


def parse_telemetry(data: bytes) -> DecodedTelemetry:
    """Decode one 246-byte tm_id 'P' housekeeping block."""

    reader = _Reader(data)
    tokens: list[str] = []
    sections: dict[str, Any] = {}

    op_mode = reader.u2()
    restart_count = reader.u2()
    reset_cause = reader.u1()
    uptime_s = reader.u4()
    obc_unix_s = reader.u4()
    obc_utc = _obc_time_token(obc_unix_s)

    obc_temps = [_degrees_c(reader.s2() / 100) for _ in range(3)]
    obc_vbat_v = _volts(reader.u2() / 1000)
    obc_vbat_i = _milliamps(reader.u2())
    obc_vbat_plat_v = _volts(reader.u2() / 1000)
    plat_3v3 = _volts(reader.u2() / 1000)
    obc_vbat_periph_i = _milliamps(reader.u2() * 10)
    obc_3v3_periph_i = _milliamps(reader.u2() * 10)
    obc_vbat_periph_v = _volts(reader.u2() / 1000)
    periph_3v3 = _volts(reader.u2() / 1000)

    rtc_registers = reader.raw(7)
    rtc_temp_raw = reader.s2()
    antenna_status = f"0x{reader.u1():02x}"
    rtc_utc = _rtc_utc(rtc_registers)

    batt_vbat = _volts(reader.u2() * 0.008993)
    batt_ibat = _milliamps(reader.s2() * 14.662757)
    batt_pcm3v3 = _volts(reader.u2() * 0.004311)
    batt_pcm5v = _volts(reader.u2() * 0.005865)
    batt_pcm3v3_i = _milliamps(reader.u2() * 1.327547)
    batt_pcm5v_i = _milliamps(reader.u2() * 1.327547)
    batt_temp_board = _degrees_c(reader.s2() * 0.372434 - 273.15)
    batt_temp_cells = [_degrees_c(reader.s2() * 0.3976 - 238.57) for _ in range(3)]

    eps_bus_v = _volts(reader.u2() * 0.008978)
    eps_bus_i = _milliamps(reader.s2() * 6.81988679)
    eps_rail_3v3 = _volts(reader.u2() * 0.004311)
    eps_rail_3v3_i = _milliamps(reader.u2() * 6.81988679)
    eps_rail_5v = _volts(reader.u2() * 0.005865)
    eps_rail_5v_i = _milliamps(reader.u2() * 6.81988679)
    eps_draw_3v3 = _milliamps(reader.u2() * 1.327547)
    eps_draw_5v = _milliamps(reader.u2() * 1.327547)
    eps_temp_a = _degrees_c(reader.u2() * 0.372434 - 273.15)
    eps_temp_b = _degrees_c(reader.u2() * 0.372434 - 273.15)
    eps_rail_12v_i = _milliamps(reader.u2() * 2.066632361)
    eps_rail_12v = _volts(reader.u2() * 0.01349)

    adcs_state = f"0x{reader.u1():02x}"
    adcs_lat = _num(reader.s2() * 0.01, 2)
    adcs_lon = _num(reader.s2() * 0.01, 2)
    adcs_alt_km = _num(reader.s2() * 0.01, 2)
    adcs_angles = [_num(reader.s2() * 0.01, 2) for _ in range(3)]
    adcs_rates = [_num(reader.s2() * 0.01, 2) for _ in range(3)]
    adcs_gps = reader.raw(18)

    uhf_smps_temp = reader.u1()
    uhf_pa_temp = reader.u1()
    uhf_3v3_i_ua = reader.u2() * 3
    uhf_3v3 = _volts(reader.u2() * 0.004)
    uhf_5v_i_ua = reader.u2() * 62
    uhf_5v = _volts(reader.u2() * 0.004)

    sband_raw = [reader.u2() for _ in range(8)]
    sband_power = "ON" if any(sband_raw) else "OFF"

    vbcr = [_volts(reader.u2() * _VBCR_SCALE[i]) for i in range(9)]
    ibcra_raw = [reader.u2() for _ in range(9)]
    ibcrb_raw = [reader.u2() for _ in range(9)]
    tbcra = [_degrees_c(reader.s2() * 0.4963 - 273.15) for _ in range(9)]
    tbcrb = [_degrees_c(reader.s2() * 0.4963 - 273.15) for _ in range(9)]
    vdiode = _volts(reader.u2() * 0.008993157)
    idiode = _milliamps(reader.u2() * 14.662757)

    ibcra = [_milliamps(raw * 0.9775) for raw in ibcra_raw]
    ibcrb = [_milliamps(raw * 0.9775) for raw in ibcrb_raw]
    array_i = _milliamps(sum(raw * 0.9775 for raw in ibcra_raw + ibcrb_raw))
    panel_temps = [t for t, _ in tbcra + tbcrb]
    valid_temps = [t for t in panel_temps if abs(t) <= _PANEL_TEMP_VALID_C]
    ranked = valid_temps or panel_temps
    temp_min = _degrees_c(min(ranked))
    temp_max = _degrees_c(max(ranked))
    illumination = "SUN" if idiode[0] > _SUN_DIODE_MA else "ECLIPSE"

    sections["system"] = {
        "obc_utc": obc_utc,
        "obc_unix_s": obc_unix_s,
        "op_mode": f"0x{op_mode:04x}",
        "restart_count": restart_count,
        "reset_cause": f"0x{reset_cause:02x}",
        "uptime_s": uptime_s,
        "antenna_status": antenna_status,
    }
    tokens += [
        obc_utc, f"0x{op_mode:04x}", str(restart_count),
        f"0x{reset_cause:02x}", str(uptime_s), antenna_status,
    ]

    sections["obc"] = {
        "temps_c": [v for v, _ in obc_temps],
        "plat_3v3_v": plat_3v3[0],
        "periph_3v3_v": periph_3v3[0],
        "raw_vbat_v": obc_vbat_v[0],
        "raw_vbat_i_ma": obc_vbat_i[0],
        "raw_vbat_plat_v": obc_vbat_plat_v[0],
        "raw_vbat_periph_v": obc_vbat_periph_v[0],
        "raw_vbat_periph_i_ma": obc_vbat_periph_i[0],
        "raw_3v3_periph_i_ma": obc_3v3_periph_i[0],
    }
    tokens += [t for _, t in obc_temps] + [plat_3v3[1], periph_3v3[1]]

    sections["rtc"] = {
        "utc": rtc_utc,
        "raw_hex": rtc_registers.hex(" "),
        "temp_raw": rtc_temp_raw,
    }

    sections["battery"] = {
        "vbat_v": batt_vbat[0],
        "ibat_ma": batt_ibat[0],
        "pcm3v3_v": batt_pcm3v3[0],
        "pcm5v_v": batt_pcm5v[0],
        "pcm3v3_i_ma": batt_pcm3v3_i[0],
        "pcm5v_i_ma": batt_pcm5v_i[0],
        "temp_board_c": batt_temp_board[0],
        "temp_cells_c": [v for v, _ in batt_temp_cells],
    }
    tokens += [
        batt_vbat[1], batt_ibat[1], batt_pcm3v3[1], batt_pcm5v[1],
        batt_pcm3v3_i[1], batt_pcm5v_i[1], batt_temp_board[1],
    ] + [t for _, t in batt_temp_cells]

    sections["eps"] = {
        "bus_v": eps_bus_v[0],
        "bus_i_ma": eps_bus_i[0],
        "rail_3v3_v": eps_rail_3v3[0],
        "rail_3v3_i_ma": eps_rail_3v3_i[0],
        "rail_5v_v": eps_rail_5v[0],
        "rail_5v_i_ma": eps_rail_5v_i[0],
        "rail_12v_v": eps_rail_12v[0],
        "rail_12v_i_ma": eps_rail_12v_i[0],
        "draw_3v3_ma": eps_draw_3v3[0],
        "draw_5v_ma": eps_draw_5v[0],
        "temp_a_c": eps_temp_a[0],
        "temp_b_c": eps_temp_b[0],
    }
    tokens += [
        eps_bus_v[1], eps_bus_i[1], eps_rail_3v3[1], eps_rail_3v3_i[1],
        eps_rail_5v[1], eps_rail_5v_i[1], eps_rail_12v[1], eps_rail_12v_i[1],
        eps_draw_3v3[1], eps_draw_5v[1], eps_temp_a[1], eps_temp_b[1],
    ]

    sections["adcs"] = {
        "state": adcs_state,
        "yaw_deg": adcs_angles[0][0],
        "pitch_deg": adcs_angles[1][0],
        "roll_deg": adcs_angles[2][0],
        "rate_yaw_dps": adcs_rates[0][0],
        "rate_pitch_dps": adcs_rates[1][0],
        "rate_roll_dps": adcs_rates[2][0],
        "llh": {
            "lat_deg": adcs_lat[0],
            "lon_deg": adcs_lon[0],
            "alt_km": adcs_alt_km[0],
        },
        "gps_hex": adcs_gps.hex(),
    }
    tokens += [adcs_state] + [t for _, t in adcs_angles] + [t for _, t in adcs_rates]

    sections["uhf"] = {
        "smps_temp_c": uhf_smps_temp,
        "pa_temp_c": uhf_pa_temp,
        "v3v3_v": uhf_3v3[0],
        "v5_v": uhf_5v[0],
        "i3v3_ua": uhf_3v3_i_ua,
        "i5_ua": uhf_5v_i_ua,
    }
    tokens += [str(uhf_smps_temp), str(uhf_pa_temp), uhf_3v3[1], uhf_5v[1]]

    sections["sband"] = {"power": sband_power, "raw": sband_raw}
    tokens.append(sband_power)

    sections["solar"] = {
        "illumination": illumination,
        "vdiode_v": vdiode[0],
        "idiode_ma": idiode[0],
        "array_i_ma": array_i[0],
        "temp_min_c": temp_min[0],
        "temp_max_c": temp_max[0],
        "vbcr_v": [v for v, _ in vbcr],
        "ibcra_ma": [v for v, _ in ibcra],
        "ibcrb_ma": [v for v, _ in ibcrb],
        "tbcra_c": [v for v, _ in tbcra],
        "tbcrb_c": [v for v, _ in tbcrb],
    }
    tokens += [
        illumination, vdiode[1], idiode[1], array_i[1], temp_min[1], temp_max[1],
    ]

    return DecodedTelemetry(
        sections=sections,
        tokens=" ".join(tokens).encode("ascii"),
    )
