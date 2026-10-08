"""SCION-X mission tests.

Frames are built from the published SCION-X ground-test frame (public
writeup). The real beacon field map is team-supplied and not in the
repository, so telemetry tests use a small invented map whose names follow
the same conventions (EPS_<rail><n>V Voltage, ... Temperature).
"""

import json
from pathlib import Path

import pytest

from mav_gss_lib.missions.scionx import telemetry
from mav_gss_lib.missions.scionx.mission import _IQ_DEFAULT_MARKER
from mav_gss_lib.platform.loader import discover_missions, load_mission_spec, load_mission_spec_from_split
from mav_gss_lib.platform.rx.pipeline import RxPipeline

REFERENCE = bytes.fromhex("""
84 9c 60 86 aa 40 60 84 9c 60 a6 86 b0 e1 03 f0
00 7c 08 3c 81 33 6a 00 02 01 00 00 00 00 00 20
00 00 00 2b 81 33 6a 00 00 00 00 00 00 03 02 00
00 44 09 00 20 10 00 00 c3 ad d0 00 99 bf 4e 04
8b c5 78 05 61 d7 f6 08 d4 a1 1f 00 00 00 00 00
""" + " 00" * 16 * 4 + """
00 00 00 00 00 00 00 00 00 00 00 00 00 00 08 26
08 26 08 26 28 26 20 26 20 00 20 00 20 00 20 00
20 00 a0 3d 78 ff 77 0f 9b ff 8a 1f 16 15 15 e7
b0 b0 b0 b0 19 18 1a e8 0c 40 02 48 1b 18 00 00
""" + " 00" * 16 * 4)

# what the burst decoder hands over for a late key-up: bytes 101..273 incl. FCS
TAIL_101 = REFERENCE[101:] + b"\x5a\xa5"

# invented layout over the ground-test frame (offsets from the APID, byte 16)
TEST_FIELDS = [
    {"name": "Early Counter", "var": "early_counter", "ctype": "uint16_t", "size": 2, "offset": 3, "endian": "Little"},
    {"name": "EPS_RAILB7V Voltage", "var": "EPS_RAILB7V_V", "ctype": "uint16_t", "size": 2, "offset": 187, "endian": "Little"},
    {"name": "EPS_PANEL0 Temperature", "var": "EPS_PANEL0_T", "ctype": "int8_t", "size": 1, "offset": 176, "endian": "Little"},
]


def _burst_meta(status="unverified", start=101, **extra):
    return {"transmitter": "SCION-X 9k6 NRZ (GSS burst decoder)",
            "burst": {"v": 1, "status": status, "start_byte": start, "crc": "unverifiable",
                      "ambiguous": False, **extra},
            "gss_burst_end": 1790000000.5}


@pytest.fixture()
def field_map(tmp_path, monkeypatch):
    path = tmp_path / "beacon_fields.json"
    path.write_text(json.dumps({"fields": TEST_FIELDS}))
    monkeypatch.setattr(telemetry, "FIELDS_PATH", path)
    return path


@pytest.fixture()
def no_field_map(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "FIELDS_PATH", tmp_path / "absent.json")


def _spec(tmp_path):
    return load_mission_spec({"mission": {"id": "scionx", "config": {}}, "platform": {}}, data_dir=tmp_path)


def _parse(spec, meta, raw):
    return spec.packets.parse(spec.packets.normalize(meta, raw))


def test_spec_is_rx_only_with_spec_root(tmp_path):
    spec = _spec(tmp_path)
    assert spec.id == "scionx"
    assert spec.commands is None
    assert spec.spec_root is not None
    assert [c.id for c in spec.spec_root.ui.rx_columns] == ["type", "check", "bytes", "fields", "src"]


def test_discoverable_for_mission_switcher():
    listed = {m["id"]: m["name"] for m in discover_missions()}
    assert listed.get("scionx") == "SCION-X"


def test_build_seeds_radio_tracking_and_turns_iq_recording_on_once(tmp_path):
    platform_cfg: dict = {"radio": {"iq_record": False}}      # platform default
    mission_cfg: dict = {}
    load_mission_spec_from_split(platform_cfg, "scionx", mission_cfg, data_dir=tmp_path)
    assert platform_cfg["rx"]["frequency"] == "437.500 MHz"
    assert platform_cfg["radio"]["script"] == "gnuradio/MAV_DUO.py"
    assert platform_cfg["radio"]["decoder_yml"] == "gnuradio/decoders/SCIONX_DECODER.yml"
    assert platform_cfg["radio"]["iq_record"] is True
    assert mission_cfg[_IQ_DEFAULT_MARKER] is True
    tracking = platform_cfg["tracking"]
    assert tracking["tle_fetch"]["identifier"] == "69880"
    assert tracking["tle"]["line1"].startswith("1 69880U")
    assert tracking["frequencies"]["rx_hz"] == 437_500_000.0
    assert mission_cfg["mission_name"] == "SCION-X"

    # operator turned it off and saved: the next boot keeps that choice
    platform_cfg["radio"]["iq_record"] = False
    load_mission_spec_from_split(platform_cfg, "scionx", mission_cfg, data_dir=tmp_path)
    assert platform_cfg["radio"]["iq_record"] is False


def test_decoder_profile_requests_the_burst_decoder():
    import yaml
    profile = yaml.safe_load(Path("gnuradio/decoders/SCIONX_DECODER.yml").read_text())
    assert profile["gss_burst_decoder"] == "scionx"
    assert profile["norad"] == 69880


def test_headerless_beacon_is_unverified_with_received_fields_only(tmp_path, field_map):
    spec = _spec(tmp_path)
    raw = TAIL_101
    packet = _parse(spec, _burst_meta(), raw)
    payload = packet.payload
    assert payload.kind == "beacon" and payload.status == "unverified"
    assert payload.src == ""                                  # header never radiated
    names = {u.name: u for u in payload.param_updates}
    # the early field (frame byte 19) was not received; the others were
    assert set(names) == {"eps.eps_railb7v_v", "eps.eps_panel0_t"}
    rail = int.from_bytes(REFERENCE[203:205], "little")
    assert names["eps.eps_railb7v_v"].value == round((rail >> 3) * 0.008, 3)
    assert names["eps.eps_railb7v_v"].unit == "V"
    assert names["eps.eps_panel0_t"].value == -80                 # 0xb0: disconnected sensor
    assert payload.implausible == ["eps.eps_panel0_t"]
    facts = packet.mission["facts"]["header"]
    assert facts == {"type": "BCN", "check": "UNVERIFIED", "bytes": "101-273",
                     "src": "", "dst": "", "fields": "2/3"}
    assert any(w.startswith("IMPLAUSIBLE (1)") for w in packet.warnings)
    flags = spec.packets.classify(packet)
    assert flags.is_unknown is False and flags.integrity_ok is None


def test_normalize_warns_unverified_and_ambiguous(tmp_path, field_map):
    spec = _spec(tmp_path)
    normalized = spec.packets.normalize(_burst_meta(ambiguous=True), TAIL_101)
    assert normalized.frame_type == "AX.25 NRZ"
    assert normalized.warnings[0] == "UNVERIFIED: starts at frame byte 101, no CRC to check"
    assert normalized.warnings[1].startswith("AMBIGUOUS")


def test_crc_verified_frame_has_callsigns(tmp_path, field_map):
    spec = _spec(tmp_path)
    packet = _parse(spec, _burst_meta(status="crc_pass", start=0, crc="pass"), REFERENCE)
    assert packet.payload.src == "BN0SCX" and packet.payload.dst == "BN0CU"
    assert packet.mission["facts"]["header"]["check"] == "CRC OK"
    assert spec.packets.classify(packet).integrity_ok is True
    assert len(packet.payload.param_updates) == 3


def test_stock_gr_satellites_ax25_frame_is_treated_as_verified(tmp_path, field_map):
    spec = _spec(tmp_path)
    packet = _parse(spec, {"transmitter": "9k6 FSK AX.25 downlink"}, REFERENCE)
    assert packet.payload.status == "crc_pass"
    assert packet.payload.src == "BN0SCX"


def test_undecoded_burst_is_kept_and_distinct(tmp_path, field_map):
    spec = _spec(tmp_path)
    a = _parse(spec, _burst_meta(status="undecoded", start=0), b"")
    meta_b = _burst_meta(status="undecoded", start=0)
    meta_b["gss_burst_end"] = 1790000210.5
    b = _parse(spec, meta_b, b"")
    assert a.payload.kind == "burst" and a.mission["facts"]["header"]["type"] == "BURST"
    assert a.mission["facts"]["header"]["check"] == "NO DECODE"
    assert spec.packets.classify(a).is_unknown is True
    assert spec.packets.classify(a).duplicate_key != spec.packets.classify(b).duplicate_key
    assert spec.packets.normalize(_burst_meta(status="undecoded", start=0), b"").warnings[0].startswith("NO DECODE")


def test_without_field_map_frames_still_log(tmp_path, no_field_map):
    spec = _spec(tmp_path)
    packet = _parse(spec, _burst_meta(), TAIL_101)
    assert packet.payload.param_updates == ()
    assert any("no local beacon field map" in w for w in packet.warnings)


def test_pipeline_publishes_mission_parameters_with_receive_time(tmp_path, field_map):
    spec = _spec(tmp_path)
    pipeline = RxPipeline(spec, None)
    result = pipeline.process(_burst_meta(), TAIL_101)
    params = {p.name: p for p in result.packet.parameters}
    assert set(params) == {"eps.eps_railb7v_v", "eps.eps_panel0_t"}
    assert all(p.ts_ms == result.packet.received_at_ms for p in params.values())


def test_eps_channels_stay_in_the_eps_group():
    f_rail = telemetry.BeaconField("EPS_UHFLINK7V Voltage", "EPS_UHFLINK7V_V", "uint16_t", 2, 0, "Little")
    f_radio = telemetry.BeaconField("UHF Temperature", "UHF_temp", "int8_t", 1, 2, "Little")
    names = telemetry.param_names((f_rail, f_radio))
    assert names[f_rail].startswith("eps.")
    assert names[f_radio].startswith("uhf.")
