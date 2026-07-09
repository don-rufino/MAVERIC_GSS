"""Astrocast 0.1 mission tests.

Fixtures are real over-the-air frames decoded from Daniel Estevez's
satellite-recordings (astrocast.wav, 2019-03-04 pass) via gr-satellites;
the constructed bytes were verified equal to the KISS output of
`gr_satellites "Astrocast 0.1" --wavfile astrocast.wav --kiss_out`.
"""

from datetime import datetime, timezone

import pytest

from mav_gss_lib.platform.loader import load_mission_spec
from mav_gss_lib.platform.runtime import PlatformRuntime


NRZI_META = {"transmitter": "1k2 FSK FX.25 NRZ-I downlink"}
NINE_K6_META = {"transmitter": "9k6 FSK downlink"}

_AX25_UI_HEADER = bytes.fromhex(
    "86a24040404060"   # dest CQ, SSID 0
    "9084728ea68c61"   # src HB9GSF, SSID 0, end-of-address bit
    "03f0"             # UI control, PID F0
)
_BEACON_TEXT_1 = (
    "$GPRMC,220516.38,A,5133.82,N,02311.12,W,13606,054.7,270816,020.3,W*"
    "$HK,0x05F739466C19,3.415,57,12,-85,9338,0xEC*"
)
_BEACON_TEXT_2 = (
    "$GPRMC,220516.38,A,5133.82,N,02311.12,W,13606,054.7,270816,020.3,W*"
    "$HK,0x05F7398280C6,3.413,57,12,-78,9378,0xEC*"
)
BEACON_FRAME_1 = _AX25_UI_HEADER + _BEACON_TEXT_1.encode("ascii").ljust(171, b" ")
BEACON_FRAME_2 = _AX25_UI_HEADER + _BEACON_TEXT_2.encode("ascii").ljust(171, b" ")
# 9k6 CCSDS frames are opaque; a synthetic 1115-byte body exercises the path.
NINE_K6_FRAME = bytes.fromhex("001917431ffe0807e0190019100319") + b"\x00" * 1100


def _spec(tmp_path):
    return load_mission_spec(
        {"mission": {"id": "astrocast", "config": {}}, "platform": {}},
        data_dir=tmp_path,
    )


def test_spec_is_rx_only_with_spec_root(tmp_path):
    spec = _spec(tmp_path)
    assert spec.id == "astrocast"
    assert spec.commands is None
    assert spec.spec_root is not None
    assert "beacon_hk" in spec.spec_root.sequence_containers
    assert spec.spec_root.ui is not None
    assert len(spec.spec_root.ui.rx_columns) >= 5


def test_normalize_strips_ax25_ui_header(tmp_path):
    spec = _spec(tmp_path)
    normalized = spec.packets.normalize(NRZI_META, BEACON_FRAME_1)
    assert normalized.frame_type == "FX.25"
    assert normalized.payload.startswith(b"$GPRMC,")
    assert normalized.stripped_header == _AX25_UI_HEADER.hex(" ")
    assert normalized.raw == BEACON_FRAME_1


def test_parse_beacon_hk_and_gps(tmp_path):
    spec = _spec(tmp_path)
    packet = spec.packets.parse(spec.packets.normalize(NRZI_META, BEACON_FRAME_1))
    payload = packet.payload
    assert payload.kind == "beacon"
    assert payload.src == "HB9GSF"
    assert payload.dst == "CQ"
    hk = payload.hk
    assert hk.clock_hex == "0x05F739466C19"
    assert hk.clock_utc == datetime(2019, 3, 4, 10, 15, 34, 422256, tzinfo=timezone.utc)
    assert hk.voltage_v == pytest.approx(3.415)
    assert hk.current_ma == 57
    assert hk.temp_c == 12
    assert hk.rssi_dbm == -85
    assert hk.afc_hz == 9338
    assert hk.flags == "0xEC"
    assert payload.gps["status"] == "A"
    facts = packet.mission["facts"]
    assert facts["header"]["type"] == "BCN"
    assert facts["beacon"]["voltage_v"] == pytest.approx(3.415)
    assert facts["beacon"]["rssi_dbm"] == -85


def test_classify_beacon_flags(tmp_path):
    spec = _spec(tmp_path)
    packet = spec.packets.parse(spec.packets.normalize(NRZI_META, BEACON_FRAME_1))
    flags = spec.packets.classify(packet)
    assert flags.is_unknown is False
    assert flags.is_uplink_echo is False
    assert flags.duplicate_key  # stable fingerprint
    again = spec.packets.classify(
        spec.packets.parse(spec.packets.normalize(NRZI_META, BEACON_FRAME_1))
    )
    assert flags.duplicate_key == again.duplicate_key
    other = spec.packets.classify(
        spec.packets.parse(spec.packets.normalize(NRZI_META, BEACON_FRAME_2))
    )
    assert flags.duplicate_key != other.duplicate_key
    assert spec.packets.match_verifiers(None, [], now_ms=0) == []


def test_seed_tracking_defaults_gap_fill():
    from mav_gss_lib.missions.astrocast.tracking_defaults import (
        ASTROCAST_FREQ_HZ,
        seed_tracking_defaults,
    )

    cfg: dict = {}
    seed_tracking_defaults(cfg)
    tracking = cfg["tracking"]
    assert tracking["tle"]["name"] == "ASTROCAST 0.1"
    assert tracking["tle"]["method"] == "seed"
    assert tracking["tle_fetch"]["identifier"] == "43798"
    assert tracking["frequencies"]["rx_hz"] == ASTROCAST_FREQ_HZ
    assert tracking["stations"][0]["id"] == "usc"


def test_seed_tracking_defaults_respects_operator_values():
    from mav_gss_lib.missions.astrocast.tracking_defaults import seed_tracking_defaults

    cfg = {"tracking": {
        "tle": {"line1": "OPERATOR1", "line2": "OPERATOR2"},
        "frequencies": {"rx_hz": 437.2e6},
    }}
    seed_tracking_defaults(cfg)
    assert cfg["tracking"]["tle"]["line1"] == "OPERATOR1"
    assert "method" not in cfg["tracking"]["tle"]
    assert cfg["tracking"]["frequencies"]["rx_hz"] == 437.2e6
    assert cfg["tracking"]["frequencies"]["tx_hz"] == 437_150_000.0


def test_build_seeds_rx_frequency_and_tracking(tmp_path):
    from mav_gss_lib.platform.loader import load_mission_spec_from_split

    platform_cfg: dict = {}
    mission_cfg: dict = {}
    load_mission_spec_from_split(platform_cfg, "astrocast", mission_cfg, data_dir=tmp_path)
    assert platform_cfg["rx"]["frequency"] == "437.150 MHz"
    assert platform_cfg["radio"]["script"] == "gnuradio/MAV_ASTROCAST.py"
    assert platform_cfg["tracking"]["frequencies"]["rx_hz"] == 437_150_000.0
    assert mission_cfg["mission_name"] == "Astrocast 0.1"


def test_mission_yml_parses_standalone():
    from pathlib import Path
    from mav_gss_lib.platform.spec import parse_yaml

    yml = Path("mav_gss_lib/missions/astrocast/mission.yml")
    mission = parse_yaml(yml, plugins={})
    assert "beacon_hk" in mission.sequence_containers
    assert set(mission.parameters) == {
        "clock_utc", "voltage", "current", "temperature",
        "rssi", "afc_offset", "hk_flags",
    }


def test_end_to_end_parameters_via_platform_runtime(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "astrocast", {},
    )
    assert runtime.walker is not None

    result = runtime.process_rx(NRZI_META, BEACON_FRAME_1)
    packet = result.packet
    values = {update.name: update for update in packet.parameters}
    assert values["beacon.voltage"].value == pytest.approx(3.415)
    assert values["beacon.voltage"].unit == "V"
    assert values["beacon.current"].value == 57
    assert values["beacon.temperature"].value == 12
    assert values["beacon.rssi"].value == -85
    assert values["beacon.afc_offset"].value == 9338
    assert values["beacon.clock_utc"].value == "2019-03-04T10:15:34+00:00"
    assert values["beacon.hk_flags"].value == "0xEC"
    assert packet.flags.is_unknown is False


def test_9k6_frame_is_opaque_telemetry(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "astrocast", {},
    )
    result = runtime.process_rx(NINE_K6_META, NINE_K6_FRAME)
    packet = result.packet
    assert packet.frame_type == "CCSDS-RS"
    assert packet.mission["facts"]["header"]["type"] == "TLM"
    assert packet.parameters == ()
    assert packet.flags.is_unknown is False


def test_garbage_fx25_frame_does_not_crash(tmp_path):
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "astrocast", {},
    )
    result = runtime.process_rx(NRZI_META, b"\x00\x01\x02garbage-no-sentences")
    packet = result.packet
    assert packet.flags.is_unknown is True
    assert packet.parameters == ()
    assert packet.warnings  # header-not-found and/or no-sentence warning
