"""HTTP <-> WS coupling tests for /api/tracking/doppler/connection/*.

The connect/disconnect endpoint must broadcast a status frame to /ws/tracking
subscribers immediately after the service flips state, so the UI button label
flips within milliseconds rather than waiting for the next 1 Hz doppler tick.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from mav_gss_lib.server.app import create_app


class _NoopSink:
    """Stand-in sink so engage() does not bind real ZMQ ports under test."""
    def publish(self, *_args, **_kwargs) -> None:
        return None

    def close(self) -> None:
        return None


def _await_status(test_case: unittest.TestCase, ws, *, max_frames: int = 8) -> dict:
    """Read frames until a `status` frame arrives. Skips `doppler` ticks that
    can race the test's POST under the 1 Hz tick loop."""
    for _ in range(max_frames):
        msg = ws.receive_json()
        if msg.get("type") == "status":
            return msg
    test_case.fail(f"no status frame within {max_frames} messages")
    return {}  # unreachable; satisfies type checker


class TrackingConnectionEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.client = TestClient(self.app)
        self.runtime = self.app.state.runtime
        self.token = self.runtime.session_token
        # Stub the sink factory before any engage() so we never bind ZMQ.
        self.runtime.tracking._sink_factory = lambda **_: _NoopSink()

    def tearDown(self) -> None:
        try:
            self.runtime.tracking.disengage()
        except Exception:
            pass
        self.client.close()

    def test_connect_unauth_returns_403(self) -> None:
        with self.client:
            r = self.client.post("/api/tracking/doppler/connection/connect")
        self.assertEqual(r.status_code, 403)

    def test_connect_returns_mode_in_body(self) -> None:
        with self.client:
            r = self.client.post(
                "/api/tracking/doppler/connection/connect",
                headers={"x-gss-token": self.token},
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json(), {"connected": True, "mode": "connected"})

    def test_connect_broadcasts_status_to_ws(self) -> None:
        with self.client:
            with self.client.websocket_connect("/ws/tracking") as ws:
                initial = ws.receive_json()
                self.assertEqual(initial["type"], "status")
                self.assertEqual(initial["mode"], "disconnected")

                r = self.client.post(
                    "/api/tracking/doppler/connection/connect",
                    headers={"x-gss-token": self.token},
                )
                self.assertEqual(r.status_code, 200, r.text)

                msg = _await_status(self, ws)
                self.assertEqual(msg["mode"], "connected")

    def test_disconnect_broadcasts_status_to_ws(self) -> None:
        # Engage directly so the disconnect endpoint has work to do.
        self.runtime.tracking.engage()
        with self.client:
            with self.client.websocket_connect("/ws/tracking") as ws:
                initial = ws.receive_json()
                self.assertEqual(initial["mode"], "connected")

                r = self.client.post(
                    "/api/tracking/doppler/connection/disconnect",
                    headers={"x-gss-token": self.token},
                )
                self.assertEqual(r.status_code, 200, r.text)

                msg = _await_status(self, ws)
                self.assertEqual(msg["mode"], "disconnected")

    def test_idempotent_connect_still_broadcasts(self) -> None:
        # Re-engage while already connected: service short-circuits, but the
        # endpoint must still broadcast so a subscriber that missed the
        # original transition catches up on its own next read.
        self.runtime.tracking.engage()
        with self.client:
            with self.client.websocket_connect("/ws/tracking") as ws:
                ws.receive_json()  # initial connected snapshot
                r = self.client.post(
                    "/api/tracking/doppler/connection/connect",
                    headers={"x-gss-token": self.token},
                )
                self.assertEqual(r.status_code, 200, r.text)
                msg = _await_status(self, ws)
                self.assertEqual(msg["mode"], "connected")


class RuntimeTleFetchWiringTests(unittest.TestCase):
    def test_runtime_has_tle_fetch_service(self):
        from mav_gss_lib.server.app import create_app
        app = create_app()
        self.assertTrue(hasattr(app.state.runtime, "tle_fetch"))
        self.assertTrue(callable(app.state.runtime.tle_fetch.fetch_preview))


class TleFetchEndpointTests(unittest.TestCase):
    def setUp(self):
        from mav_gss_lib.server.app import create_app
        self.app = create_app()
        self.client = TestClient(self.app)
        self.runtime = self.app.state.runtime
        self.token = self.runtime.session_token
        from mav_gss_lib.platform.tracking.fetch import FetchResult
        self.runtime.tle_fetch._fetch_fn = lambda settings, now_ms: FetchResult(
            ok=True, via="celestrak", name="MAVERIC",
            line1="1 25544U 98067A   26001.50000000  .00000000  00000-0  00000-0 0  9990",
            line2="2 25544  51.6400   0.0000 0000000   0.0000   0.0000 15.50000000000007",
            tle_epoch_ms=1767225600000)

    def tearDown(self):
        self.client.close()

    def test_fetch_requires_token(self):
        r = self.client.post("/api/tracking/tle/fetch")
        self.assertEqual(r.status_code, 403)

    def test_fetch_returns_preview(self):
        r = self.client.post("/api/tracking/tle/fetch", headers={"x-gss-token": self.token})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["via"], "celestrak")
        self.assertTrue(body["line1"].startswith("1 25544"))

    def test_status_ungated(self):
        r = self.client.get("/api/tracking/tle/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("ok", body)
        self.assertIn("spacetrack", body)
        self.assertEqual(set(body["spacetrack"].keys()), {"identity_set", "password_set"})
        self.assertIsInstance(body["spacetrack"]["identity_set"], bool)
        self.assertIsInstance(body["spacetrack"]["password_set"], bool)


if __name__ == "__main__":
    unittest.main()
