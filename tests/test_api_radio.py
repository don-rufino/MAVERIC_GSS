"""Tests for /api/radio/* — status codes, auth gating, error contracts."""

from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from mav_gss_lib.server.app import create_app


class RadioApiSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.client = TestClient(self.app)
        self.runtime = self.app.state.runtime
        self.token = self.runtime.session_token

    def test_status_returns_200_with_envelope(self) -> None:
        r = self.client.get("/api/radio/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("enabled", "state", "running", "pid", "log_lines"):
            self.assertIn(key, body)


class RadioActionErrorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.client = TestClient(self.app)
        self.runtime = self.app.state.runtime
        self.token = self.runtime.session_token
        # Force radio integration disabled for this test
        self.runtime.platform_cfg["radio"]["enabled"] = False

    def test_start_unauth_returns_403(self) -> None:
        r = self.client.post("/api/radio/start")
        self.assertEqual(r.status_code, 403)

    def test_start_when_disabled_returns_409(self) -> None:
        r = self.client.post("/api/radio/start", headers={"x-gss-token": self.token})
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self.assertIn("disabled", body["error"].lower())

    def test_restart_terminal_cleanup_failure_returns_500(self) -> None:
        self.runtime.platform_cfg["radio"]["enabled"] = True
        failed = {
            "enabled": True,
            "state": "stopped",
            "running": False,
            "error": "previous radio process did not finish terminal cleanup",
        }
        with mock.patch.object(self.runtime.radio, "restart", return_value=failed):
            r = self.client.post(
                "/api/radio/restart",
                headers={"x-gss-token": self.token},
            )
        self.assertEqual(r.status_code, 500)
        self.assertIn("terminal cleanup", r.json()["error"])


class RadioReadAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.client = TestClient(self.app)
        self.runtime = self.app.state.runtime
        self.token = self.runtime.session_token

    def test_status_unauth_default_off(self) -> None:
        r = self.client.get("/api/radio/status")
        self.assertEqual(r.status_code, 200)

    def test_status_unauth_when_enforced_returns_403(self) -> None:
        self.runtime.platform_cfg.setdefault("auth", {})["require_token_for_reads"] = True
        try:
            r = self.client.get("/api/radio/status")
            self.assertEqual(r.status_code, 403)
        finally:
            self.runtime.platform_cfg["auth"]["require_token_for_reads"] = False

    def test_status_with_token_when_enforced_returns_200(self) -> None:
        self.runtime.platform_cfg.setdefault("auth", {})["require_token_for_reads"] = True
        try:
            r = self.client.get("/api/radio/status", headers={"x-gss-token": self.token})
            self.assertEqual(r.status_code, 200)
        finally:
            self.runtime.platform_cfg["auth"]["require_token_for_reads"] = False


if __name__ == "__main__":
    unittest.main()
