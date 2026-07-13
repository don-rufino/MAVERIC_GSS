"""PUT /api/config applies native split updates through the spec boundaries."""
import asyncio
import copy
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mav_gss_lib.platform import MissionConfigSpec  # noqa: E402
from mav_gss_lib.platform.config import (  # noqa: E402
    apply_platform_config_update as _apply_platform_update,
)
from mav_gss_lib.platform.tx.verifiers import (  # noqa: E402
    CheckWindow,
    CommandInstance,
    VerifierOutcome,
    VerifierRegistry,
    VerifierSet,
    VerifierSpec,
    write_instances,
)


class TestApplyPlatformUpdate(unittest.TestCase):
    def test_whitelists_general_keys(self):
        platform_cfg = {"general": {"log_dir": "logs", "version": "0.0.0"}}
        _apply_platform_update(platform_cfg, {"general": {
            "log_dir": "new_logs",
            "version": "SENTINEL",
            "generated_commands_dir": "queue_out",
        }})
        self.assertEqual(platform_cfg["general"]["log_dir"], "new_logs")
        self.assertEqual(platform_cfg["general"]["generated_commands_dir"], "queue_out")
        # Version must stay as-was — not a whitelisted platform key.
        self.assertEqual(platform_cfg["general"]["version"], "0.0.0")

    def test_merges_tx_rx_sections(self):
        platform_cfg = {"tx": {"zmq_addr": "tcp://old", "delay_ms": 500}, "rx": {"zmq_addr": "tcp://old-rx"}}
        _apply_platform_update(platform_cfg, {
            "tx": {
                "zmq_addr": "tcp://new",
                "frequency": "437.6 MHz",
                "verifiers_enabled": False,
            },
            "rx": {"frequency": "437.7 MHz", "tx_blackout_ms": 750},
        })
        self.assertEqual(platform_cfg["tx"]["zmq_addr"], "tcp://new")
        self.assertEqual(platform_cfg["tx"]["delay_ms"], 500)
        self.assertEqual(platform_cfg["tx"]["frequency"], "437.6 MHz")
        self.assertIs(platform_cfg["tx"]["verifiers_enabled"], False)
        self.assertEqual(platform_cfg["rx"]["zmq_addr"], "tcp://old-rx")
        self.assertEqual(platform_cfg["rx"]["frequency"], "437.7 MHz")
        self.assertEqual(platform_cfg["rx"]["tx_blackout_ms"], 750)

    def test_drops_retired_tx_uplink_mode(self):
        platform_cfg = {"tx": {"delay_ms": 500}}
        _apply_platform_update(platform_cfg, {
            "tx": {"uplink_mode": "ASM+Golay", "delay_ms": 600},
        })

        self.assertEqual(platform_cfg["tx"]["delay_ms"], 600)
        self.assertNotIn("uplink_mode", platform_cfg["tx"])

    def test_drops_non_editable_sections(self):
        """Sections outside editable_sections never land on platform_cfg."""
        platform_cfg = {"tx": {}, "rx": {}}
        _apply_platform_update(platform_cfg, {
            "tx": {"delay_ms": 10},
            "stations": {"h1": "Pad"},
            "nodes": {"1": "LEAK"},
            "csp": {"source": 99},
        })
        self.assertEqual(platform_cfg["tx"]["delay_ms"], 10)
        self.assertNotIn("stations", platform_cfg)
        self.assertNotIn("nodes", platform_cfg)
        self.assertNotIn("csp", platform_cfg)


class _FakeService:
    def __init__(self):
        self.sending = {"active": False}
        self.status = ("ok",)
        self.log = None
        self.send_lock = threading.Lock()
        self.messages = []
    async def broadcast(self, message):
        self.messages.append(message)
    async def broadcast_verification_reset(self):
        await self.broadcast({"type": "verification_restore", "instances": []})
    def restart_pub(self, *_a, **_k): pass
    def restart_receiver(self): pass


class _FakeMission:
    def __init__(self, spec: MissionConfigSpec):
        self.config = spec


class _FakeRuntime:
    """Minimal split-state runtime compatible with /api/config PUT."""

    def __init__(self, platform_cfg, mission_id, mission_cfg, spec):
        self.platform_cfg = platform_cfg
        self.mission_id = mission_id
        self.mission_cfg = mission_cfg
        self.cfg_lock = threading.RLock()
        self.session_token = "test-token"
        self.tx = _FakeService()
        self.rx = _FakeService()
        self.platform = SimpleNamespace(verifiers=VerifierRegistry())
        self.mission = _FakeMission(spec)

    @property
    def log_dir(self):
        return self.platform_cfg.get("general", {}).get("log_dir", "logs")


class TestConfigEndpointRoundTrip(unittest.TestCase):
    """End-to-end: the real PUT handler accepts native-shape updates."""

    def _build_app(self, runtime):
        from fastapi import FastAPI
        from mav_gss_lib.server.api.config import router
        app = FastAPI()
        app.state.runtime = runtime
        app.include_router(router)
        return app

    def _mavericish_spec(self):
        return MissionConfigSpec(
            editable_paths={"csp.*", "imaging.thumb_prefix"},
            protected_paths={
                "nodes",
                "ptypes",
                "node_descriptions",
                "gs_node",
                "mission_name",
                "command_defs",
                "command_defs_resolved",
                "command_defs_warning",
                "rx_title",
                "tx_title",
                "splash_subtitle",
            },
        )

    def test_put_applies_native_platform_and_mission_updates(self):
        import yaml
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        import mav_gss_lib.config as _cfg_mod

        with tempfile.TemporaryDirectory() as tmp:
            gss_path = os.path.join(tmp, "gss.yml")

            platform_cfg = {
                "general": {
                    "log_dir": "logs",
                    "generated_commands_dir": "generated_commands",
                    "version": "5.0.0",
                },
                "tx": {"zmq_addr": "tcp://127.0.0.1:52002", "delay_ms": 500, "frequency": "437.6 MHz"},
                "rx": {"zmq_addr": "tcp://127.0.0.1:52001", "frequency": "437.7 MHz"},
                "stations": {},
            }
            mission_cfg = {
                "mission_name": "MAVERIC",
                "nodes": {"1": "LPPM"},
                "ptypes": {"1": "CMD"},
                "gs_node": "GS",
                "csp": {"priority": 2, "destination": 8, "source": 6},
            }
            runtime = _FakeRuntime(
                platform_cfg=platform_cfg,
                mission_id="maveric",
                mission_cfg=copy.deepcopy(mission_cfg),
                spec=self._mavericish_spec(),
            )

            app = self._build_app(runtime)
            client = TestClient(app)

            update = {
                "platform": {
                    "tx": {"delay_ms": 600, "verifiers_enabled": False},
                    # These must be dropped by apply_platform_config_update:
                    "stations": {"h1": "LEAK"},
                    "general": {
                        "log_dir": "logs",
                        "version": "0.0.1-sentinel",
                    },
                },
                "mission": {
                    "config": {
                        "csp": {"priority": 3, "source": 7},
                        # Mission-protected top-level keys — spec MUST reject:
                        "nodes": {"99": "SENTINEL_NODE"},
                        "ptypes": {"99": "SENTINEL_PTYPE"},
                        "mission_name": "SENTINEL_MISSION_NAME",
                        "gs_node": "SENTINEL_GS",
                        "rx_title": "SENTINEL_RX_TITLE",
                    },
                },
            }

            with patch.object(_cfg_mod, "_DEFAULT_GSS_PATH", gss_path):
                resp = client.put(
                    "/api/config",
                    json=update,
                    headers={"x-gss-token": "test-token"},
                )

            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(resp.json(), {"ok": True})

            with open(gss_path) as f:
                persisted = yaml.safe_load(f)

            # Persisted in native {platform, mission} shape.
            self.assertEqual(persisted["mission"]["id"], "maveric")
            self.assertEqual(persisted["platform"]["rx"]["frequency"], "437.7 MHz")
            self.assertEqual(persisted["platform"]["tx"]["frequency"], "437.6 MHz")
            self.assertEqual(persisted["platform"]["tx"]["delay_ms"], 600)
            self.assertIs(persisted["platform"]["tx"]["verifiers_enabled"], False)
            self.assertEqual(persisted["mission"]["config"]["csp"]["priority"], 3)
            self.assertEqual(persisted["mission"]["config"]["csp"]["destination"], 8)
            self.assertEqual(persisted["mission"]["config"]["csp"]["source"], 7)

            # Protected mission-identity keys (nodes, ptypes, mission_name,
            # gs_node, rx_title) are seeded from mission code at build time
            # and must NOT appear on disk — otherwise a stale snapshot could
            # silently override code defaults.
            persisted_mission = persisted["mission"]["config"]
            self.assertNotIn("nodes", persisted_mission)
            self.assertNotIn("ptypes", persisted_mission)
            self.assertNotIn("mission_name", persisted_mission)
            self.assertNotIn("gs_node", persisted_mission)
            self.assertNotIn("rx_title", persisted_mission)

            # Platform spec rejected stations + version smuggled in updates.
            self.assertNotIn("stations", persisted["platform"])
            self.assertNotIn("version", persisted["platform"].get("general", {}))

            # In-memory primary split state still reflects protections —
            # the protected keys stay live in memory (seeded at build time)
            # even though they don't persist.
            self.assertEqual(runtime.mission_cfg["nodes"], {"1": "LPPM"})
            self.assertEqual(runtime.mission_cfg["csp"]["source"], 7)
            self.assertEqual(runtime.mission_cfg["mission_name"], "MAVERIC")
            self.assertNotIn("99", runtime.mission_cfg["nodes"])
            self.assertIs(runtime.platform_cfg["tx"]["verifiers_enabled"], False)
            # Platform version still sourced from platform defaults (not clobbered).
            self.assertEqual(runtime.platform_cfg["general"]["version"], "5.0.0")

    def test_put_rejects_missing_token(self):
        from fastapi.testclient import TestClient
        runtime = _FakeRuntime(
            platform_cfg={"general": {"log_dir": "logs"}, "tx": {}, "rx": {}},
            mission_id="maveric",
            mission_cfg={},
            spec=self._mavericish_spec(),
        )
        app = self._build_app(runtime)
        client = TestClient(app)
        resp = client.put("/api/config", json={"platform": {"tx": {"delay_ms": 600}}})
        self.assertEqual(resp.status_code, 403)

    def test_put_disabling_verifiers_clears_runtime_disk_and_clients(self):
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        import mav_gss_lib.config as _cfg_mod

        with tempfile.TemporaryDirectory() as tmp:
            runtime = _FakeRuntime(
                platform_cfg={
                    "general": {"log_dir": tmp},
                    "tx": {
                        "zmq_addr": "tcp://127.0.0.1:52002",
                        "verifiers_enabled": True,
                    },
                    "rx": {"zmq_addr": "tcp://127.0.0.1:52001"},
                },
                mission_id="maveric",
                mission_cfg={},
                spec=self._mavericish_spec(),
            )
            verifier_set = VerifierSet(verifiers=(
                VerifierSpec(
                    "uppm_ack", "received", CheckWindow(0, 30_000),
                    "UPPM", "info",
                ),
            ))
            instance = CommandInstance(
                instance_id="open-instance",
                correlation_key=("com_ping", "LPPM"),
                t0_ms=1_000_000,
                cmd_event_id="cmd-event",
                verifier_set=verifier_set,
                outcomes={"uppm_ack": VerifierOutcome.pending()},
                stage="released",
            )
            runtime.platform.verifiers.register(instance)
            pending_path = os.path.join(tmp, ".pending_instances.jsonl")
            write_instances(pending_path, [instance])

            app = self._build_app(runtime)
            client = TestClient(app)
            with patch.object(
                _cfg_mod, "_DEFAULT_GSS_PATH", os.path.join(tmp, "gss.yml"),
            ):
                resp = client.put(
                    "/api/config",
                    json={"platform": {"tx": {"verifiers_enabled": False}}},
                    headers={"x-gss-token": "test-token"},
                )

            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(runtime.platform.verifiers.open_instances(), [])
            self.assertEqual(Path(pending_path).read_text(), "")
            self.assertEqual(runtime.tx.messages, [{
                "type": "verification_restore",
                "instances": [],
            }])

    def test_put_disabling_during_active_send_releases_real_waiter(self):
        from unittest.mock import patch
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient
        from mav_gss_lib.server.api.config import router
        from mav_gss_lib.server.state import create_runtime

        async def _run(tmp: str):
            runtime = create_runtime()
            runtime.config_save_disabled = True
            runtime.platform_cfg.setdefault("general", {})["log_dir"] = tmp
            runtime.platform_cfg.setdefault("tx", {})["verifiers_enabled"] = True
            runtime.tx.sending["active"] = True

            verifier_set = VerifierSet(verifiers=(
                VerifierSpec(
                    "uppm_ack", "received", CheckWindow(0, 30_000),
                    "UPPM", "info",
                ),
            ))
            runtime.platform.verifiers.register(CommandInstance(
                instance_id="active-wait",
                correlation_key=("com_ping", "LPPM"),
                t0_ms=1_000_000,
                cmd_event_id="active-event",
                verifier_set=verifier_set,
                outcomes={"uppm_ack": VerifierOutcome.pending()},
                stage="released",
            ))

            app = FastAPI()
            app.state.runtime = runtime
            app.include_router(router)
            entered_wait = asyncio.Event()
            original_abort_wait = runtime.tx.abort.wait

            async def _signal_wait():
                entered_wait.set()
                await original_abort_wait()

            with patch.object(runtime.tx.abort, "wait", side_effect=_signal_wait):
                wait_task = asyncio.create_task(
                    runtime.tx._send_coord._wait_for_pending_verifications_clear(
                        poll_ms=10,
                        max_wait_ms=5_000,
                    )
                )
                await asyncio.wait_for(entered_wait.wait(), timeout=0.5)
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                ) as client:
                    response = await client.put(
                        "/api/config",
                        json={"platform": {"tx": {"verifiers_enabled": False}}},
                        headers={"x-gss-token": runtime.session_token},
                    )
                wait_result = await asyncio.wait_for(wait_task, timeout=0.5)

            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(wait_result)
            self.assertTrue(runtime.tx.sending["active"])
            self.assertFalse(runtime.tx_verifiers_enabled)
            self.assertEqual(runtime.platform.verifiers.open_instances(), [])
            self.assertEqual(
                (Path(tmp) / ".pending_instances.jsonl").read_text(), "",
            )

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(_run(tmp))


if __name__ == "__main__":
    unittest.main()
