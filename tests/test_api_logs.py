"""Contract tests for /api/logs endpoints against the unified schema.

Uses FastAPI's TestClient over a live WebRuntime fixture so the filters,
pagination, and event_kind routing exercise real code paths.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from mav_gss_lib.server.app import create_app


def _build_fixture(log_dir: Path) -> str:
    (log_dir / "json").mkdir(parents=True, exist_ok=True)
    stem = "session_20260423_140000_GS-0_irfan"
    path = log_dir / "json" / f"{stem}.jsonl"
    rx = {
        "event_id": "e1", "event_kind": "rx_packet",
        "session_id": stem, "ts_ms": 1714053603500,
        "ts_iso": "2026-04-23T14:00:03.500+00:00",
        "seq": 1, "v": "5.7.0", "mission_id": "maveric",
        "operator": "irfan", "station": "GS-0",
        "frame_label": "ASM+GOLAY", "transport_meta": "",
        "inner_hex": "deadbeef", "inner_len": 4,
        "duplicate": False, "uplink_echo": False, "unknown": False,
        "warnings": [],
        "mission": {"id": "maveric", "facts": {"header": {"cmd_id": "eps_hk"}}},
    }
    tel = {
        "event_id": "t1", "event_kind": "parameter",
        "session_id": stem, "ts_ms": 1714053603500,
        "ts_iso": "2026-04-23T14:00:03.500+00:00",
        "seq": 1, "v": "5.7.0", "mission_id": "maveric",
        "operator": "irfan", "station": "GS-0",
        "rx_event_id": "e1",
        "name": "eps.vbatt", "value": 7.42,
        "unit": "V", "display_only": False,
    }
    tx = {
        "event_id": "e2", "event_kind": "tx_command",
        "session_id": stem, "ts_ms": 1714053607500,
        "ts_iso": "2026-04-23T14:00:07.500+00:00",
        "seq": 2, "v": "5.7.0", "mission_id": "maveric",
        "operator": "irfan", "station": "GS-0",
        "frame_label": "ASM+Golay",
        "inner_hex": "dead", "inner_len": 2,
        "wire_hex": "deadbeef", "wire_len": 4,
        "warnings": [],
        "mission": {
            "id": "maveric",
            "cmd_id": "com_ping",
            "facts": {
                "header": {"dest": "EPS", "src": "GS", "echo": "NONE", "ptype": "CMD"},
                "protocol": {
                    "args_hex": "",
                    "args_len": 0,
                    "inner_len": 2,
                    "csp_header": {"prio": 2, "src": 0, "dest": 8,
                                   "dport": 24, "sport": 0, "flags": 0,
                                   "csp_crc": True},
                },
            },
            "parameters": [],
        },
    }
    trk = {
        "event_id": "e3", "event_kind": "tracking",
        "session_id": stem, "ts_ms": 1714053609500,
        "ts_iso": "2026-04-23T14:00:09.500+00:00",
        "seq": 0, "v": "5.7.0", "mission_id": "maveric",
        "operator": "irfan", "station": "GS-0",
        "tracking": {
            "action": "connect",
            "mode": "connected",
            "prev_mode": "disconnected",
            "station_id": "GS-0",
            "rx_zmq_addr": "tcp://127.0.0.1:52003",
            "tx_zmq_addr": "tcp://127.0.0.1:52004",
            "detail": "",
        },
    }
    path.write_text("\n".join(json.dumps(x) for x in [rx, tel, tx, trk]) + "\n")
    return stem


def test_list_sessions_enumerates_json_dir():
    with tempfile.TemporaryDirectory() as tmp:
        stem = _build_fixture(Path(tmp))
        app = create_app()
        # `log_dir` is a read-only property over platform_cfg.general.log_dir
        app.state.runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
        with TestClient(app) as client:
            r = client.get("/api/logs")
        assert r.status_code == 200
        items = r.json()
        assert any(s["session_id"] == stem for s in items)


def test_entries_endpoint_defaults_exclude_parameters():
    with tempfile.TemporaryDirectory() as tmp:
        stem = _build_fixture(Path(tmp))
        app = create_app()
        # `log_dir` is a read-only property over platform_cfg.general.log_dir
        app.state.runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
        with TestClient(app) as client:
            r = client.get(f"/api/logs/{stem}")
        assert r.status_code == 200
        data = r.json()
        kinds = {e["event_kind"] for e in data["entries"]}
        # parameter records are filtered out by default; rx, tx, and system
        # audit events (radio, tracking) come through.
        assert kinds == {"rx_packet", "tx_command", "tracking"}
        assert len(data["entries"]) == 3


def test_entries_endpoint_opt_in_parameters():
    with tempfile.TemporaryDirectory() as tmp:
        stem = _build_fixture(Path(tmp))
        app = create_app()
        # `log_dir` is a read-only property over platform_cfg.general.log_dir
        app.state.runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
        with TestClient(app) as client:
            r = client.get(f"/api/logs/{stem}?event_kind=rx_packet,parameter")
        assert r.status_code == 200
        kinds = [e["event_kind"] for e in r.json()["entries"]]
        assert kinds.count("parameter") == 1
        assert kinds.count("rx_packet") == 1


def test_label_filter_matches_tx_mission_cmd_id():
    with tempfile.TemporaryDirectory() as tmp:
        stem = _build_fixture(Path(tmp))
        app = create_app()
        # `log_dir` is a read-only property over platform_cfg.general.log_dir
        app.state.runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
        with TestClient(app) as client:
            r = client.get(f"/api/logs/{stem}?label=ping")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["seq"] == 2


def test_label_filter_matches_rx_mission_facts_cmd_id():
    with tempfile.TemporaryDirectory() as tmp:
        stem = _build_fixture(Path(tmp))
        app = create_app()
        app.state.runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
        with TestClient(app) as client:
            r = client.get(f"/api/logs/{stem}?label=eps_hk")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["event_kind"] == "rx_packet"
        assert entries[0]["seq"] == 1


def test_parameters_endpoint_filters_by_name():
    with tempfile.TemporaryDirectory() as tmp:
        stem = _build_fixture(Path(tmp))
        app = create_app()
        # `log_dir` is a read-only property over platform_cfg.general.log_dir
        app.state.runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
        with TestClient(app) as client:
            r = client.get(f"/api/logs/{stem}/parameters?name=eps.vbatt")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["value"] == 7.42
        assert entries[0]["unit"] == "V"


def test_tracking_event_kind_is_filterable():
    with tempfile.TemporaryDirectory() as tmp:
        stem = _build_fixture(Path(tmp))
        app = create_app()
        app.state.runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
        with TestClient(app) as client:
            r = client.get(f"/api/logs/{stem}?event_kind=tracking")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["event_kind"] == "tracking"
        assert entry["tracking"]["action"] == "connect"
        assert entry["tracking"]["mode"] == "connected"
        assert entry["tracking"]["prev_mode"] == "disconnected"


def test_missing_session_returns_404():
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(Path(tmp))
        app = create_app()
        app.state.runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
        with TestClient(app) as client:
            r = client.get("/api/logs/does_not_exist")
        assert r.status_code == 404
