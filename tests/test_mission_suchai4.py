"""SUCHAI-4 mission tests.

The three fixture frames are real over-the-air captures GT_MAV demodulated
on 2026-08-28 (NORAD 69911, "Transporter-17 Object AU" — believed
SUCHAI-4, officially unclaimed). All three decode as a consistent
big-endian CSP v1 header (prio=2, src=1, dest=30, dport=20), which pins
this mission's CSP endianness/field values against real data. They also
all decode as type-105 status beacons — see missions/suchai4/telemetry.py
for the field-level golden test and missions/suchai4/README.md for the
source and validation story.
"""

from datetime import datetime
from pathlib import Path

from mav_gss_lib.platform import PlatformRuntime
from mav_gss_lib.platform.loader import discover_missions, load_mission_spec


ASM_GOLAY_META = {"transmitter": "4k8 FSK AX100 ASM+Golay downlink"}

# First real frame received, 2026-08-28T07:41:34Z.
GOLDEN_FRAME = bytes.fromhex(
    "83e538010000690100000001000000006a6a33e86a6a33e800000004000004c3"
    "000000010000004e000d028000023f8000000000000000006a25acd500000000"
    "00000000000000001a0fe7d000000003000012c0000000780000205a00000000"
    "000000684196cccd000000730000000200000000000000000000000000000000"
    "00000003ffffffff00001a2c0003f48000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000631b8a56f76dddbe"
)
# Second and third frames, same session — confirm the CSP header is
# stable across independent captures, not a one-off coincidence.
FRAME_2 = bytes.fromhex(
    "83e529010000690100000001000000006a6a34616a6a346100000004000004c3"
    "000000010000004e000d029700023f8400000000000000006a25acd500000000"
    "00000000000000001a0fe7d000000003000012c0000000780000205000000000"
    "000000684196cccd000000780000000200000000000000000000000000000000"
    "00000003ffffffff00001a2c0003f48000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000088cfd0d7634ddd72"
)
FRAME_3 = bytes.fromhex(
    "83e53a010000690100000001000000006a6a34da6a6a34da00000004000004c3"
    "000000010000004e000d02ae00023f8800000000000000006a25acd500000000"
    "00000000000000001a0fe7d000000003000012c0000000780000205000000000"
    "0000005c41960000000000780000000200000000000000000000000000000000"
    "00000003ffffffff00001a2c0003f48000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000d8f2fecbaffd88a6"
)


def _spec(tmp_path):
    return load_mission_spec(
        {"mission": {"id": "suchai4", "config": {}}, "platform": {}},
        data_dir=tmp_path,
    )


def test_spec_is_rx_only_with_spec_root(tmp_path):
    spec = _spec(tmp_path)
    assert spec.id == "suchai4"
    assert spec.commands is None
    assert spec.spec_root is not None
    assert spec.spec_root.ui is not None
    assert len(spec.spec_root.ui.rx_columns) >= 4


def test_discoverable_for_mission_switcher():
    listed = {m["id"]: m["name"] for m in discover_missions()}
    assert "suchai4" in listed


def test_golden_frames_decode_consistent_csp_header(tmp_path):
    spec = _spec(tmp_path)
    for frame in (GOLDEN_FRAME, FRAME_2, FRAME_3):
        normalized = spec.packets.normalize(ASM_GOLAY_META, frame)
        assert normalized.frame_type == "ASM+GOLAY"
        packet = spec.packets.parse(normalized)
        csp = packet.payload.csp
        assert csp is not None
        assert (csp["prio"], csp["src"], csp["dest"], csp["dport"]) == (2, 1, 30, 20)
        flags = spec.packets.classify(packet)
        assert flags.is_unknown is False


def test_distinct_frames_get_distinct_fingerprints(tmp_path):
    spec = _spec(tmp_path)
    fingerprints = set()
    for frame in (GOLDEN_FRAME, FRAME_2, FRAME_3):
        normalized = spec.packets.normalize(ASM_GOLAY_META, frame)
        packet = spec.packets.parse(normalized)
        fingerprints.add(packet.payload.fingerprint)
    assert len(fingerprints) == 3


def test_golden_frame_no_longer_flagged_implausible(tmp_path):
    """dest=30 is suchai_4_ground_station (per suchai4.ksy), not noise —
    regression guard for the mission's max_plausible_dest override."""
    spec = _spec(tmp_path)
    normalized = spec.packets.normalize(ASM_GOLAY_META, GOLDEN_FRAME)
    packet = spec.packets.parse(normalized)
    assert "implausible CSP src/dest" not in packet.payload.warnings


def test_golden_beacon_decodes_end_to_end(tmp_path):
    """First frame ever decoded from this mission — full parameter table."""
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "suchai4", {},
    )
    result = runtime.process_rx(ASM_GOLAY_META, GOLDEN_FRAME)
    values = {u.name: u.value for u in result.packet.parameters}
    assert result.container_id == "beacon"
    assert len(values) == 39
    assert values["tm.timestamp"] == "2026-07-29T17:10:00+00:00"
    assert values["obc.rtc_date_time"] == "2026-07-29T17:10:00+00:00"
    assert values["obc.last_reset"] == 4
    assert values["obc.reset_counter"] == 78
    assert values["obc.executed_cmds"] == 852608
    assert values["obc.failed_cmds"] == 147328
    assert values["obc.temp_1"] == 18.85  # rendered as an ascii token, 2dp
    assert values["com.freq"] == 437250000
    assert values["com.baud"] == 4800
    assert values["com.last_tc"] == "2026-06-07T17:39:33+00:00"
    assert values["eps.vbatt"] == 8282
    assert values["eps.temp_bat0"] == 115
    assert values["mm.sciencemode"] == "0xffffffff"
    facts = result.packet.mission["facts"]
    assert facts["header"]["type"] == "BCN"
    assert facts["beacon"]["node_name"] == "suchai_4_obc"
    assert facts["beacon"]["vbat_mv"] == 8282
    assert result.packet.flags.is_unknown is False


def test_second_and_third_frames_show_monotonic_counters(tmp_path):
    """Independent evidence the decode is right, not coincidence: command
    counters only increase and the onboard clock advances by exactly the
    beacon period (120s) between frames captured ~2 minutes apart."""
    runtime = PlatformRuntime.from_split(
        {"logs": {"dir": str(tmp_path)}}, "suchai4", {},
    )
    counts = []
    for frame in (GOLDEN_FRAME, FRAME_2, FRAME_3):
        result = runtime.process_rx(ASM_GOLAY_META, frame)
        values = {u.name: u.value for u in result.packet.parameters}
        counts.append((values["tm.timestamp"], values["obc.executed_cmds"]))
    timestamps = [int(datetime.fromisoformat(ts).timestamp()) for ts, _ in counts]
    assert timestamps[1] - timestamps[0] == 121
    assert timestamps[2] - timestamps[1] == 121
    assert counts[0][1] < counts[1][1] < counts[2][1]


def test_suchai4_yml_containers_match_field_table():
    """beacon is the only container; its entries mirror telemetry.py."""
    import yaml as _yaml

    from mav_gss_lib.missions.suchai4 import telemetry as st

    yml = Path(__file__).resolve().parent.parent / "mav_gss_lib" / "missions" / "suchai4" / "mission.yml"
    doc = _yaml.safe_load(yml.read_text(encoding="utf-8"))
    containers = doc["sequence_containers"]
    assert list(containers) == ["beacon"]
    beacon = containers["beacon"]
    assert beacon["restriction_criteria"]["packet"]["kind"] == "hk"
    assert tuple(e["name"] for e in beacon["entry_list"]) == st.token_names()


def test_short_payload_is_opaque():
    from mav_gss_lib.missions.suchai4.telemetry import decode_beacon

    # BEACON_SIZE is 164B; the golden payload (208B after the CSP header)
    # has ~44B of trailing frame padding, so this must cut well below 164
    # to actually exercise the short-payload path.
    assert decode_beacon({"flags": 1}, GOLDEN_FRAME[4:100]) is None


def test_wrong_tm_type_is_opaque():
    from mav_gss_lib.missions.suchai4.telemetry import decode_beacon

    payload = bytearray(GOLDEN_FRAME[4:])
    payload[2] = 0x10  # not 105 (suchai_4_beacon)
    assert decode_beacon({"flags": 1}, bytes(payload)) is None


def test_wrong_node_is_opaque():
    from mav_gss_lib.missions.suchai4.telemetry import decode_beacon

    payload = bytearray(GOLDEN_FRAME[4:])
    payload[3] = 30  # not 1 (suchai_4_obc) — e.g. addressed FROM ground
    assert decode_beacon({"flags": 1}, bytes(payload)) is None
