"""Envelope-stability guardrail for the unified JSONL logging schema.

Every JSONL line (rx_packet, parameter, tx_command) must carry the full
envelope (`event_id`, `event_kind`, `session_id`, `ts_ms`, `ts_iso`, `seq`,
`v`, `mission_id`, `operator`, `station`). Missing keys break SQL ingest
on the other team's side, so the test fails fast instead of letting the
drift go unnoticed.
"""

from __future__ import annotations

import json
import tempfile

from mav_gss_lib.logging import SessionLog
from mav_gss_lib.platform import MissionSpec
from mav_gss_lib.platform.contract.packets import PacketEnvelope, PacketFlags
from mav_gss_lib.platform.contract.parameters import ParamUpdate
from mav_gss_lib.platform.log_records import (
    parameter_records,
    rx_packet_record,
    tx_command_record,
)


_ENVELOPE_KEYS = {
    "event_id", "event_kind", "session_id", "ts_ms", "ts_iso",
    "seq", "v", "mission_id", "operator", "station",
}

_ALLOWED_KINDS = {"rx_packet", "tx_command", "parameter", "alarm", "radio", "tracking",
                   "tracking_sample"}


def _assert_envelope(rec: dict) -> None:
    missing = _ENVELOPE_KEYS - rec.keys()
    assert not missing, f"record missing envelope keys {missing}: {rec}"
    assert rec["event_kind"] in _ALLOWED_KINDS, (
        f"unknown event_kind {rec['event_kind']}"
    )
    assert isinstance(rec["event_id"], str) and rec["event_id"], rec
    assert isinstance(rec["ts_ms"], int), rec
    assert isinstance(rec["ts_iso"], str) and rec["ts_iso"], rec
    assert isinstance(rec["seq"], int), rec


def _make_packet() -> PacketEnvelope:
    return PacketEnvelope(
        seq=42,
        received_at_ms=1714053603500,
        frame_type="ASM+GOLAY",
        raw=b"\x01\x02\x03\x04",
        payload=b"\x02\x03",
        transport_meta={"transmitter": "probe"},
        warnings=[],
        mission_payload={},
        mission={"id": "maveric", "cmd_id": "probe", "facts": {"header": {}}},
        flags=PacketFlags(),
        parameters=(
            ParamUpdate(name="eps.vbatt", value=7.42,
                        ts_ms=1714053603500, unit="V"),
            ParamUpdate(name="eps.temp_batt", value=18.3,
                        ts_ms=1714053603500, unit="C"),
            ParamUpdate(name="gnc.heartbeat", value=1,
                        ts_ms=1714053603501, display_only=True),
        ),
    )


def _make_spec() -> MissionSpec:
    return MissionSpec(id="maveric", name="MAVERIC", packets=None, config=None)


def test_rx_packet_envelope_shape():
    spec = _make_spec()
    pkt = _make_packet()
    record = rx_packet_record(
        spec, pkt, "5.7.0",
        session_id="session_20260423_140000",
        mission_id="maveric", operator="irfan", station="GS-0",
    )
    _assert_envelope(record)
    assert record["event_kind"] == "rx_packet"
    assert record["inner_hex"] == "01020304"
    assert record["inner_len"] == 4
    assert "raw_hex" not in record
    assert "size" not in record
    assert "wire_hex" not in record    # RX has no preserved outer frame
    assert "wire_len" not in record
    assert record["frame_label"] == pkt.frame_type
    assert "frame_type" not in record
    assert "warnings" in record
    assert record["mission"] == {"id": "maveric", "cmd_id": "probe", "facts": {"header": {}}}
    assert "_rendering" not in record
    assert "telemetry" not in record


def test_parameter_records_envelope_shape():
    pkt = _make_packet()
    rows = list(parameter_records(
        pkt,
        session_id="session_20260423_140000",
        rx_event_id="parent_event_id",
        version="5.7.0",
        mission_id="maveric", operator="irfan", station="GS-0",
    ))
    assert len(rows) == 3
    for row in rows:
        _assert_envelope(row)
        assert row["event_kind"] == "parameter"
        assert row["rx_event_id"] == "parent_event_id"
        assert row["seq"] == 42
        assert isinstance(row["ts_ms"], int)
        assert row["v"] == "5.7.0"
        assert "domain" not in row
        assert "key" not in row
    assert {row["name"] for row in rows} == {"eps.vbatt", "eps.temp_batt", "gnc.heartbeat"}
    display_only = next(row for row in rows if row["name"] == "gnc.heartbeat")
    assert display_only["display_only"] is True
    persisted = [row for row in rows if row["name"] != "gnc.heartbeat"]
    assert all(row["display_only"] is False for row in persisted)


def test_tx_command_envelope_shape():
    with tempfile.TemporaryDirectory() as tmp:
        log = SessionLog(tmp, zmq_addr="tcp://127.0.0.1:52002", version="5.7.0",
                    mission_id="maveric", station="GS-0", operator="irfan")
        try:
            raw_cmd = b"\x01\x02\x03"
            wire = b"\x01\x02\x03\x04\x05"
            record = tx_command_record(
                1,
                cmd_id="com_ping",
                # Canonical post-codec shape: header has no cmd_id duplicate;
                # protocol uses inner_len (not wire_len) for the inner-CSP length.
                mission_facts={
                    "header": {"dest": "EPS", "src": "GS", "echo": "NONE", "ptype": "CMD", "args": ""},
                    "protocol": {"args_hex": "", "args_len": 0, "inner_len": 3},
                },
                # One real parameter row with ts_ms=0 — proves the platform
                # logger strips it.
                parameters=[
                    {"name": "module", "value": 0, "unit": "", "display_only": False, "ts_ms": 0},
                ],
                raw_cmd=raw_cmd,
                wire=wire,
                session_id=log.session_id,
                ts_ms=1_700_000_000_000,
                version="5.7.0",
                mission_id="maveric", operator="irfan", station="GS-0",
                frame_label="ASM+Golay",
                # Framer log_fields now arrive pre-nested.
                log_fields={"facts": {"protocol": {"csp_header": {"prio": 2, "dest": 8}}}},
            )
            log.write_mission_command(record, raw_cmd=raw_cmd, wire=wire, log_text=[])
        finally:
            log.close()

        with open(log.jsonl_path) as f:
            rec = json.loads(f.readline())

    _assert_envelope(rec)
    assert rec["event_kind"] == "tx_command"
    assert rec["mission_id"] == "maveric"
    assert "label" not in rec
    assert "cmd_id" not in rec
    assert "dest" not in rec
    assert "ptype" not in rec
    assert rec["frame_label"] == "ASM+Golay"
    assert "uplink_mode" not in rec
    assert "uplink_mode" not in rec["mission"]
    assert rec["inner_hex"] == "010203"
    assert rec["inner_len"] == 3
    assert rec["wire_hex"] == "0102030405"
    assert rec["wire_len"] == 5
    assert rec["warnings"] == []
    # Mission-owned: cmd_id canonical at mission.cmd_id; not duplicated.
    assert rec["mission"]["cmd_id"] == "com_ping"
    assert "cmd_id" not in rec["mission"]["facts"]["header"]
    assert rec["mission"]["facts"]["header"]["dest"] == "EPS"
    assert rec["mission"]["facts"]["protocol"]["inner_len"] == 3
    assert "wire_len" not in rec["mission"]["facts"]["protocol"]
    assert rec["mission"]["facts"]["protocol"]["csp_header"]["dest"] == 8
    assert "csp" not in rec["mission"]
    assert len(rec["mission"]["parameters"]) == 1
    p = rec["mission"]["parameters"][0]
    assert p["name"] == "module"
    assert "ts_ms" not in p


def test_session_id_matches_file_stem():
    with tempfile.TemporaryDirectory() as tmp:
        log = SessionLog(tmp, zmq_addr="tcp://127.0.0.1:52002", version="5.7.0",
                    mission_id="maveric", station="GS-0", operator="irfan")
        try:
            record = tx_command_record(
                1, cmd_id="x", mission_facts={}, parameters=[], raw_cmd=b"", wire=b"",
                session_id=log.session_id,
                ts_ms=1_700_000_000_000,
                version="5.7.0",
                mission_id="maveric", operator="irfan", station="GS-0",
            )
            log.write_mission_command(record, raw_cmd=b"", wire=b"", log_text=[])
            expected_stem = log.session_id
        finally:
            log.close()

        with open(log.jsonl_path) as f:
            rec = json.loads(f.readline())
    assert rec["session_id"] == expected_stem
    import os
    assert os.path.basename(log.jsonl_path).removesuffix(".jsonl") == expected_stem


def test_radio_event_envelope_shape():
    with tempfile.TemporaryDirectory() as tmp:
        log = SessionLog(tmp, zmq_addr="tcp://127.0.0.1:52002", version="5.7.0",
                    mission_id="maveric", station="GS-0", operator="irfan")
        try:
            log.write_radio_event(
                "start",
                state="running",
                pid=1234,
                command=["python", "-u", "gnuradio/MAV_DUO.py"],
                script="gnuradio/MAV_DUO.py",
                cwd="gnuradio",
                detail="python -u gnuradio/MAV_DUO.py",
            )
        finally:
            log.close()

        with open(log.jsonl_path) as f:
            rec = json.loads(f.readline())

    _assert_envelope(rec)
    assert rec["event_kind"] == "radio"
    assert rec["mission_id"] == "maveric"
    assert rec["radio"]["action"] == "start"
    assert rec["radio"]["state"] == "running"
    assert rec["radio"]["pid"] == 1234
    assert rec["radio"]["command"] == ["python", "-u", "gnuradio/MAV_DUO.py"]


def test_tracking_event_envelope_shape():
    with tempfile.TemporaryDirectory() as tmp:
        log = SessionLog(tmp, zmq_addr="tcp://127.0.0.1:52002", version="5.7.0",
                    mission_id="maveric", station="GS-0", operator="irfan")
        try:
            log.write_tracking_event(
                "connect",
                mode="connected",
                prev_mode="disconnected",
                station_id="GS-0",
                rx_zmq_addr="tcp://127.0.0.1:52003",
                tx_zmq_addr="tcp://127.0.0.1:52004",
            )
            log.write_tracking_event(
                "disconnect",
                mode="disconnected",
                prev_mode="connected",
                station_id="GS-0",
            )
        finally:
            log.close()

        with open(log.jsonl_path) as f:
            lines = [json.loads(l) for l in f if l.strip()]

    assert len(lines) == 2
    for rec in lines:
        _assert_envelope(rec)
        assert rec["event_kind"] == "tracking"
        assert rec["mission_id"] == "maveric"
        assert rec["station"] == "GS-0"
        assert rec["seq"] == 0
        assert rec["tracking"]["station_id"] == "GS-0"

    assert lines[0]["tracking"]["action"] == "connect"
    assert lines[0]["tracking"]["mode"] == "connected"
    assert lines[0]["tracking"]["prev_mode"] == "disconnected"
    assert lines[0]["tracking"]["rx_zmq_addr"] == "tcp://127.0.0.1:52003"
    assert lines[0]["tracking"]["tx_zmq_addr"] == "tcp://127.0.0.1:52004"
    assert lines[1]["tracking"]["action"] == "disconnect"
    assert lines[1]["tracking"]["mode"] == "disconnected"
    assert lines[1]["tracking"]["prev_mode"] == "connected"


def test_tracking_sample_envelope_shape():
    doppler = {
        "ts_ms": 1714053603500,
        "mode": "connected",
        "station_id": "GS-0",
        "satellite": "MAVERIC",
        "elevation_deg": 42.5,
        "azimuth_deg": 187.3,
        "range_km": 612.4,
        "range_rate_mps": -6120.0,
        "rx_hz": 437_575_000.0,
        "rx_shift_hz": 8900.0,
        "rx_tune_hz": 437_583_900.0,
        "tx_hz": 437_575_000.0,
        "tx_shift_hz": -8900.0,
        "tx_tune_hz": 437_566_100.0,
    }
    with tempfile.TemporaryDirectory() as tmp:
        log = SessionLog(tmp, zmq_addr="tcp://127.0.0.1:52002", version="5.7.0",
                    mission_id="maveric", station="GS-0", operator="irfan")
        try:
            log.write_tracking_sample(doppler, source="tick")
            log.write_tracking_sample({**doppler, "ts_ms": 1714053604500}, source="tx_attempt")
        finally:
            log.close()

        with open(log.jsonl_path) as f:
            lines = [json.loads(l) for l in f if l.strip()]

    assert len(lines) == 2
    for rec in lines:
        _assert_envelope(rec)
        assert rec["event_kind"] == "tracking_sample"
        assert rec["mission_id"] == "maveric"
        assert rec["station"] == "GS-0"
        assert rec["seq"] == 0
        assert rec["tracking_sample"]["mode"] == "connected"
        assert rec["tracking_sample"]["elevation_deg"] == 42.5
        assert rec["tracking_sample"]["azimuth_deg"] == 187.3
        assert rec["tracking_sample"]["rx_tune_hz"] == 437_583_900.0
        assert rec["tracking_sample"]["tx_tune_hz"] == 437_566_100.0

    assert lines[0]["tracking_sample"]["source"] == "tick"
    assert lines[1]["tracking_sample"]["source"] == "tx_attempt"
