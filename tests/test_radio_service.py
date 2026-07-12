"""Unit tests for RadioService — the optional GNU Radio supervisor."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mav_gss_lib.server.radio.service import RadioService


def _fake_runtime(radio_cfg=None):
    radio_cfg = radio_cfg or {"enabled": True, "script": "gnuradio/MAV_DUO.py"}
    return SimpleNamespace(
        platform_cfg={
            "radio": radio_cfg,
            "rx": {"frequency": "437.575 MHz"},
            "tx": {"frequency": "437575000"},
            "tracking": {"frequencies": {"rx_hz": 437_575_000.0, "tx_hz": 437_575_000.0}},
        },
        cfg_lock=threading.Lock(),
        mission_id="maveric",
        rx=SimpleNamespace(log=None),
        tx=SimpleNamespace(log=None),
    )


class RadioServiceExitCallbackTests(unittest.TestCase):
    def test_exit_callbacks_fire_on_process_exit(self) -> None:
        svc = RadioService(_fake_runtime())
        fired: list[str] = []
        svc.add_exit_callback(lambda: fired.append("called"))

        fake_proc = SimpleNamespace(poll=lambda: 0, wait=lambda: 0)
        svc.proc = fake_proc
        svc.started_at = 0.0
        svc._waiter(fake_proc)

        self.assertEqual(fired, ["called"])

    def test_exit_callback_failure_is_logged_not_raised(self) -> None:
        svc = RadioService(_fake_runtime())

        def boom() -> None:
            raise RuntimeError("callback exploded")

        svc.add_exit_callback(boom)

        fake_proc = SimpleNamespace(poll=lambda: 0, wait=lambda: 0)
        svc.proc = fake_proc
        svc.started_at = 0.0
        svc._waiter(fake_proc)  # must not raise


class RadioServiceConfigTests(unittest.TestCase):
    def test_log_capacity_clamped(self):
        rt = _fake_runtime({"log_lines": 50})
        svc = RadioService(rt)
        self.assertEqual(svc.log_capacity(), 100)  # clamps to floor

    def test_disabled_start_returns_status_with_error(self):
        rt = _fake_runtime({"enabled": False})
        svc = RadioService(rt)
        result = svc.start()
        self.assertEqual(result["state"], "stopped")
        self.assertIn("disabled", result["error"].lower())

    def test_frequency_env_includes_absolute_waterfall_dir(self):
        svc = RadioService(_fake_runtime())
        env = svc._frequency_env()
        waterfall_dir = env["GSS_WATERFALL_DIR"]
        self.assertTrue(os.path.isabs(waterfall_dir))
        self.assertEqual(os.path.basename(waterfall_dir), "waterfalls")
        # _fake_runtime has no general.log_dir, so the default "logs" applies
        self.assertEqual(os.path.basename(os.path.dirname(waterfall_dir)), "logs")

    def test_frequency_env_reads_split_rx_tx_config(self):
        rt = _fake_runtime({"enabled": True})
        rt.platform_cfg["rx"]["frequency"] = "437.7 MHz"
        rt.platform_cfg["tx"]["frequency"] = "437800000"
        svc = RadioService(rt)
        self.assertEqual(svc._frequency_env()["GSS_RX_FREQ_HZ"], "437700000.0")
        self.assertEqual(svc._frequency_env()["GSS_TX_FREQ_HZ"], "437800000.0")

    def test_frequency_env_falls_back_to_tracking_base(self):
        rt = _fake_runtime({"enabled": True})
        rt.platform_cfg["rx"].pop("frequency")
        rt.platform_cfg["tx"].pop("frequency")
        rt.platform_cfg["tracking"]["frequencies"] = {
            "rx_hz": 437_610_000.0,
            "tx_hz": 437_620_000.0,
        }
        svc = RadioService(rt)
        self.assertEqual(svc._frequency_env()["GSS_RX_FREQ_HZ"], "437610000.0")
        self.assertEqual(svc._frequency_env()["GSS_TX_FREQ_HZ"], "437620000.0")

    def test_frequency_env_carries_lo_offsets_from_control_defaults(self):
        rt = _fake_runtime({"enabled": True})
        svc = RadioService(rt)
        env = svc._frequency_env()
        self.assertEqual(env["GSS_RX_LO_OFFSET_HZ"], "250000.0")
        self.assertEqual(env["GSS_TX_LO_OFFSET_HZ"], "-400000.0")

    def test_frequency_env_carries_configured_lo_offsets(self):
        rt = _fake_runtime({"enabled": True})
        rt.platform_cfg["tracking"]["control"] = {
            "rx_lo_offset_hz": 111_000.0,
            "tx_lo_offset_hz": -222_000.0,
        }
        svc = RadioService(rt)
        env = svc._frequency_env()
        self.assertEqual(env["GSS_RX_LO_OFFSET_HZ"], "111000.0")
        self.assertEqual(env["GSS_TX_LO_OFFSET_HZ"], "-222000.0")

    def test_frequency_env_carries_explicit_decoder_yml(self):
        # Config value is repo-root-relative; the env must arrive absolute
        # because the flowgraph child runs with cwd=gnuradio/, where a
        # relative path would resolve to gnuradio/gnuradio/....
        rt = _fake_runtime({"enabled": True, "decoder_yml": "gnuradio/ROADS_DECODER.yml"})
        svc = RadioService(rt)
        injected = Path(svc._frequency_env()["GSS_DECODER_YML"])
        self.assertTrue(injected.is_absolute())
        self.assertEqual(injected.parts[-2:], ("gnuradio", "ROADS_DECODER.yml"))
        self.assertTrue(injected.is_file())

    def test_frequency_env_omits_decoder_yml_by_default(self):
        # Without an explicit override the flowgraph's own
        # <GSS_MISSION>_DECODER.yml convention picks the database.
        svc = RadioService(_fake_runtime())
        self.assertNotIn("GSS_DECODER_YML", svc._frequency_env())

    def test_frequency_env_gates_iq_recording(self):
        rt = _fake_runtime({"enabled": True, "iq_record": True})
        svc = RadioService(rt)
        env = svc._frequency_env()
        self.assertEqual(env["GSS_IQ_RECORD"], "1")
        iq_dir = env["GSS_IQ_DIR"]
        self.assertTrue(os.path.isabs(iq_dir))
        self.assertEqual(os.path.basename(iq_dir), "iq")
        self.assertEqual(os.path.basename(os.path.dirname(iq_dir)), "logs")

    def test_frequency_env_gates_raw_iq_and_silences_uhd_fastpath(self):
        svc = RadioService(_fake_runtime({"enabled": True, "iq_raw_record": True}))
        env = svc._frequency_env()
        self.assertEqual(env["GSS_IQ_RAW_RECORD"], "1")
        # Overflows are counted flowgraph-side (rx_time tags); the raw O/U
        # fastpath chars would only garble the line-based log stream.
        self.assertEqual(env["UHD_LOG_FASTPATH_DISABLE"], "1")
        off = RadioService(_fake_runtime())._frequency_env()
        self.assertNotIn("GSS_IQ_RAW_RECORD", off)

    def test_frequency_env_carries_rx_gain_and_build_sha(self):
        rt = _fake_runtime({"enabled": True, "rx_gain": 70})
        rt.platform_cfg["general"] = {"build_sha": "abc1234"}
        env = RadioService(rt)._frequency_env()
        self.assertEqual(env["GSS_RX_GAIN"], "70.0")
        self.assertEqual(env["GSS_BUILD_SHA"], "abc1234")
        # Non-numeric gain never reaches the flowgraph.
        rt_bad = _fake_runtime({"enabled": True, "rx_gain": "high"})
        self.assertNotIn("GSS_RX_GAIN", RadioService(rt_bad)._frequency_env())

    def test_stream_health_lines_surface_in_status(self):
        svc = RadioService(_fake_runtime())
        self.assertIsNone(svc.status()["stream_health"])
        svc._ingest_stream_health(
            'STREAM_HEALTH {"rms_dbfs": -44.1, "peak_dbfs": -22.3, '
            '"clip_count": 0, "overflows_total": 2, "span_s": 10.0}')
        health = svc.status()["stream_health"]
        self.assertEqual(health["rms_dbfs"], -44.1)
        self.assertEqual(health["overflows_total"], 2)
        self.assertGreater(health["ts_ms"], 0)
        # Malformed payloads never clobber the last good report.
        svc._ingest_stream_health("STREAM_HEALTH {not json")
        self.assertEqual(svc.status()["stream_health"]["rms_dbfs"], -44.1)

    def test_frequency_env_omits_iq_gate_when_disabled(self):
        svc = RadioService(_fake_runtime())
        env = svc._frequency_env()
        self.assertNotIn("GSS_IQ_RECORD", env)
        # Destination is injected unconditionally; only the gate toggles.
        self.assertIn("GSS_IQ_DIR", env)

    def test_frequency_env_carries_mission_id(self):
        svc = RadioService(_fake_runtime())
        self.assertEqual(svc._frequency_env()["GSS_MISSION"], "maveric")

    def test_frequency_env_omits_empty_mission_id(self):
        rt = _fake_runtime()
        rt.mission_id = ""
        svc = RadioService(rt)
        self.assertNotIn("GSS_MISSION", svc._frequency_env())


class RadioServiceLoopBindingTests(unittest.TestCase):
    def test_schedule_broadcast_no_loop_is_silent(self):
        svc = RadioService(_fake_runtime())
        # Must not raise even without bind_loop
        svc._schedule_broadcast({"type": "log", "line": "hello"})

    def test_schedule_broadcast_uses_most_recently_bound_loop(self):
        svc = RadioService(_fake_runtime())
        loop_a = asyncio.new_event_loop()
        loop_b = asyncio.new_event_loop()
        try:
            calls: list[asyncio.AbstractEventLoop] = []

            def fake_run(coro, loop):
                calls.append(loop)
                coro.close()
                return mock.MagicMock()

            with mock.patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run):
                svc.bind_loop(loop_a)
                svc._schedule_broadcast({"type": "log", "line": "x"})
                svc.bind_loop(loop_b)
                svc._schedule_broadcast({"type": "log", "line": "y"})
            self.assertEqual(calls, [loop_a, loop_b])
        finally:
            loop_a.close(); loop_b.close()

    def test_schedule_broadcast_concurrent_bind_safe(self):
        svc = RadioService(_fake_runtime())
        loop = asyncio.new_event_loop()
        try:
            errors: list[BaseException] = []

            def hammer_bind():
                try:
                    for _ in range(200):
                        svc.bind_loop(loop)
                except BaseException as e:
                    errors.append(e)

            def hammer_broadcast():
                try:
                    with mock.patch("asyncio.run_coroutine_threadsafe", return_value=mock.MagicMock()):
                        for _ in range(200):
                            svc._schedule_broadcast({"type": "log", "line": "x"})
                except BaseException as e:
                    errors.append(e)

            t1 = threading.Thread(target=hammer_bind)
            t2 = threading.Thread(target=hammer_broadcast)
            t1.start(); t2.start()
            t1.join(); t2.join()
            self.assertEqual(errors, [])
        finally:
            loop.close()


class RadioServiceActionLockTests(unittest.TestCase):
    def test_concurrent_start_stop_serialized(self):
        svc = RadioService(_fake_runtime({"enabled": False}))  # disabled → start() short-circuits cheaply
        results: list[str] = []

        def call_start():
            results.append("start:" + svc.start()["state"])

        def call_stop():
            results.append("stop:" + svc.stop()["state"])

        threads = [threading.Thread(target=call_start) for _ in range(4)] + \
                  [threading.Thread(target=call_stop) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(results), 8)


class RadioServicePersistenceTests(unittest.TestCase):
    def test_status_includes_last_runtime_s_field(self):
        svc = RadioService(_fake_runtime())
        status = svc.status()
        self.assertIn("last_runtime_s", status)
        self.assertEqual(status["last_runtime_s"], 0.0)

    def test_stop_timeout_reads_from_config(self):
        svc = RadioService(_fake_runtime({"enabled": True, "stop_timeout_s": 12.5}))
        self.assertEqual(svc.stop_timeout_s(), 12.5)

    def test_stop_timeout_default(self):
        svc = RadioService(_fake_runtime())
        self.assertEqual(svc.stop_timeout_s(), 30.0)


class RadioServiceLogPrefixTests(unittest.TestCase):
    def test_appended_line_starts_with_timestamp(self):
        svc = RadioService(_fake_runtime())
        svc._append_log("hello world")
        snapshot = svc.log_snapshot()
        self.assertEqual(len(snapshot), 1)
        # Local time, seconds resolution: HH:MM:SS
        self.assertRegex(snapshot[0], r"^\d{2}:\d{2}:\d{2}\s")
        self.assertTrue(snapshot[0].endswith("hello world"))


if __name__ == "__main__":
    unittest.main()
