"""Sharjahsat-1 mission tests.

The telemetry fixture is the real over-the-air frame received by GS-1 on
2026-07-10 05:37:23 UTC (first-light pass: Sharjahsat-1 TLE tracking,
MAV_DUO `9k6 FSK AX.25 G3RUH downlink` branch). Its interface-board RTC
and OBC unix clock agree to the second, which pins the field layout.
"""

from pathlib import Path

import pytest
import yaml

from mav_gss_lib.platform.loader import discover_missions, load_mission_spec
from mav_gss_lib.platform.runtime import PlatformRuntime


G3RUH_META = {"transmitter": "9k6 FSK AX.25 G3RUH downlink"}

TLM_FRAME = bytes.fromhex(
    "826c64aa9ea6e0826c60aa9ea66103f04553455250f600000000ff1ff90b0230"
    "3d00001385506a0b0632060006540810025b08360d1d00090058082b0d233705"
    "05100726c0050f8103170006035d030200030007038102800280027a03070006"
    "0347005c030a0011001800040309030200820355911aede3ffffeeff01000200"
    "0000000000000000000000000000000000000000000000000a0b0c003f030000"
    "e904000000000000000000000000000000000200030002000200020002000200"
    "0200020003000300030002000200020001000200020003000300020002000200"
    "0200010002000200070201020402f301a00203029c029e029c02f603f503f203"
    "9c029e029e029f029f029d027e030200"
)
_AX25_UI_HEADER = TLM_FRAME[:16]
# Same telemetry, packet counter 0 -> 1: distinct frame, distinct fingerprint.
TLM_FRAME_2 = TLM_FRAME[:22] + b"\x01" + TLM_FRAME[23:]
IMAGE_FRAME = _AX25_UI_HEADER + b"ESERA" + bytes([4]) + (7).to_bytes(4, "little") + b"\xde\xad\xbe\xef"
UNKNOWN_TM_FRAME = _AX25_UI_HEADER + b"ESERZ" + bytes([0]) + (9).to_bytes(4, "little")
TRUNCATED_TLM_FRAME = _AX25_UI_HEADER + b"ESERP" + bytes([246]) + (2).to_bytes(4, "little") + b"\x00" * 10


def _spec(tmp_path):
    return load_mission_spec(
        {"mission": {"id": "sharjahsat", "config": {}}, "platform": {}},
        data_dir=tmp_path,
    )


def test_spec_is_rx_only_with_spec_root(tmp_path):
    spec = _spec(tmp_path)
    assert spec.id == "sharjahsat"
    assert spec.commands is None
    assert spec.spec_root is not None
    assert "beacon_tlm" in spec.spec_root.sequence_containers
    assert spec.spec_root.ui is not None
    assert len(spec.spec_root.ui.rx_columns) >= 8


def test_discoverable_for_mission_switcher():
    listed = {m["id"]: m["name"] for m in discover_missions()}
    assert listed.get("sharjahsat") == "SharjahSat-1"


def test_normalize_strips_ax25_ui_header(tmp_path):
    spec = _spec(tmp_path)
    normalized = spec.packets.normalize(G3RUH_META, TLM_FRAME)
    assert normalized.frame_type == "AX.25"
    assert normalized.payload.startswith(b"ESERP")
    assert normalized.stripped_header == _AX25_UI_HEADER.hex(" ")
    assert normalized.raw == TLM_FRAME


def test_parse_telemetry_beacon(tmp_path):
    spec = _spec(tmp_path)
    packet = spec.packets.parse(spec.packets.normalize(G3RUH_META, TLM_FRAME))
    payload = packet.payload
    assert payload.kind == "telemetry"
    assert payload.src == "A60UOS"
    assert payload.dst == "A62UOS"
    assert payload.counter == 0
    assert packet.warnings == []

    facts = packet.mission["facts"]
    assert facts["header"]["type"] == "TLM"
    assert facts["system"]["obc_utc"] == "2026-07-10T05:37:23+00:00"
    assert facts["system"]["op_mode"] == "0x1fff"
    assert facts["system"]["restart_count"] == 3065
    assert facts["system"]["uptime_s"] == 15664
    assert facts["system"]["antenna_status"] == "0x0f"
    # Interface-board RTC independently agrees with the OBC unix clock.
    assert facts["rtc"]["utc"] == facts["system"]["obc_utc"]
    assert facts["battery"]["vbat_v"] == pytest.approx(8.067)
    assert facts["battery"]["ibat_ma"] == pytest.approx(337.2)
    assert facts["battery"]["temp_cells_c"][0] == pytest.approx(16.29)
    assert facts["eps"]["bus_v"] == pytest.approx(7.99)
    assert facts["eps"]["rail_12v_v"] == pytest.approx(12.114)
    assert facts["adcs"]["state"] == "0x55"
    assert facts["uhf"]["smps_temp_c"] == 10
    assert facts["sband"]["power"] == "OFF"
    assert facts["solar"]["illumination"] == "ECLIPSE"
    assert facts["solar"]["temp_min_c"] == pytest.approx(-25.5)
    assert facts["solar"]["temp_max_c"] == pytest.approx(60.36)
    assert len(facts["solar"]["tbcrb_c"]) == 9


def test_classify_beacon_flags(tmp_path):
    spec = _spec(tmp_path)
    packet = spec.packets.parse(spec.packets.normalize(G3RUH_META, TLM_FRAME))
    flags = spec.packets.classify(packet)
    assert flags.is_unknown is False
    assert flags.is_uplink_echo is False
    assert flags.duplicate_key  # stable fingerprint
    again = spec.packets.classify(
        spec.packets.parse(spec.packets.normalize(G3RUH_META, TLM_FRAME))
    )
    assert flags.duplicate_key == again.duplicate_key
    other = spec.packets.classify(
        spec.packets.parse(spec.packets.normalize(G3RUH_META, TLM_FRAME_2))
    )
    assert flags.duplicate_key != other.duplicate_key
    assert spec.packets.match_verifiers(None, [], now_ms=0) == []


def test_seed_tracking_defaults_gap_fill():
    from mav_gss_lib.missions.sharjahsat.tracking_defaults import (
        SHARJAHSAT_FREQ_HZ,
        seed_tracking_defaults,
    )

    cfg: dict = {}
    seed_tracking_defaults(cfg)
    tracking = cfg["tracking"]
    assert tracking["tle"]["name"] == "SHARJAHSAT-1"
    assert tracking["tle"]["method"] == "seed"
    assert tracking["tle_fetch"]["identifier"] == "55104"
    assert tracking["frequencies"]["rx_hz"] == SHARJAHSAT_FREQ_HZ
    assert tracking["stations"][0]["id"] == "usc"


def test_seed_tracking_defaults_respects_operator_values():
    from mav_gss_lib.missions.sharjahsat.tracking_defaults import seed_tracking_defaults

    cfg = {"tracking": {
        "tle": {"line1": "OPERATOR1", "line2": "OPERATOR2"},
        "frequencies": {"rx_hz": 437.4e6},
    }}
    seed_tracking_defaults(cfg)
    assert cfg["tracking"]["tle"]["line1"] == "OPERATOR1"
    assert "method" not in cfg["tracking"]["tle"]
    assert cfg["tracking"]["frequencies"]["rx_hz"] == 437.4e6
    assert cfg["tracking"]["frequencies"]["tx_hz"] == 437_325_000.0


def test_build_seeds_rx_frequency_and_tracking(tmp_path):
    from mav_gss_lib.platform.loader import load_mission_spec_from_split

    platform_cfg: dict = {}
    mission_cfg: dict = {}
    load_mission_spec_from_split(platform_cfg, "sharjahsat", mission_cfg, data_dir=tmp_path)
    assert platform_cfg["rx"]["frequency"] == "437.325 MHz"
    assert platform_cfg["radio"]["script"] == "gnuradio/MAV_DUO.py"
    assert platform_cfg["tracking"]["frequencies"]["rx_hz"] == 437_325_000.0
    assert mission_cfg["mission_name"] == "SharjahSat-1"


def test_mission_yml_parses_standalone():
    from mav_gss_lib.platform.spec import parse_yaml

    yml = Path("mav_gss_lib/missions/sharjahsat/mission.yml")
    mission = parse_yaml(yml, plugins={})
    container = mission.sequence_containers["beacon_tlm"]
    entry_names = [entry.name for entry in container.entry_list]
    assert len(entry_names) == 51
    assert set(entry_names) == set(mission.parameters)


def test_radio_decoder_has_sharjahsat_branch():
    yml = Path("gnuradio/MAVERIC_DECODER.yml")
    raw = yaml.safe_load(yml.read_text(encoding="utf-8"))
    branch = raw["transmitters"]["9k6 FSK AX.25 G3RUH downlink"]
    assert branch["baudrate"] == 9600
    assert branch["framing"] == "AX.25 G3RUH"


def test_end_to_end_parameters_via_platform_runtime(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "sharjahsat", {},
    )
    assert runtime.walker is not None

    result = runtime.process_rx(G3RUH_META, TLM_FRAME)
    packet = result.packet
    values = {update.name: update for update in packet.parameters}
    assert len(values) == 51
    assert values["sys.obc_utc"].value == "2026-07-10T05:37:23+00:00"
    assert values["sys.op_mode"].value == "0x1fff"
    assert values["sys.restart_count"].value == 3065
    assert values["sys.uptime"].value == 15664
    assert values["sys.antenna"].value == "0x0f"
    assert values["obc.obc_temp1"].value == pytest.approx(15.47)
    assert values["obc.plat_3v3"].value == pytest.approx(3.382)
    assert values["batt.vbat"].value == pytest.approx(8.067)
    assert values["batt.vbat"].unit == "V"
    assert values["batt.ibat"].value == pytest.approx(337.2)
    assert values["batt.ibat"].unit == "mA"
    assert values["batt.temp_cell1"].value == pytest.approx(16.29)
    assert values["eps.bus_v"].value == pytest.approx(7.99)
    assert values["eps.rail_3v3"].value == pytest.approx(3.337)
    assert values["eps.rail_3v3_i"].value == pytest.approx(484.2)
    assert values["eps.rail_5v"].value == pytest.approx(5.044)
    assert values["eps.rail_12v"].value == pytest.approx(12.114)
    assert values["adcs.state"].value == "0x55"
    assert values["adcs.yaw"].value == pytest.approx(-0.18)
    assert values["adcs.rate_roll"].value == pytest.approx(0.0)
    assert values["uhf.smps_temp"].value == 10
    assert values["uhf.pa_temp"].value == 11
    assert values["uhf.uhf_5v"].value == pytest.approx(5.028)
    assert values["sband.power"].value == "OFF"
    assert values["solar.illumination"].value == "ECLIPSE"
    assert values["solar.vdiode"].value == pytest.approx(8.04)
    assert values["solar.idiode"].value == pytest.approx(29.3)
    assert values["solar.array_i"].value == pytest.approx(38.1)
    assert values["solar.temp_min"].value == pytest.approx(-25.5)
    assert values["solar.temp_max"].value == pytest.approx(60.36)
    assert packet.flags.is_unknown is False
    assert result.container_id == "beacon_tlm"


def test_image_frame_is_opaque_product(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "sharjahsat", {},
    )
    result = runtime.process_rx(G3RUH_META, IMAGE_FRAME)
    packet = result.packet
    facts = packet.mission["facts"]
    assert facts["header"]["type"] == "IMG"
    assert facts["header"]["counter"] == 7
    assert facts["image"]["length"] == 4
    assert packet.parameters == ()
    assert packet.flags.is_unknown is False


def test_unknown_tm_id_is_flagged(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "sharjahsat", {},
    )
    packet = runtime.process_rx(G3RUH_META, UNKNOWN_TM_FRAME).packet
    assert packet.flags.is_unknown is True
    assert any("unknown tm_id 0x5a" in w for w in packet.warnings)


def test_truncated_telemetry_is_flagged(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "sharjahsat", {},
    )
    packet = runtime.process_rx(G3RUH_META, TRUNCATED_TLM_FRAME).packet
    assert packet.flags.is_unknown is True
    assert any("truncated telemetry" in w for w in packet.warnings)
    assert packet.parameters == ()


def test_garbage_frame_does_not_crash(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "sharjahsat", {},
    )
    result = runtime.process_rx(G3RUH_META, b"\x00\x01\x02garbage-no-header")
    packet = result.packet
    assert packet.flags.is_unknown is True
    assert packet.parameters == ()
    assert packet.warnings
