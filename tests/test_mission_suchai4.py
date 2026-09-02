"""SUCHAI-4 mission tests.

The three fixture frames are real over-the-air captures GT_MAV demodulated
on 2026-08-28 (NORAD 69911, "Transporter-17 Object AU" — believed
SUCHAI-4, officially unclaimed). All three decode as a consistent
big-endian CSP v1 header (prio=2, src=1, dest=30, dport=20), which pins
this mission's CSP endianness/field values against real data. No public
housekeeping payload format exists yet, so this does not attempt to pin
any telemetry field beyond the CSP header — see missions/suchai4/README.md.
"""

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
