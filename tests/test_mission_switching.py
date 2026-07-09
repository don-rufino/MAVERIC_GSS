"""Mission switching: per-mission config files, discovery, switch endpoint."""

import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mav_gss_lib import config
from mav_gss_lib.platform.loader import discover_missions
from mav_gss_lib.server.api import mission as mission_api


# ── per-mission operator config files ───────────────────────────────────

def test_active_gss_path_defaults_to_legacy(monkeypatch):
    monkeypatch.delenv("GSS_MISSION", raising=False)
    assert config._active_gss_path() == config._DEFAULT_GSS_PATH


def test_active_gss_path_maveric_keeps_legacy(monkeypatch):
    monkeypatch.setenv("GSS_MISSION", "maveric")
    assert config._active_gss_path() == config._DEFAULT_GSS_PATH


def test_active_gss_path_other_mission_gets_own_file(monkeypatch):
    monkeypatch.setenv("GSS_MISSION", "astrocast")
    path = config._active_gss_path()
    assert path.name == "gss.astrocast.yml"
    assert path.parent == config._DEFAULT_GSS_PATH.parent


def test_load_split_config_forces_env_mission(monkeypatch, tmp_path):
    cfg_file = tmp_path / "gss.yml"
    cfg_file.write_text("platform: {}\nmission:\n  id: maveric\n")
    monkeypatch.setenv("GSS_MISSION", "astrocast")
    _, mission_id, _ = config.load_split_config(path=str(cfg_file))
    assert mission_id == "astrocast"


def test_load_split_config_unforced_uses_file(monkeypatch, tmp_path):
    cfg_file = tmp_path / "gss.yml"
    cfg_file.write_text("platform: {}\nmission:\n  id: maveric\n")
    monkeypatch.delenv("GSS_MISSION", raising=False)
    _, mission_id, _ = config.load_split_config(path=str(cfg_file))
    assert mission_id == "maveric"


# ── mission discovery ────────────────────────────────────────────────────

def test_discover_missions_lists_deployable_only():
    missions = discover_missions()
    ids = {m["id"] for m in missions}
    assert "astrocast" in ids
    assert "echo_v2" not in ids
    assert "balloon_v2" not in ids
    astrocast = next(m for m in missions if m["id"] == "astrocast")
    assert astrocast["name"] == "Astrocast 0.1"


# ── argv rewriting ───────────────────────────────────────────────────────

def test_argv_with_mission_appends_when_absent():
    assert mission_api._argv_with_mission(["MAV_WEB.py"], "astrocast") == [
        "MAV_WEB.py", "--mission", "astrocast",
    ]


def test_argv_with_mission_replaces_pair_form():
    argv = ["MAV_WEB.py", "--ephemeral", "--mission", "maveric"]
    assert mission_api._argv_with_mission(argv, "astrocast") == [
        "MAV_WEB.py", "--ephemeral", "--mission", "astrocast",
    ]


def test_argv_with_mission_replaces_equals_form():
    argv = ["MAV_WEB.py", "--mission=maveric"]
    assert mission_api._argv_with_mission(argv, "astrocast") == [
        "MAV_WEB.py", "--mission", "astrocast",
    ]


# ── switch endpoint ──────────────────────────────────────────────────────

class _FakeRuntime:
    def __init__(self):
        self.mission_id = "maveric"
        self.mission_name = "MAVERIC"
        self.session_token = "tok-123"


def _client():
    app = FastAPI()
    app.include_router(mission_api.router)
    app.state.runtime = _FakeRuntime()
    return TestClient(app)


def test_api_missions_lists_active_and_discovered():
    client = _client()
    data = client.get("/api/missions").json()
    assert data["active"] == "maveric"
    ids = {m["id"] for m in data["missions"]}
    assert "astrocast" in ids
    assert "maveric" in ids  # injected even if maveric's mission.yml is absent


def test_switch_requires_token():
    client = _client()
    response = client.post("/api/mission/switch", json={"id": "astrocast"})
    assert response.status_code == 403


def test_switch_rejects_unknown_mission():
    client = _client()
    response = client.post(
        "/api/mission/switch", json={"id": "voyager"},
        headers={"x-gss-token": "tok-123"},
    )
    assert response.status_code == 400
    assert "unknown mission" in response.json()["error"]


def test_switch_rejects_current_mission(monkeypatch):
    client = _client()
    client.app.state.runtime.mission_id = "astrocast"
    response = client.post(
        "/api/mission/switch", json={"id": "astrocast"},
        headers={"x-gss-token": "tok-123"},
    )
    assert response.status_code == 400
    assert "already the active mission" in response.json()["error"]


def test_switch_happy_path_schedules_restart(monkeypatch):
    calls = []
    done = threading.Event()

    def fake_restart(runtime, mission_id):
        calls.append((runtime, mission_id))
        done.set()

    monkeypatch.setattr(mission_api, "_restart_fn", fake_restart)
    client = _client()
    response = client.post(
        "/api/mission/switch", json={"id": "astrocast"},
        headers={"x-gss-token": "tok-123"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "switching_to": "astrocast"}
    assert done.wait(timeout=5), "restart thread never ran"
    assert calls[0][1] == "astrocast"
    assert calls[0][0] is client.app.state.runtime
