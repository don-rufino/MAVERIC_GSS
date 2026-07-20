"""Unit tests for RadioService — the optional GNU Radio supervisor."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mav_gss_lib.server.radio.service import (
    POST_FIR_IQ_MAX_BYTES,
    RAW_IQ_MAX_BYTES,
    RadioService,
)


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


class _SignalProcess:
    """Small controllable Popen stand-in for lifecycle race tests."""

    def __init__(self, pid: int = 1234) -> None:
        self.pid = pid
        self.stdout = None
        self._exited = threading.Event()

    def poll(self):
        return 0 if self._exited.is_set() else None

    def wait(self, timeout=None):
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired("fake-radio", timeout)
        return 0

    def send_signal(self, _signal) -> None:
        self._exited.set()

    def kill(self) -> None:
        self._exited.set()


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

    def test_explicit_stop_waits_for_terminal_callback(self) -> None:
        svc = RadioService(_fake_runtime())
        proc = _SignalProcess()
        terminal_done = threading.Event()
        fired: list[str] = []
        svc.add_exit_callback(lambda: fired.append("disengage"))
        svc.proc = proc
        svc.started_at = time.time()
        svc._terminal_done = terminal_done
        waiter = threading.Thread(
            target=svc._waiter,
            args=(proc, None, None, terminal_done),
        )
        svc._wait_thread = waiter
        waiter.start()

        status = svc.stop()
        waiter.join(timeout=2)

        self.assertFalse(waiter.is_alive())
        self.assertTrue(terminal_done.is_set())
        self.assertEqual(fired, ["disengage"])
        self.assertIsNone(svc.proc)
        self.assertEqual(status["state"], "stopped")

    def test_stop_signal_exit_race_still_waits_for_callback(self) -> None:
        class _ExitDuringSignal(_SignalProcess):
            def send_signal(self, _signal) -> None:
                self._exited.set()
                raise ProcessLookupError("already exited")

        svc = RadioService(_fake_runtime())
        proc = _ExitDuringSignal()
        terminal_done = threading.Event()
        fired: list[str] = []
        svc.add_exit_callback(lambda: fired.append("disengage"))
        svc.proc = proc
        svc.started_at = time.time()
        svc._terminal_done = terminal_done
        waiter = threading.Thread(
            target=svc._waiter,
            args=(proc, None, None, terminal_done),
        )
        waiter.start()

        status = svc.stop()
        waiter.join(timeout=2)

        self.assertTrue(terminal_done.is_set())
        self.assertEqual(fired, ["disengage"])
        self.assertIsNone(svc.proc)
        self.assertEqual(status["state"], "stopped")

    def test_replacement_waits_until_old_callback_finishes(self) -> None:
        svc = RadioService(_fake_runtime())
        old_proc = SimpleNamespace(poll=lambda: 0, wait=lambda: 0, pid=1)
        new_proc = _SignalProcess(pid=2)
        old_done = threading.Event()
        callback_entered = threading.Event()
        callback_release = threading.Event()

        def slow_disengage() -> None:
            callback_entered.set()
            callback_release.wait(timeout=2)

        svc.add_exit_callback(slow_disengage)
        svc.proc = old_proc
        svc.started_at = time.time()
        svc._terminal_done = old_done
        old_waiter = threading.Thread(
            target=svc._waiter,
            args=(old_proc, None, None, old_done),
        )
        old_waiter.start()
        self.assertTrue(callback_entered.wait(timeout=2))
        self.assertFalse(old_done.is_set())

        inert_log = SimpleNamespace(path=None, write=lambda _line: None,
                                    close=lambda _note: None)
        start_result: list[dict] = []
        start_waiting = threading.Event()
        real_wait_for_terminal = svc._wait_for_terminal

        def observed_wait(done) -> bool:
            start_waiting.set()
            return real_wait_for_terminal(done)

        with mock.patch("subprocess.Popen", return_value=new_proc) as popen, \
             mock.patch.object(svc, "_open_run_log", return_value=inert_log), \
             mock.patch.object(svc, "_wait_for_terminal",
                               side_effect=observed_wait):
            starter = threading.Thread(
                target=lambda: start_result.append(svc.start()),
            )
            starter.start()
            self.assertTrue(start_waiting.wait(timeout=2))
            self.assertFalse(popen.called)
            self.assertIsNone(svc.proc)

            callback_release.set()
            starter.join(timeout=2)

        old_waiter.join(timeout=2)
        self.assertFalse(starter.is_alive())
        self.assertTrue(old_done.is_set())
        self.assertTrue(popen.called)
        self.assertIs(svc.proc, new_proc)
        self.assertEqual(start_result[0]["state"], "running")

        # Release the replacement's daemon waiter and leave the service clean.
        svc.stop()


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
        rt = _fake_runtime({
            "enabled": True,
            "decoder_yml": "gnuradio/decoders/ROADS_DECODER.yml",
        })
        svc = RadioService(rt)
        injected = Path(svc._frequency_env()["GSS_DECODER_YML"])
        self.assertTrue(injected.is_absolute())
        self.assertEqual(
            injected.parts[-3:], ("gnuradio", "decoders", "ROADS_DECODER.yml")
        )
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

    def test_capture_storage_plan_injects_full_caps_above_reserve(self):
        import tempfile as _tempfile
        with _tempfile.TemporaryDirectory() as tmp:
            rt = _fake_runtime({
                "enabled": True,
                "iq_record": True,
                "iq_raw_record": True,
            })
            rt.platform_cfg["general"] = {"log_dir": tmp}
            svc = RadioService(rt)
            env = svc._frequency_env()
            usage = SimpleNamespace(free=80_000_000_000)
            with mock.patch("shutil.disk_usage", return_value=usage):
                notice = svc._plan_capture_storage(env)

            self.assertEqual(notice, "")
            self.assertEqual(env["GSS_IQ_MAX_BYTES"], str(POST_FIR_IQ_MAX_BYTES))
            self.assertEqual(env["GSS_IQ_RAW_MAX_BYTES"], str(RAW_IQ_MAX_BYTES))
            storage = svc.status()["capture_storage"]
            self.assertFalse(storage["constrained"])
            self.assertEqual(storage["allocated_bytes"], 58_000_000_000)
            self.assertEqual(storage["reserve_bytes"], 10_000_000_000)

    def test_capture_storage_plan_scales_both_caps_to_preserve_reserve(self):
        import tempfile as _tempfile
        with _tempfile.TemporaryDirectory() as tmp:
            rt = _fake_runtime({
                "enabled": True,
                "iq_record": True,
                "iq_raw_record": True,
            })
            rt.platform_cfg["general"] = {"log_dir": tmp}
            svc = RadioService(rt)
            env = svc._frequency_env()
            # 39 GB free - 10 GB reserve = 29 GB across both products.
            usage = SimpleNamespace(free=39_000_000_000)
            with mock.patch("shutil.disk_usage", return_value=usage):
                notice = svc._plan_capture_storage(env)

            self.assertIn("reduced", notice)
            post_cap = int(env["GSS_IQ_MAX_BYTES"])
            raw_cap = int(env["GSS_IQ_RAW_MAX_BYTES"])
            self.assertEqual(post_cap + raw_cap, 29_000_000_000)
            self.assertGreater(post_cap, 0)
            self.assertGreater(raw_cap, post_cap)
            self.assertTrue(svc.status()["capture_storage"]["constrained"])

    def test_capture_storage_plan_disables_recorders_below_reserve(self):
        import tempfile as _tempfile
        with _tempfile.TemporaryDirectory() as tmp:
            rt = _fake_runtime({
                "enabled": True,
                "iq_record": True,
                "iq_raw_record": True,
            })
            rt.platform_cfg["general"] = {"log_dir": tmp}
            svc = RadioService(rt)
            env = svc._frequency_env()
            usage = SimpleNamespace(free=9_000_000_000)
            with mock.patch("shutil.disk_usage", return_value=usage):
                notice = svc._plan_capture_storage(env)

            self.assertIn("disabled", notice)
            self.assertNotIn("GSS_IQ_RECORD", env)
            self.assertNotIn("GSS_IQ_RAW_RECORD", env)
            self.assertEqual(svc.status()["capture_storage"]["allocated_bytes"], 0)

    def test_capture_storage_reserve_can_be_configured(self):
        svc = RadioService(_fake_runtime({
            "enabled": True,
            "iq_disk_reserve_gb": 3.5,
        }))
        self.assertEqual(svc.iq_disk_reserve_bytes(), 3_500_000_000)

    def test_frequency_env_carries_rx_gain_and_build_sha(self):
        rt = _fake_runtime({"enabled": True, "rx_gain": 70})
        rt.platform_cfg["general"] = {"build_sha": "abc1234"}
        env = RadioService(rt)._frequency_env()
        self.assertEqual(env["GSS_RX_GAIN"], "70.0")
        self.assertEqual(env["GSS_BUILD_SHA"], "abc1234")
        # Non-numeric gain never reaches the flowgraph.
        rt_bad = _fake_runtime({"enabled": True, "rx_gain": "high"})
        self.assertNotIn("GSS_RX_GAIN", RadioService(rt_bad)._frequency_env())

    def test_frequency_env_carries_rx_bw(self):
        rt = _fake_runtime({"enabled": True, "rx_bw": 25_000})
        env = RadioService(rt)._frequency_env()
        self.assertEqual(env["GSS_RX_BW_HZ"], "25000.0")
        # Non-numeric bandwidth never reaches the flowgraph; its own boot
        # default (50e3) applies instead.
        rt_bad = _fake_runtime({"enabled": True, "rx_bw": "wide"})
        self.assertNotIn("GSS_RX_BW_HZ", RadioService(rt_bad)._frequency_env())

    def test_run_log_persists_radio_stdout(self):
        import tempfile as _tempfile
        with _tempfile.TemporaryDirectory() as tmp:
            rt = _fake_runtime()
            rt.platform_cfg["general"] = {"log_dir": tmp}
            svc = RadioService(rt)
            self.assertIsNone(svc.status()["log_file"])
            run_log = svc._open_run_log(["python3", "gnuradio/MAV_DUO.py"])
            run_log.write(
                "10:00:00 MAV_DUO decoder database: "
                "/opt/gss/gnuradio/decoders/MAVERIC_DECODER.yml (mission maveric)"
            )
            run_log.write('10:00:10 STREAM_HEALTH {"rms_dbfs": -44.0}')
            run_log.close("process exited code=0")
            log_file = svc.status()["log_file"]
            self.assertIsNotNone(log_file)
            self.assertEqual(os.path.basename(os.path.dirname(log_file)), "radio")
            self.assertTrue(os.path.basename(log_file).startswith("radio_maveric_"))
            text = Path(log_file).read_text(encoding="utf-8")
            self.assertIn("# command: python3 gnuradio/MAV_DUO.py", text)
            self.assertIn("MAVERIC_DECODER.yml (mission maveric)", text)
            self.assertIn("STREAM_HEALTH", text)
            self.assertIn("# process exited code=0", text)
            # closing twice / writing after close must be harmless
            run_log.close("again")
            run_log.write("post-close line never raises")

    def test_run_log_failure_disables_quietly(self):
        import tempfile as _tempfile
        with _tempfile.NamedTemporaryFile() as blocker:
            rt = _fake_runtime()
            # log_dir points at a FILE -> mkdir of <file>/radio must fail
            rt.platform_cfg["general"] = {"log_dir": blocker.name}
            svc = RadioService(rt)
            run_log = svc._open_run_log(["python3", "x.py"])  # must not raise
            run_log.write("still fine without a run log")
            run_log.close("noop")
            self.assertIsNone(run_log.path)
            self.assertIsNone(svc.status()["log_file"])

    def test_superseded_run_cannot_touch_new_log_or_fire_callbacks(self):
        # The restart race: the OLD process's waiter must never close the
        # NEW run's log, and must not fire exit callbacks (state.py wires
        # tracking.disengage there — a phantom exit would kill Doppler
        # under the replacement radio).
        import tempfile as _tempfile
        with _tempfile.TemporaryDirectory() as tmp:
            rt = _fake_runtime()
            rt.platform_cfg["general"] = {"log_dir": tmp}
            svc = RadioService(rt)
            fired: list[str] = []
            svc.add_exit_callback(lambda: fired.append("disengage"))

            old_log = svc._open_run_log(["old-run"])
            new_log = svc._open_run_log(["new-run"])
            self.assertNotEqual(str(old_log.path), str(new_log.path))
            self.assertEqual(svc.status()["log_file"], str(new_log.path))

            old_proc = SimpleNamespace(poll=lambda: 0, wait=lambda: 0)
            new_proc = SimpleNamespace(poll=lambda: None, wait=lambda: 0)
            svc.proc = new_proc  # the replacement run is current

            svc._waiter(old_proc, old_log)  # superseded exit
            self.assertEqual(fired, [])  # no phantom disengage
            new_log.write("10:00:01 still writable after old run closed")
            new_text = Path(new_log.path).read_text(encoding="utf-8")
            self.assertIn("still writable", new_text)
            self.assertNotIn("process exited", new_text)
            old_text = Path(old_log.path).read_text(encoding="utf-8")
            self.assertIn("# process exited code=0", old_text)

            # the CURRENT run exiting still fires callbacks exactly once
            svc.started_at = 0.0
            svc._waiter(new_proc, new_log)
            self.assertEqual(fired, ["disengage"])

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

    def test_stream_health_is_marked_stale_while_running(self):
        svc = RadioService(_fake_runtime())
        svc.proc = _SignalProcess()
        svc.started_at = time.time()
        svc._stream_health = {
            "rms_dbfs": -44.1,
            "ts_ms": int((time.time() - 31.0) * 1000),
        }
        health = svc.status()["stream_health"]
        self.assertTrue(health["stale"])
        self.assertGreaterEqual(health["age_s"], 30.0)

    def test_tx_health_lines_surface_in_status(self):
        svc = RadioService(_fake_runtime())
        self.assertIsNone(svc.status()["tx_health"])
        svc._ingest_tx_health(
            'TX_HEALTH {"underflows_total": 3, "seq_errors_total": 1, '
            '"time_errors_total": 0, "last_event_code": 2}')
        health = svc.status()["tx_health"]
        self.assertEqual(health["underflows_total"], 3)
        self.assertEqual(health["seq_errors_total"], 1)
        self.assertGreater(health["ts_ms"], 0)
        self.assertFalse(health["stale"])
        svc._ingest_tx_health("TX_HEALTH {not json")
        self.assertEqual(svc.status()["tx_health"]["underflows_total"], 3)

    def test_tx_health_is_marked_stale_while_running(self):
        svc = RadioService(_fake_runtime())
        svc.proc = _SignalProcess()
        svc.started_at = time.time()
        svc._tx_health = {
            "underflows_total": 0,
            "ts_ms": int((time.time() - 31.0) * 1000),
        }
        self.assertTrue(svc.status()["tx_health"]["stale"])

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
        # UTC, seconds resolution: HH:MM:SSZ
        self.assertRegex(snapshot[0], r"^\d{2}:\d{2}:\d{2}Z\s")
        self.assertTrue(snapshot[0].endswith("hello world"))


if __name__ == "__main__":
    unittest.main()
