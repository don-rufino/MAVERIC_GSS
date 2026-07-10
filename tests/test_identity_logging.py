from mav_gss_lib.platform import MissionSpec
from mav_gss_lib.platform.log_records import rx_packet_record
from mav_gss_lib.platform.contract.packets import PacketEnvelope, PacketFlags


def test_rx_record_carries_identity():
    pkt = PacketEnvelope(
        seq=1,
        received_at_ms=0,
        frame_type="AX.25",
        raw=b"abc",
        payload=b"a",
        transport_meta={"transmitter": "gr"},
        warnings=[],
        mission_payload={},
        flags=PacketFlags(),
    )
    spec = MissionSpec(id="test", name="Test", packets=None, config=None)
    record = rx_packet_record(
        spec, pkt, version="1.2.3",
        session_id="session_test",
        operator="irfan", station="GS-0",
    )
    assert record["operator"] == "irfan"
    assert record["station"] == "GS-0"
    assert record["event_kind"] == "rx_packet"
    assert record["session_id"] == "session_test"


import json
import os

from mav_gss_lib.logging import SessionLog
from mav_gss_lib.platform.log_records import tx_command_record


def test_tx_record_carries_identity(tmp_path):
    log = SessionLog(str(tmp_path), zmq_addr="tcp://127.0.0.1:52002", version="1.2.3")
    try:
        record = tx_command_record(
            1,
            cmd_id="PING",
            mission_facts={"cmd": "ping"},
            parameters=[],
            raw_cmd=b"\x01\x02",
            wire=b"\x01\x02",
            session_id=log.session_id,
            ts_ms=1_700_000_000_000,
            version="1.2.3",
            operator="irfan", station="GS-0",
            frame_label="RAW",
            log_fields={},
        )
        log.write_mission_command(record, raw_cmd=b"\x01\x02", wire=b"\x01\x02", log_text=[])
    finally:
        log.close()

    with open(log.jsonl_path) as f:
        rec = json.loads(f.readline())
    assert rec["operator"] == "irfan"
    assert rec["station"] == "GS-0"
    assert rec["frame_label"] == "RAW"
    assert "uplink_mode" not in rec


import re


def test_rx_filename_includes_station_and_operator(tmp_path):
    from mav_gss_lib.logging import SessionLog
    log = SessionLog(str(tmp_path), zmq_addr="tcp://127.0.0.1:52001", version="1.2.3",
                     station="GS-0", operator="irfan")
    try:
        name = os.path.basename(log.jsonl_path)
        # shape: session_<ts>_<station>_<operator>_<tag>.jsonl (tag empty)
        assert re.match(r"session_\d{8}_\d{6}_GS-0_irfan(?:_.*)?\.jsonl$", name), name
    finally:
        log.close()


def test_filename_includes_mission_id(tmp_path):
    from mav_gss_lib.logging import SessionLog
    log = SessionLog(str(tmp_path), zmq_addr="tcp://127.0.0.1:52001", version="1.2.3",
                     mission_id="sharjahsat", station="GS-0", operator="irfan")
    try:
        name = os.path.basename(log.jsonl_path)
        # shape: session_<ts>_<mission>_<station>_<operator>.jsonl — mission
        # sits after the date/time so /api/logs keeps parsing parts[1].
        assert re.match(r"session_\d{8}_\d{6}_sharjahsat_GS-0_irfan\.jsonl$", name), name
    finally:
        log.close()


def test_new_session_filename_keeps_mission_id(tmp_path):
    from mav_gss_lib.logging import SessionLog
    log = SessionLog(str(tmp_path), zmq_addr="tcp://127.0.0.1:52001", version="1.2.3",
                     mission_id="astrocast")
    try:
        prepared = log.prepare_new_session("pass2")
        log.commit_new_session(prepared)
        name = os.path.basename(log.jsonl_path)
        assert re.match(r"session_\d{8}_\d{6}_astrocast_pass2\.jsonl$", name), name
    finally:
        log.close()
