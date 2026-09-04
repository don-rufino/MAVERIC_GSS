"""Contract tests for /api/logs/{session_id}/tracking_samples.

Mirrors the fixture style of test_api_logs.py: a live WebRuntime fixture
over a temp log dir so filtering and the default-exclusion contract with
the main /api/logs/{session_id} listing exercise real code paths.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from mav_gss_lib.server.app import create_app


def _build_fixture(log_dir: Path) -> str:
    (log_dir / "json").mkdir(parents=True, exist_ok=True)
    stem = "session_20260904_071200_suchai4_usc_rperea"
    path = log_dir / "json" / f"{stem}.jsonl"

    def sample(event_id: str, ts_ms: int, source: str) -> dict:
        return {
            "event_id": event_id, "event_kind": "tracking_sample",
            "session_id": stem, "ts_ms": ts_ms,
            "ts_iso": "2026-09-04T07:12:03.822+00:00",
            "seq": 0, "v": "6.1.0", "mission_id": "suchai4",
            "operator": "rperea", "station": "usc",
            "tracking_sample": {
                "source": source, "mode": "connected", "station_id": "usc",
                "satellite": "TRANSPORTER-17 OBJECT AU",
                "elevation_deg": 15.2, "azimuth_deg": 5.6,
                "range_km": 1624.0, "range_rate_mps": -6689.2,
                "rx_hz": 437250000.0, "rx_shift_hz": 9756.2,
                "rx_tune_hz": 437259756.2,
                "tx_hz": 437250000.0, "tx_shift_hz": -9756.2,
                "tx_tune_hz": 437240243.8,
            },
        }

    rx = {
        "event_id": "e1", "event_kind": "rx_packet",
        "session_id": stem, "ts_ms": 1788505925561,
        "ts_iso": "2026-09-04T07:12:05.561+00:00",
        "seq": 1, "v": "6.1.0", "mission_id": "suchai4",
        "operator": "rperea", "station": "usc",
        "frame_label": "ASM+GOLAY", "transport_meta": "",
        "inner_hex": "83e5", "inner_len": 2,
        "duplicate": False, "uplink_echo": False, "unknown": False,
        "warnings": ["implausible CSP src/dest"],
        "mission": {"id": "suchai4", "cmd_id": "", "facts": {"header": {}}},
    }
    tick = sample("e2", 1788505923822, "tick")
    rx_decode = sample("e3", 1788505925561, "rx_decode")
    path.write_text("\n".join(json.dumps(x) for x in [tick, rx, rx_decode]) + "\n")
    return stem


def test_main_listing_excludes_tracking_sample_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        stem = _build_fixture(Path(tmp))
        app = create_app()
        app.state.runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
        with TestClient(app) as client:
            r = client.get(f"/api/logs/{stem}")
        assert r.status_code == 200
        kinds = {e["event_kind"] for e in r.json()["entries"]}
        assert kinds == {"rx_packet"}


def test_tracking_samples_endpoint_returns_all_without_filter():
    with tempfile.TemporaryDirectory() as tmp:
        stem = _build_fixture(Path(tmp))
        app = create_app()
        app.state.runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
        with TestClient(app) as client:
            r = client.get(f"/api/logs/{stem}/tracking_samples")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 2
        sources = {e["tracking_sample"]["source"] for e in entries}
        assert sources == {"tick", "rx_decode"}


def test_tracking_samples_endpoint_filters_by_source():
    with tempfile.TemporaryDirectory() as tmp:
        stem = _build_fixture(Path(tmp))
        app = create_app()
        app.state.runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
        with TestClient(app) as client:
            r = client.get(f"/api/logs/{stem}/tracking_samples?source=rx_decode,tx_attempt")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["event_id"] == "e3"
        assert entries[0]["tracking_sample"]["source"] == "rx_decode"


def test_tracking_samples_endpoint_missing_session_returns_404():
    with tempfile.TemporaryDirectory() as tmp:
        _build_fixture(Path(tmp))
        app = create_app()
        app.state.runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
        with TestClient(app) as client:
            r = client.get("/api/logs/does_not_exist/tracking_samples")
        assert r.status_code == 404
