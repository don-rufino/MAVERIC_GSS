from pathlib import Path

from mav_gss_lib.platform.loader import load_mission_spec
from mav_gss_lib.platform.log_records import parameter_records, rx_packet_record
from mav_gss_lib.platform.rx.pipeline import RxPipeline


def _pipeline_for(spec, tmp_path: Path) -> RxPipeline:
    return RxPipeline(spec, walker=None)


def test_build_rx_packet_record_wraps_echo_packet_in_platform_envelope(tmp_path):
    spec = load_mission_spec(
        {"mission": {"id": "echo_v2", "config": {}}, "platform": {}},
        data_dir=tmp_path,
    )
    result = _pipeline_for(spec, tmp_path).process(
        {"transmitter": "fixture"},
        b"\xde\xad",
    )

    record = rx_packet_record(
        spec, result.packet, "1.2.3",
        session_id="session_test",
        operator="op", station="gs",
    )

    # Unified envelope fields
    assert record["event_kind"] == "rx_packet"
    assert record["session_id"] == "session_test"
    assert isinstance(record["event_id"], str) and len(record["event_id"]) == 32
    assert isinstance(record["ts_ms"], int)
    assert record["v"] == "1.2.3"
    assert record["mission_id"] == "echo_v2"
    assert record["operator"] == "op"
    assert record["station"] == "gs"

    # RX logs keep one canonical packet body.
    assert record["inner_hex"] == "dead"
    assert record["inner_len"] == 2
    assert "raw_hex" not in record
    assert "size" not in record
    assert "wire_hex" not in record
    assert "wire_len" not in record

    # Mission block always present; `_rendering` and nested `telemetry` gone
    assert record["mission"] == {}
    assert "_rendering" not in record
    assert "telemetry" not in record


def test_balloon_v2_emits_no_parameters_post_swap(tmp_path):
    """balloon_v2 has no declarative spec post-Task-4 — packets still flow,
    but no parameter records are emitted."""
    spec = load_mission_spec(
        {"mission": {"id": "balloon_v2", "config": {}}, "platform": {}},
        data_dir=tmp_path,
    )
    result = _pipeline_for(spec, tmp_path).process(
        {},
        b'{"type":"beacon","alt_m":1200,"lat":34.0,"lon":-118.2,"temp_c":18.4}',
    )

    record = rx_packet_record(
        spec, result.packet, "1.2.3",
        session_id="session_test",
        mission_id="balloon_v2",
    )
    rows = list(parameter_records(
        result.packet,
        session_id="session_test",
        rx_event_id=record["event_id"],
        version="1.2.3",
        mission_id="balloon_v2",
    ))

    assert record["mission"] == {}
    assert rows == []
