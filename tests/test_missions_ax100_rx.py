"""Tests for the AX100 Mode 5 RX-only mission family
(snipe / catsat / suomi100 / luojia1 / roads / aistechsat2 / innocube /
nushsat1 + the shared ax100_rx library)."""

from __future__ import annotations

import struct
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mav_gss_lib.platform import PlatformRuntime
from mav_gss_lib.platform.loader import discover_missions, load_mission_spec_from_split
from mav_gss_lib.platform.spec import parse_yaml

from mav_gss_lib.missions.ax100_rx import Ax100RxPacketOps
from mav_gss_lib.missions.aistechsat2.mission import TARGET as AISTECH_TARGET
from mav_gss_lib.missions.catsat.mission import TARGET as CATSAT_TARGET
from mav_gss_lib.missions.innocube.mission import TARGET as INNOCUBE_TARGET
from mav_gss_lib.missions.luojia1.mission import TARGET as LUOJIA_TARGET
from mav_gss_lib.missions.nushsat1.mission import TARGET as NUSHSAT_TARGET
from mav_gss_lib.missions.roads.mission import TARGET as ROADS_TARGET
from mav_gss_lib.missions.snipe.mission import TARGET as SNIPE_TARGET
from mav_gss_lib.missions.suomi100.mission import TARGET as SUOMI_TARGET
from mav_gss_lib.missions.suomi100.telemetry import (
    BEACON0_SIZE,
    BEACON1_SIZE,
    decode_beacon,
)


M5_META = {"transmitter": "9k6 FSK AX100 ASM+Golay downlink"}
AX25_META = {"transmitter": "9k6 FSK AX.25 G3RUH downlink"}

ALL_TARGETS = [
    SNIPE_TARGET, CATSAT_TARGET, SUOMI_TARGET, LUOJIA_TARGET, ROADS_TARGET,
    AISTECH_TARGET, INNOCUBE_TARGET, NUSHSAT_TARGET,
]

EXPECTED = {
    "snipe": (56749, 435_450_000.0, "435.450 MHz"),
    "catsat": (60246, 437_185_000.0, "437.185 MHz"),
    "suomi100": (43804, 437_775_000.0, "437.775 MHz"),
    "luojia1": (43485, 437_250_000.0, "437.250 MHz"),
    "roads": (64535, 435_400_000.0, "435.400 MHz"),
    "aistechsat2": (43768, 436_600_000.0, "436.600 MHz"),
    "innocube": (62616, 435_950_000.0, "435.950 MHz"),
    "nushsat1": (63211, 436_200_000.0, "436.200 MHz"),
}


def _csp_header(prio=1, src=1, dest=10, dport=25, sport=1, flags=0) -> bytes:
    h = (prio << 30) | (src << 25) | (dest << 20) | (dport << 14) | (sport << 8) | flags
    return h.to_bytes(4, "big")


# ---------------------------------------------------------------- discovery


def test_all_family_missions_discoverable():
    ids = {m["id"] for m in discover_missions()}
    assert {"snipe", "catsat", "suomi100", "luojia1", "roads",
            "aistechsat2", "innocube", "nushsat1"} <= ids


def test_shared_library_is_not_a_mission():
    assert "ax100_rx" not in {m["id"] for m in discover_missions()}


# ---------------------------------------------------------------- build/seed


@pytest.mark.parametrize("target", ALL_TARGETS, ids=lambda t: t.mission_id)
def test_build_seeds_frequency_tracking_and_radio(target, tmp_path):
    platform_cfg: dict = {}
    mission_cfg: dict = {}
    spec = load_mission_spec_from_split(
        platform_cfg, target.mission_id, mission_cfg, data_dir=tmp_path
    )
    norad, freq_hz, label = EXPECTED[target.mission_id]
    assert spec.id == target.mission_id
    assert spec.commands is None
    assert spec.spec_root is not None
    assert platform_cfg["rx"]["frequency"] == label
    assert platform_cfg["radio"]["script"] == "gnuradio/MAV_DUO.py"
    tracking = platform_cfg["tracking"]
    assert tracking["tle_fetch"]["identifier"] == str(norad)
    assert tracking["frequencies"]["rx_hz"] == freq_hz
    assert str(norad) in tracking["tle"]["line1"]
    assert mission_cfg["mission_name"] == target.mission_name


@pytest.mark.parametrize("target", ALL_TARGETS, ids=lambda t: t.mission_id)
def test_build_respects_operator_values(target, tmp_path):
    platform_cfg = {
        "rx": {"frequency": "437.000 MHz"},
        "tracking": {"tle": {"line1": "l1", "line2": "l2", "method": "manual"}},
    }
    load_mission_spec_from_split(platform_cfg, target.mission_id, {}, data_dir=tmp_path)
    assert platform_cfg["rx"]["frequency"] == "437.000 MHz"
    assert platform_cfg["tracking"]["tle"]["line1"] == "l1"
    assert platform_cfg["tracking"]["tle"]["method"] == "manual"


def test_mission_ymls_parse_standalone():
    missions_root = Path(__file__).resolve().parent.parent / "mav_gss_lib" / "missions"
    for mission_id in ("snipe", "catsat", "suomi100", "luojia1", "roads",
                       "aistechsat2", "innocube", "nushsat1"):
        mission = parse_yaml(missions_root / mission_id / "mission.yml", plugins={})
        assert mission.ui is not None
        assert len(mission.ui.rx_columns) >= 5


# ---------------------------------------------------------------- shared ops


def test_csp_frame_parses_into_header_facts():
    ops = Ax100RxPacketOps("snipe")
    frame = _csp_header(src=5, dest=9, dport=30) + b"\x01\x02\x03\x04"
    packet = ops.parse(ops.normalize(M5_META, frame))
    facts = packet.mission["facts"]
    assert facts["header"]["type"] == "TLM"
    assert facts["header"]["src"] == 5
    assert facts["header"]["dst"] == 9
    assert facts["header"]["dport"] == 30
    assert facts["header"]["len"] == len(frame)
    assert facts["protocol"]["csp_header"]["sport"] == 1
    flags = ops.classify(packet)
    assert flags.is_unknown is False
    assert flags.duplicate_key


def test_duplicate_key_is_stable_per_frame():
    ops = Ax100RxPacketOps("snipe")
    frame = _csp_header() + b"payload"
    k1 = ops.classify(ops.parse(ops.normalize(M5_META, frame))).duplicate_key
    k2 = ops.classify(ops.parse(ops.normalize(M5_META, frame))).duplicate_key
    assert k1 == k2
    other = ops.classify(ops.parse(ops.normalize(M5_META, frame + b"!"))).duplicate_key
    assert other != k1


def test_ax25_branch_frame_is_unknown():
    ops = Ax100RxPacketOps("snipe")
    packet = ops.parse(ops.normalize(AX25_META, b"\x82l dummy ax25 frame"))
    assert ops.classify(packet).is_unknown is True
    assert any("AX.25" in w for w in packet.warnings)


def test_short_frame_is_unknown():
    ops = Ax100RxPacketOps("snipe")
    packet = ops.parse(ops.normalize(M5_META, b"\x01\x02"))
    assert ops.classify(packet).is_unknown is True


def test_match_verifiers_returns_empty():
    ops = Ax100RxPacketOps("snipe")
    assert ops.match_verifiers(None, [], now_ms=0) == []


# ---------------------------------------------------------------- suomi100


def _suomi_beacon0() -> bytes:
    ts = 1_783_662_000  # 2026-07-10T05:40:00Z
    eps = struct.pack(
        ">I3HH7H3HHH6hB",
        ts, 5010, 5020, 4990, 8123,
        10, 20, 30, 40, 50, 60, 70,
        110, 120, 130,
        120, 310,
        11, 12, 13, 14, 15, 16,
        3,
    )
    com = struct.pack(">I5h", ts + 1, 21, -4, -97, 1500, -105)
    obc = struct.pack(">I6H2h", ts + 2, 1, 2, 3, 4, 5, 6, 24, 25)
    return b"\x00" + eps + com + obc


def _suomi_beacon1() -> bytes:
    ts = 1_783_662_100
    eps = struct.pack(
        ">8I2BB6H8BB",
        ts, 3600, 86400, 42, 7, 8, 9, 10,
        2, 3,
        6,
        0, 1, 0, 2, 0, 0,
        1, 1, 0, 1, 0, 0, 0, 0,
        1,
    )
    com = struct.pack(
        ">IB4IHIIIBII",
        ts + 1, 10,
        100_000, 200_000, 3_000_000_000, 4_000_000_000,
        77, 305_419_896,
        123_456, 654_321,
        5,
        1111, 2222,
    )
    obc = struct.pack(
        ">I6BHBHII",
        ts + 2, 1, 0, 1, 0, 0, 0,
        99, 1, 55, 2, ts + 3,
    )
    return b"\x01" + eps + com + obc


def test_suomi_beacon_sizes_match_gr_satellites_layout():
    assert BEACON0_SIZE == 84
    assert BEACON1_SIZE == 124
    assert len(_suomi_beacon0()) == BEACON0_SIZE
    assert len(_suomi_beacon1()) == BEACON1_SIZE


def test_suomi_beacon0_end_to_end_parameters(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "suomi100", {},
    )
    assert runtime.walker is not None
    frame = _csp_header(src=1, dest=10) + _suomi_beacon0()
    result = runtime.process_rx(M5_META, frame)
    packet = result.packet
    values = {u.name: u for u in packet.parameters}
    assert result.container_id == "beacon0"
    assert len(values) == 39
    assert values["eps.eps_ts"].value == "2026-07-10T05:40:00+00:00"
    assert values["eps.vbat"].value == 8123
    assert values["eps.vbat"].unit == "mV"
    assert values["eps.pv1_v"].value == 5010
    assert values["eps.out7_i"].value == 70
    assert values["eps.batt_out_i"].value == 310
    assert values["eps.eps_temp6"].value == 16
    assert values["eps.batt_mode"].value == 3
    assert values["com.com_temp2"].value == -4
    assert values["com.rssi"].value == -97
    assert values["com.rssi"].unit == "dBm"
    assert values["com.rferr"].value == 1500
    assert values["com.rssi_bgnd"].value == -105
    assert values["obc.obc_cur6"].value == 6
    assert values["obc.obc_temp2"].value == 25
    facts = packet.mission["facts"]
    assert facts["header"]["type"] == "BCN"
    assert facts["beacon"]["vbat_mv"] == 8123
    assert facts["beacon"]["rssi_dbm"] == -97
    assert packet.flags.is_unknown is False


def test_suomi_beacon1_end_to_end_parameters(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "suomi100", {},
    )
    frame = _csp_header(src=1, dest=10) + _suomi_beacon1()
    result = runtime.process_rx(M5_META, frame)
    values = {u.name: u for u in result.packet.parameters}
    assert result.container_id == "beacon1"
    assert len(values) == 39
    assert values["eps.wdt_gnd"].value == 86400
    assert values["eps.eps_boots"].value == 42
    assert values["eps.latchup4"].value == 2
    assert values["eps.outputs"].value == "0x0101000100000000"
    assert values["eps.ppt_mode"].value == 1
    assert values["com.tx_duty"].value == 10
    assert values["com.total_tx_bytes"].value == 3_000_000_000
    assert values["com.total_rx_bytes"].value == 4_000_000_000
    assert values["com.com_boot_cause"].value == 305_419_896
    assert values["com.rx_count"].value == 2222
    assert values["obc.obc_pwr"].value == "0x010001000000"
    assert values["obc.sw_count"].value == 99
    assert values["obc.obc_boots"].value == 55
    assert values["obc.obc_clock"].value == datetime.fromtimestamp(
        1_783_662_103, timezone.utc
    ).isoformat(timespec="seconds")


def test_suomi_unrecognized_payload_is_opaque_telemetry():
    frame_payload = b"\x07" + b"\x00" * 90
    assert decode_beacon({}, frame_payload) is None
    ops = Ax100RxPacketOps("suomi100", hk_decoder=decode_beacon)
    packet = ops.parse(ops.normalize(M5_META, _csp_header() + frame_payload))
    assert packet.mission["facts"]["header"]["type"] == "TLM"
    assert ops.classify(packet).is_unknown is False


def test_suomi_truncated_beacon_is_opaque():
    truncated = _suomi_beacon0()[: BEACON0_SIZE - 5]
    assert decode_beacon({}, truncated) is None


def test_suomi_trailing_bytes_warn_but_decode():
    hk = decode_beacon({}, _suomi_beacon0() + b"\xaa\xbb")
    assert hk is not None
    assert hk.container_kind == "beacon0"
    assert any("trailing" in w for w in hk.warnings)


# ---------------------------------------------------------------- catsat


def _csp_header_le(prio=1, src=5, dest=10, dport=25, sport=1, flags=0) -> bytes:
    h = (prio << 30) | (src << 25) | (dest << 20) | (dport << 14) | (sport << 8) | flags
    return h.to_bytes(4, "little")


def _pack_catsat(type_id: int, values: dict | None = None, *,
                 satid: int = 1234, ts: int = 1_783_700_000) -> bytes:
    """Build a CATSAT frame body from the telemetry field table itself."""
    from mav_gss_lib.missions.catsat import telemetry as ct

    values = values or {}
    body = b""
    for name, fmt, count, kind in ct.BEACONS[type_id][1]:
        if kind == "eh":
            body += struct.pack(">HIH", 0, values.get(name, ts), 0)
        elif kind in ("str", "hexstr"):
            size = struct.calcsize(">" + fmt)
            body += values.get(name, b"").ljust(size, b"\x00")[:size]
        elif kind == "hexblob":
            size = struct.calcsize(">" + fmt) * count
            body += values.get(name, b"").ljust(size, b"\x00")[:size]
        elif count == 1:
            body += struct.pack(">" + fmt, values.get(name, 0))
        else:
            body += struct.pack(">" + fmt * count, *values.get(name, [0] * count))
    return bytes([2, type_id, 1]) + satid.to_bytes(2, "big") + body


def test_catsat_all_beacon_types_end_to_end(tmp_path):
    from mav_gss_lib.missions.catsat import telemetry as ct

    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "catsat", {},
    )
    assert runtime.walker is not None
    for type_id, (name, _fields) in sorted(ct.BEACONS.items()):
        frame = _csp_header_le() + _pack_catsat(type_id)
        result = runtime.process_rx(M5_META, frame)
        assert result.container_id == name, (type_id, name, result.container_id)
        assert len(result.packet.parameters) == ct.token_count(type_id), name
        assert result.packet.flags.is_unknown is False


def test_catsat_crit1_values_end_to_end(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "catsat", {},
    )
    frame = _csp_header_le(src=1, dest=10) + _pack_catsat(1, {
        "obc_temp_mcu": 21,
        "obc_boot_cnt": 57,
        "bpx_vbatt": 8050,
        "bpx_temp": 12,
        "ax100_temp_brd": 11,
        "p60_batt_v": 7900,
        "p60_batt_c": -250,
        "p60_batt_mode": 3,
        "pdu_x2_cout": [10, -20, 30, 0, 0, 0, 0, 0, 415],
    })
    result = runtime.process_rx(M5_META, frame)
    values = {u.name: u for u in result.packet.parameters}
    assert result.container_id == "crit1"
    assert values["crit.p60_batt_v"].value == 7900
    assert values["crit.p60_batt_c"].value == -250
    assert values["crit.bpx_vbatt"].value == 8050
    assert values["crit.obc_temp_mcu"].value == 21
    assert values["crit.pdu_x2_cout9"].value == 415
    assert values["crit.hk_1_4_1"].value == "2026-07-10T16:13:20+00:00"
    facts = result.packet.mission["facts"]
    assert facts["header"]["src"] == 1
    assert facts["beacon"]["kind"] == "crit1"
    assert facts["beacon"]["vbat_mv"] == 7900
    assert facts["beacon"]["satid"] == 1234


def test_catsat_motd_text_lands_in_facts():
    from mav_gss_lib.missions.catsat.telemetry import decode_beacon

    payload = _pack_catsat(0, {
        "callsign": b"CATSAT",
        "motd": b"HELLO FROM ARIZONA",
    })
    hk = decode_beacon({}, payload)
    assert hk is not None
    assert hk.container_kind == "motd"
    assert hk.facts["callsign"] == "CATSAT"
    assert hk.facts["motd"] == "HELLO FROM ARIZONA"
    tokens = hk.tokens.split(b" ")
    assert tokens[1] == b"CATSAT"
    assert tokens[2].startswith(b"0x")


def test_catsat_le_csp_header_parses():
    from mav_gss_lib.missions.catsat.telemetry import decode_beacon

    ops = Ax100RxPacketOps("catsat", hk_decoder=decode_beacon, csp_endianness="little")
    frame = _csp_header_le(src=7, dest=19, dport=31) + _pack_catsat(6)
    packet = ops.parse(ops.normalize(M5_META, frame))
    header = packet.mission["facts"]["header"]
    assert header["src"] == 7
    assert header["dst"] == 19
    assert header["dport"] == 31
    assert packet.mission["facts"]["beacon"]["kind"] == "dep"


def test_catsat_unknown_beacon_type_is_opaque():
    from mav_gss_lib.missions.catsat.telemetry import decode_beacon

    assert decode_beacon({}, bytes([2, 42, 1, 0, 0]) + b"\x00" * 64) is None


def test_catsat_truncated_beacon_is_opaque():
    from mav_gss_lib.missions.catsat.telemetry import decode_beacon

    payload = _pack_catsat(1)
    assert decode_beacon({}, payload[:-6]) is None


def test_catsat_yml_containers_match_field_table():
    """Every container's entry count equals the walker's token count."""
    import yaml as _yaml

    from mav_gss_lib.missions.catsat import telemetry as ct

    yml = Path(__file__).resolve().parent.parent / "mav_gss_lib" / "missions" / "catsat" / "mission.yml"
    doc = _yaml.safe_load(yml.read_text(encoding="utf-8"))
    containers = doc["sequence_containers"]
    assert len(containers) == len(ct.BEACONS)
    for type_id, (name, _fields) in ct.BEACONS.items():
        assert len(containers[name]["entry_list"]) == ct.token_count(type_id), name


# ---------------------------------------------------------------- roads


def _roads_beacon(values: dict | None = None, *, protocol_version=1,
                  satid=4242, ts=1_783_800_000, blocks=None) -> bytes:
    from mav_gss_lib.missions.roads import telemetry as rt

    values = values or {}
    body = struct.pack(">BBBH", protocol_version, 0, 1, satid)
    fields = rt._FIELDS if blocks is None else tuple(
        f for f in rt._FIELDS if f[0] in blocks)
    for _domain, key, fmt, _render in fields:
        body += struct.pack(">HIH", 0, ts, 1)
        body += struct.pack(">" + fmt, values.get(key, 7))
    return body


def test_roads_beacon_size_matches_und_format():
    from mav_gss_lib.missions.roads import telemetry as rt

    assert rt.BEACON_SIZE == 444
    assert rt.TOKEN_COUNT == 47
    assert len(_roads_beacon()) == rt.BEACON_SIZE


def test_roads_on_air_runs_fit_one_mode5_frame():
    """Every supported slice except the full table fits RS(255,223)."""
    from mav_gss_lib.missions.roads import telemetry as rt

    for run in rt.SUPPORTED_RUNS:
        if run != ("obc", "gnss", "eps", "uhf", "vhf"):
            assert rt.run_size(run) <= rt.MODE5_INNER_CAP, run
    # the documented full table is deliberately NOT transmittable in one frame
    assert rt.BEACON_SIZE > rt.MODE5_INNER_CAP


def test_roads_beacon_end_to_end_parameters(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "roads", {},
    )
    assert runtime.walker is not None
    frame = _csp_header(src=1, dest=10) + _roads_beacon({
        "vbatt": 8123,
        "temp_mcu": -12,
        "bootcount": 57,
        "cursys": 415,
        "battmode": 3,
        "uhf_boot_cause": 0xDEADBEEF,
    })
    result = runtime.process_rx(M5_META, frame)
    values = {u.name: u for u in result.packet.parameters}
    assert result.container_id == "beacon_hk"
    assert len(values) == 47
    assert values["eps.vbatt"].value == 8123
    assert values["eps.vbatt"].unit == "mV"
    assert values["obc.temp_mcu"].value == -12
    assert values["obc.bootcount"].value == 57
    assert values["eps.cursys"].value == 415
    assert values["uhf.uhf_boot_cause"].value == "0xdeadbeef"
    assert values["obc.obc_ts"].value == "2026-07-11T20:00:00+00:00"
    facts = result.packet.mission["facts"]
    assert facts["header"]["type"] == "BCN"
    assert facts["beacon"]["satid"] == 4242
    assert facts["beacon"]["vbat_mv"] == 8123
    assert result.packet.flags.is_unknown is False


def test_roads_eps_slice_end_to_end(tmp_path):
    """A 173-byte eps-only beacon — the realistic on-air shape."""
    from mav_gss_lib.missions.roads import telemetry as rt

    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "roads", {},
    )
    beacon = _roads_beacon({"vbatt": 8123, "cursys": 415, "battmode": 3},
                           blocks=("eps",))
    assert len(beacon) == rt.run_size(("eps",)) == 173
    result = runtime.process_rx(M5_META, _csp_header() + beacon)
    values = {u.name: u for u in result.packet.parameters}
    assert result.container_id == "beacon_eps"
    assert len(values) == 18
    assert values["eps.vbatt"].value == 8123
    assert values["eps.cursys"].value == 415
    facts = result.packet.mission["facts"]
    assert facts["beacon"]["kind"] == "hk_eps"
    assert facts["beacon"]["blocks"] == "eps"
    assert facts["beacon"]["vbat_mv"] == 8123


def test_roads_obc_gnss_slice_end_to_end(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "roads", {},
    )
    beacon = _roads_beacon({"bootcount": 57}, blocks=("obc", "gnss"))
    result = runtime.process_rx(M5_META, _csp_header() + beacon)
    values = {u.name: u for u in result.packet.parameters}
    assert result.container_id == "beacon_obc_gnss"
    assert len(values) == 15
    assert values["obc.bootcount"].value == 57
    assert "vbat_mv" not in result.packet.mission["facts"]["beacon"]


def test_roads_uhf_slice_assumes_uhf_with_warning():
    from mav_gss_lib.missions.roads.telemetry import decode_beacon

    hk = decode_beacon({}, _roads_beacon(blocks=("uhf",)))
    assert hk is not None
    assert hk.container_kind == "hk_uhf"
    assert any("ambiguous" in w for w in hk.warnings)


def test_roads_wrong_protocol_version_is_opaque():
    from mav_gss_lib.missions.roads.telemetry import decode_beacon

    assert decode_beacon({}, _roads_beacon(protocol_version=2)) is None


def test_roads_unsupported_length_is_opaque():
    from mav_gss_lib.missions.roads.telemetry import decode_beacon

    assert decode_beacon({}, _roads_beacon()[:-5]) is None
    assert decode_beacon({}, _roads_beacon() + b"\xaa\xbb") is None
    # a 308-byte obc+gnss+eps slice cannot fit one Mode 5 frame
    assert decode_beacon({}, _roads_beacon(blocks=("obc", "gnss", "eps"))) is None


def test_roads_yml_containers_match_runs():
    """Every run container's entry count equals the decoder token count."""
    import yaml as _yaml

    from mav_gss_lib.missions.roads import telemetry as rt

    yml = Path(__file__).resolve().parent.parent / "mav_gss_lib" / "missions" / "roads" / "mission.yml"
    doc = _yaml.safe_load(yml.read_text(encoding="utf-8"))
    containers = doc["sequence_containers"]
    assert len(containers) == len(rt.SUPPORTED_RUNS)
    by_kind = {c["restriction_criteria"]["packet"]["kind"]: c
               for c in containers.values()}
    for run in rt.SUPPORTED_RUNS:
        container = by_kind[rt.run_kind(run)]
        assert len(container["entry_list"]) == rt.run_token_count(run), run


# ---------------------------------------------------------------- aistechsat2


def _lume_frame(payload_id: int, overrides: dict | None = None, *,
                day=9500, ms=43_200_123) -> bytes:
    from mav_gss_lib.missions.aistechsat2 import telemetry as at

    overrides = overrides or {}
    _, fields = at.PAYLOADS[payload_id]
    body = b""
    for name, fmt, count, _render, _t in fields:
        if fmt == "32s":
            body += overrides.get(name, b"AIST fw v2.1").ljust(32, b"\x00")[:32]
        elif count == 1:
            default = 2.5 if fmt == "f" else 2
            body += struct.pack(">" + fmt, overrides.get(name, default))
        else:
            default = [2.5 if fmt == "f" else 2] * count
            body += struct.pack(">" + fmt * count, *overrides.get(name, default))
    sp_header = struct.pack(">HHH", (1 << 11) | 100, (3 << 14) | 7, 0)
    return (bytes(5) + sp_header + bytes(7) + struct.pack(">HI", day, ms)
            + struct.pack(">H", payload_id) + body + bytes(8))


def test_aistechsat2_all_payloads_end_to_end(tmp_path):
    from mav_gss_lib.missions.aistechsat2 import telemetry as at

    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "aistechsat2", {},
    )
    assert runtime.walker is not None
    for payload_id, (name, _fields) in sorted(at.PAYLOADS.items()):
        frame = _csp_header() + _lume_frame(payload_id)
        result = runtime.process_rx(M5_META, frame)
        assert result.container_id == name, (payload_id, result.container_id)
        assert len(result.packet.parameters) == at.token_count(payload_id), name
        assert result.packet.flags.is_unknown is False


def test_aistechsat2_eps_values_end_to_end(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "aistechsat2", {},
    )
    frame = _csp_header(src=5, dest=9) + _lume_frame(2, {
        "vbatt": 7900,
        "cursun": 415,
        "battmode": 3,
        "counter_boot": 88,
        "output": bytes([1, 0, 1, 0, 0, 0, 0, 1]),
    })
    result = runtime.process_rx(M5_META, frame)
    values = {u.name: u for u in result.packet.parameters}
    assert result.container_id == "eps"
    assert values["eps.eps_vbatt"].value == 7900
    assert values["eps.eps_vbatt"].unit == "mV"
    assert values["eps.eps_cursun"].value == 415
    assert values["eps.eps_counter_boot"].value == 88
    assert values["eps.eps_output"].value == "0x0100010000000001"
    assert values["eps.eps_pus_day"].value == 9500
    assert values["eps.eps_pus_ms"].value == 43_200_123
    facts = result.packet.mission["facts"]
    assert facts["beacon"]["kind"] == "eps"
    assert facts["beacon"]["vbat_mv"] == 7900


def test_aistechsat2_obc_string_and_temps():
    from mav_gss_lib.missions.aistechsat2.telemetry import decode_beacon

    hk = decode_beacon({}, _lume_frame(1, {"temp": [-125, 231]}))
    assert hk is not None
    tokens = hk.tokens.split(b" ")
    assert b"AIST_fw_v2.1" in tokens
    assert b"-12.5" in tokens
    assert b"23.1" in tokens


def test_aistechsat2_unknown_payload_id_is_opaque():
    from mav_gss_lib.missions.aistechsat2.telemetry import decode_beacon

    frame = bytearray(_lume_frame(1))
    frame[24:26] = (99).to_bytes(2, "big")
    assert decode_beacon({}, bytes(frame)) is None
    assert decode_beacon({}, _lume_frame(2)[:40]) is None


def test_aistechsat2_yml_containers_match_field_table():
    """Every container's entry count equals the decoder's token count."""
    import yaml as _yaml

    from mav_gss_lib.missions.aistechsat2 import telemetry as at

    yml = Path(__file__).resolve().parent.parent / "mav_gss_lib" / "missions" / "aistechsat2" / "mission.yml"
    doc = _yaml.safe_load(yml.read_text(encoding="utf-8"))
    containers = doc["sequence_containers"]
    assert len(containers) == len(at.PAYLOADS)
    for payload_id, (name, _fields) in at.PAYLOADS.items():
        assert len(containers[name]["entry_list"]) == at.token_count(payload_id), name


def test_aistechsat2_matches_gr_satellites_lume():
    """The port must agree with the reference construct parser."""
    satellites = pytest.importorskip("satellites.telemetry.lume")

    frame_after_csp = _lume_frame(2, {
        "vbatt": 7900,
        "cursun": 415,
        "battmode": 3,
        "counter_boot": 88,
        "curout": [10, 20, 30, 40, 50, 60],
        "output": bytes([1, 0, 1, 0, 0, 0, 0, 1]),
    })
    parsed = satellites.lume.parse(_csp_header() + frame_after_csp)
    data = parsed.payload.data
    assert parsed.payload.id == 2
    assert data.vbatt == 7900
    assert data.cursun == 415
    assert data.battmode == 3
    assert data.counter_boot == 88
    assert list(data.curout) == [10, 20, 30, 40, 50, 60]
    assert list(data.output) == [1, 0, 1, 0, 0, 0, 0, 1]
    assert parsed.pus_time_field.day == 9500
    assert parsed.pus_time_field.milliseconds_of_day == 43_200_123

    from mav_gss_lib.missions.aistechsat2.telemetry import decode_beacon

    hk = decode_beacon({}, frame_after_csp)
    assert hk is not None
    tokens = hk.tokens.split(b" ")
    assert tokens[0] == b"9500"
    assert b"7900" in tokens
    assert b"0x0100010000000001" in tokens


# ---------------------------------------------------------------- decoder yml


def test_radio_decoder_has_2k4_mode5_branch_for_catsat():
    import yaml

    decoder = Path(__file__).resolve().parent.parent / "gnuradio" / "MAVERIC_DECODER.yml"
    doc = yaml.safe_load(decoder.read_text(encoding="utf-8"))
    branch = doc["transmitters"].get("2k4 FSK AX100 ASM+Golay downlink")
    assert branch is not None
    assert branch["baudrate"] == 2400
    assert branch["deviation"] == 750
    assert branch["framing"] == "AX100 ASM+Golay"
