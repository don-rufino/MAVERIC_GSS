"""Tests for mav_gss_lib.log_tools.backfill_decode.

Fixture records mirror the real shape logs/json/session_*_suchai4_*.jsonl
had before this mission's housekeeping decoder + dest-plausibility fix
existed (captured 2026-09-11): rx_packet with only header/protocol facts,
the now-fixed false-positive warning, and no parameter records at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mav_gss_lib.log_tools.backfill_decode import BackfillGuardError, backfill_session


# Real golden frame, missions/suchai4 test suite, captured 2026-08-28.
GOLDEN_FRAME_HEX = (
    "83e538010000690100000001000000006a6a33e86a6a33e800000004000004c3"
    "000000010000004e000d028000023f8000000000000000006a25acd500000000"
    "00000000000000001a0fe7d000000003000012c0000000780000205a00000000"
    "000000684196cccd000000730000000200000000000000000000000000000000"
    "00000003ffffffff00001a2c0003f48000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000631b8a56f76dddbe"
)


def _pre_fix_rx_record(event_id: str, seq: int, ts_ms: int) -> dict:
    """Shape a pre-decoder, pre-plausibility-fix suchai4 rx_packet record —
    i.e. exactly what real archived sessions look like."""
    return {
        "event_id": event_id, "event_kind": "rx_packet",
        "session_id": "session_test_suchai4", "ts_ms": ts_ms,
        "ts_iso": "2026-08-28T07:41:34+00:00", "seq": seq, "v": "6.1.0",
        "mission_id": "suchai4", "operator": "rperea", "station": "usc",
        "frame_label": "ASM+GOLAY",
        "transport_meta": "4k8 FSK AX100 ASM+Golay downlink",
        "inner_hex": GOLDEN_FRAME_HEX, "inner_len": len(GOLDEN_FRAME_HEX) // 2,
        "duplicate": False, "uplink_echo": False, "unknown": False,
        "warnings": ["implausible CSP src/dest"],
        "mission": {
            "id": "suchai4", "cmd_id": "",
            "facts": {
                "header": {"type": "TLM", "len": 106, "src": 1, "dst": 30, "dport": 20, "sport": 56},
                "protocol": {"csp_header": {"prio": 2, "src": 1, "dest": 30, "dport": 20, "sport": 56, "flags": 1}},
            },
        },
    }


def _write_session(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "session_test_suchai4.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_backfills_facts_and_clears_false_warning(tmp_path):
    records = [
        {"event_id": "tick1", "event_kind": "tracking_sample", "ts_ms": 1},
        _pre_fix_rx_record("rx1", 1, 1756366894000),
        {"event_id": "tick2", "event_kind": "tracking_sample", "ts_ms": 2},
    ]
    path = _write_session(tmp_path, records)

    result = backfill_session(path)

    assert result.changed == 1
    assert result.wrote is True
    assert not result.soft_mismatches

    out = [json.loads(l) for l in path.read_text().splitlines()]
    assert [r["event_kind"] for r in out[:1]] == ["tracking_sample"]
    rx = out[1]
    assert rx["event_id"] == "rx1"  # preserved
    assert rx["warnings"] == []  # false positive cleared
    assert rx["mission"]["facts"]["beacon"]["kind"] == "hk"
    assert rx["mission"]["facts"]["beacon"]["vbat_mv"] == 8282
    params = [r for r in out if r.get("event_kind") == "parameter"]
    assert len(params) == 39
    assert all(p["rx_event_id"] == "rx1" for p in params)
    # parent-before-children ordering, same as live logging
    assert out.index(rx) < out.index(params[0])
    assert out[-1]["event_kind"] == "tracking_sample"


def test_backup_written_once_and_preserved_on_rerun(tmp_path):
    path = _write_session(tmp_path, [_pre_fix_rx_record("rx1", 1, 1756366894000)])
    original_text = path.read_text()

    backfill_session(path)
    backup = path.with_suffix(".jsonl.bak")
    assert backup.exists()
    assert backup.read_text() == original_text

    # Corrupt the backup on purpose; a second run must not touch it again.
    backup.write_text("sentinel", encoding="utf-8")
    result = backfill_session(path)
    assert result.changed == 0  # already backfilled — no-op
    assert backup.read_text() == "sentinel"


def test_rerun_on_already_backfilled_session_is_a_noop(tmp_path):
    path = _write_session(tmp_path, [_pre_fix_rx_record("rx1", 1, 1756366894000)])
    backfill_session(path)
    once = path.read_text()

    result = backfill_session(path)

    assert result.changed == 0
    assert result.wrote is False
    assert path.read_text() == once


def test_dry_run_writes_nothing(tmp_path):
    path = _write_session(tmp_path, [_pre_fix_rx_record("rx1", 1, 1756366894000)])
    original = path.read_text()

    result = backfill_session(path, dry_run=True)

    assert result.changed == 1
    assert result.wrote is False
    assert path.read_text() == original
    assert not path.with_suffix(".jsonl.bak").exists()


def test_no_rx_packet_records_is_skipped(tmp_path):
    path = _write_session(tmp_path, [{"event_id": "e1", "event_kind": "tracking_sample", "ts_ms": 1}])
    result = backfill_session(path)
    assert result.skipped_reason == "no rx_packet records"


def test_mixed_mission_id_is_skipped(tmp_path):
    a = _pre_fix_rx_record("rx1", 1, 1756366894000)
    b = _pre_fix_rx_record("rx2", 2, 1756366895000)
    b["mission_id"] = "roads"
    path = _write_session(tmp_path, [a, b])
    result = backfill_session(path)
    assert result.skipped_reason is not None
    assert "mission_id" in result.skipped_reason


def test_seq_mismatch_aborts_without_writing(tmp_path):
    rec = _pre_fix_rx_record("rx1", 1, 1756366894000)
    rec["seq"] = 99  # replay will recompute seq=1 for the first frame in the file
    path = _write_session(tmp_path, [rec])
    original = path.read_text()

    with pytest.raises(BackfillGuardError):
        backfill_session(path)

    assert path.read_text() == original  # nothing written on abort
    assert not path.with_suffix(".jsonl.bak").exists()
