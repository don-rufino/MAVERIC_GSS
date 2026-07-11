"""
mav_gss_lib.server.radio.service -- GNU Radio Process Supervisor

Owns the optional GNU Radio flowgraph child process used by the web runtime.
The flowgraph remains an external Qt/GNU Radio process; this service only
starts/stops it, captures stdout/stderr, and fans log lines out to browser
clients.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from mav_gss_lib.config import get_tracking_control, resolve_project_path

from .._broadcast import broadcast_safe

if TYPE_CHECKING:
    from ..state import WebRuntime


DEFAULT_RADIO_SCRIPT = "gnuradio/MAV_DUO.py"
DEFAULT_LOG_LINES = 1000
DEFAULT_STOP_TIMEOUT_S = 30.0
_FREQ_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(ghz|mhz|khz|hz)?\s*$", re.IGNORECASE)


def _parse_frequency_hz(value: Any, fallback: float | None = None) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else fallback
    if not isinstance(value, str):
        return fallback
    match = _FREQ_RE.match(value)
    if not match:
        return fallback
    numeric = float(match.group(1))
    unit = (match.group(2) or "").lower()
    if unit == "ghz":
        scale = 1_000_000_000
    elif unit == "mhz" or (not unit and numeric < 10_000):
        scale = 1_000_000
    elif unit == "khz":
        scale = 1_000
    else:
        scale = 1
    return numeric * scale


class RadioService:
    """Supervise one optional GNU Radio flowgraph process."""

    def __init__(self, runtime: "WebRuntime") -> None:
        self.runtime = runtime
        self.clients: list = []
        self.lock = threading.Lock()
        self.proc: subprocess.Popen[str] | None = None
        self.started_at: float | None = None
        self.last_exit_code: int | None = None
        self.last_error: str = ""
        self.last_stop_expected: bool = False
        self._reader_thread: threading.Thread | None = None
        self._wait_thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._action_lock = threading.Lock()
        self._stopping = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_lock = threading.Lock()
        self._log: deque[str] = deque(maxlen=DEFAULT_LOG_LINES)
        self.last_runtime_s: float = 0.0
        self._command_snapshot: list[str] = []
        self._exit_callbacks: list[Callable[[], None]] = []

    def add_exit_callback(self, cb: Callable[[], None]) -> None:
        with self._state_lock:
            self._exit_callbacks.append(cb)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._loop_lock:
            self._loop = loop

    def config(self) -> dict[str, Any]:
        radio = self.runtime.platform_cfg.get("radio")
        return radio if isinstance(radio, dict) else {}

    def enabled(self) -> bool:
        cfg = self.config()
        return bool(cfg.get("enabled", True))

    def autostart(self) -> bool:
        cfg = self.config()
        return bool(cfg.get("autostart", False))

    def log_capacity(self) -> int:
        cfg = self.config()
        try:
            return max(100, min(int(cfg.get("log_lines", DEFAULT_LOG_LINES)), 10000))
        except (TypeError, ValueError):
            return DEFAULT_LOG_LINES

    def stop_timeout_s(self) -> float:
        cfg = self.config()
        try:
            return max(1.0, min(float(cfg.get("stop_timeout_s", DEFAULT_STOP_TIMEOUT_S)), 120.0))
        except (TypeError, ValueError):
            return DEFAULT_STOP_TIMEOUT_S

    def _resize_log_if_needed(self) -> None:
        capacity = self.log_capacity()
        if self._log.maxlen == capacity:
            return
        self._log = deque(self._log, maxlen=capacity)

    def _script_path(self) -> Path:
        cfg = self.config()
        raw_script = str(cfg.get("script") or DEFAULT_RADIO_SCRIPT)
        return resolve_project_path(raw_script)

    def _python_path(self) -> str:
        cfg = self.config()
        raw_python = cfg.get("python")
        return str(raw_python) if raw_python else sys.executable

    def _args(self) -> list[str]:
        cfg = self.config()
        raw_args = cfg.get("args", [])
        if not isinstance(raw_args, list):
            return []
        return [str(arg) for arg in raw_args]

    def _frequency_env(self) -> dict[str, str]:
        with self.runtime.cfg_lock:
            platform_cfg = self.runtime.platform_cfg
            rx_cfg = platform_cfg.get("rx") if isinstance(platform_cfg.get("rx"), dict) else {}
            tx_cfg = platform_cfg.get("tx") if isinstance(platform_cfg.get("tx"), dict) else {}
            tracking = platform_cfg.get("tracking") if isinstance(platform_cfg.get("tracking"), dict) else {}
            frequencies = tracking.get("frequencies") if isinstance(tracking.get("frequencies"), dict) else {}
            rx_hz = _parse_frequency_hz(rx_cfg.get("frequency"), _parse_frequency_hz(frequencies.get("rx_hz")))
            tx_hz = _parse_frequency_hz(tx_cfg.get("frequency"), _parse_frequency_hz(frequencies.get("tx_hz")))
            control = get_tracking_control(platform_cfg)
            general = platform_cfg.get("general") if isinstance(platform_cfg.get("general"), dict) else {}
            log_dir_raw = str(general.get("log_dir", "logs"))
        env: dict[str, str] = {}
        if rx_hz is not None:
            env["GSS_RX_FREQ_HZ"] = str(rx_hz)
        if tx_hz is not None:
            env["GSS_TX_FREQ_HZ"] = str(tx_hz)
        # Parked-LO placement for MAV_DUO's rx_lo_offset / tx_lo_offset
        # variables, single-sourced from tracking.control so the flowgraph
        # and the doppler sink can never disagree on where the LOs sit.
        env["GSS_RX_LO_OFFSET_HZ"] = str(control["rx_lo_offset_hz"])
        env["GSS_TX_LO_OFFSET_HZ"] = str(control["tx_lo_offset_hz"])
        # Absolute on purpose: the radio child runs with cwd=gnuradio/, so a
        # relative log_dir would land waterfall PNGs inside the flowgraph dir.
        env["GSS_WATERFALL_DIR"] = str(resolve_project_path(log_dir_raw) / "waterfalls")
        # Mission id rides into the flowgraph so waterfall captures carry the
        # active mission in their filenames.
        mission_id = str(self.runtime.mission_id or "")
        if mission_id:
            env["GSS_MISSION"] = mission_id
        return env

    def command(self) -> list[str]:
        return [self._python_path(), "-u", str(self._script_path()), *self._args()]

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            proc = self.proc
            started_at = self.started_at
            last_exit_code = self.last_exit_code
            last_error = self.last_error
            last_stop_expected = self.last_stop_expected
            stopping = self._stopping
            last_runtime_s = self.last_runtime_s
            command_snapshot = list(self._command_snapshot)

        running = proc is not None and proc.poll() is None
        if running:
            state = "stopping" if stopping else "running"
            pid = proc.pid
            exit_code = None
        else:
            pid = None
            exit_code = last_exit_code
            if exit_code is None or exit_code == 0 or last_stop_expected:
                state = "stopped"
            else:
                state = "crashed"

        script = self._script_path()
        return {
            "enabled": self.enabled(),
            "autostart": self.autostart(),
            "state": state,
            "running": running,
            "pid": pid,
            "started_at_ms": int(started_at * 1000) if started_at else None,
            "uptime_s": max(0.0, time.time() - started_at) if running and started_at else 0.0,
            "exit_code": exit_code,
            "error": last_error,
            "script": str(script),
            "cwd": str(script.parent),
            "command": list(command_snapshot) if running and command_snapshot else self.command(),
            "log_lines": self.log_capacity(),
            "last_runtime_s": float(last_runtime_s),
            "stop_timeout_s": self.stop_timeout_s(),
        }

    def log_snapshot(self) -> list[str]:
        with self._state_lock:
            self._resize_log_if_needed()
            return list(self._log)

    def _append_log(self, line: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        stamped = f"{ts} {line}" if line else ts
        with self._state_lock:
            self._resize_log_if_needed()
            self._log.append(stamped)
        self._schedule_broadcast({"type": "log", "line": stamped})

    async def broadcast(self, msg: dict[str, Any] | str) -> None:
        text = json.dumps(msg) if isinstance(msg, dict) else msg
        await broadcast_safe(self.clients, self.lock, text)

    def _schedule_broadcast(self, msg: dict[str, Any]) -> None:
        with self._loop_lock:
            loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(msg), loop)
        except RuntimeError:
            pass

    def _set_error(self, message: str) -> None:
        with self._state_lock:
            self.last_error = message

    def _write_radio_event(
        self,
        action: str,
        *,
        status: dict[str, Any] | None = None,
        detail: str = "",
        expected: bool | None = None,
    ) -> None:
        log = getattr(getattr(self.runtime, "rx", None), "log", None)
        if log is None:
            log = getattr(getattr(self.runtime, "tx", None), "log", None)
        if log is None or not hasattr(log, "write_radio_event"):
            return
        snapshot = status or self.status()
        try:
            log.write_radio_event(
                action,
                state=str(snapshot.get("state") or ""),
                pid=snapshot.get("pid"),
                exit_code=snapshot.get("exit_code"),
                command=list(snapshot.get("command") or ()),
                script=str(snapshot.get("script") or ""),
                cwd=str(snapshot.get("cwd") or ""),
                detail=detail,
                expected=expected,
            )
        except Exception:
            logging.exception("radio lifecycle log failed")

    def _start_locked(self) -> dict[str, Any]:
        if not self.enabled():
            self._set_error("radio integration disabled")
            status = self.status()
            self._write_radio_event(
                "start_failed", status=status, detail="radio integration disabled",
            )
            return status

        already_running = False
        with self._state_lock:
            if self.proc is not None and self.proc.poll() is None:
                already_running = True
        if already_running:
            return self.status()

        script = self._script_path()
        if not script.is_file():
            self._set_error(f"radio script not found: {script}")
            self._schedule_broadcast({"type": "status", "status": self.status()})
            status = self.status()
            self._write_radio_event("start_failed", status=status, detail=self.last_error)
            return status

        python = self._python_path()
        python_exists = Path(python).is_file() or shutil.which(python) is not None
        if not python_exists:
            self._set_error(f"python executable not found: {python}")
            self._schedule_broadcast({"type": "status", "status": self.status()})
            status = self.status()
            self._write_radio_event("start_failed", status=status, detail=self.last_error)
            return status

        cmd = [python, "-u", str(script), *self._args()]
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env.update(self._frequency_env())
        command_text = " ".join(cmd)

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(script.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            self._set_error(f"radio start failed: {exc}")
            self._schedule_broadcast({"type": "status", "status": self.status()})
            status = self.status()
            self._write_radio_event("start_failed", status=status, detail=self.last_error)
            return status

        with self._state_lock:
            self.proc = proc
            self.started_at = time.time()
            self.last_exit_code = None
            self.last_error = ""
            self.last_stop_expected = False
            self._stopping = False
            self._command_snapshot = list(cmd)
            self._resize_log_if_needed()
            self._log.clear()

        self._reader_thread = threading.Thread(
            target=self._reader,
            args=(proc,),
            daemon=True,
            name="radio-log",
        )
        self._wait_thread = threading.Thread(
            target=self._waiter,
            args=(proc,),
            daemon=True,
            name="radio-wait",
        )
        self._reader_thread.start()
        self._wait_thread.start()
        status = self.status()
        self._write_radio_event("start", status=status, detail=command_text)
        self._schedule_broadcast({"type": "status", "status": status})
        return status

    def _reader(self, proc: subprocess.Popen[str]) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for line in stream:
                self._append_log(line.rstrip("\n"))
        except Exception as exc:
            logging.warning("radio stdout reader failed: %s", exc)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _waiter(self, proc: subprocess.Popen[str]) -> None:
        code = proc.wait()
        should_log = False
        was_stopping = False
        with self._state_lock:
            if self.proc is proc:
                runtime_s = max(0.0, time.time() - self.started_at) if self.started_at else 0.0
                self.last_runtime_s = runtime_s
                self.last_exit_code = code
                self.proc = None
                self.started_at = None
                was_stopping = self._stopping
                self.last_stop_expected = was_stopping
                self._stopping = False
                if code not in (0, None) and not was_stopping and not self.last_error:
                    self.last_error = f"radio process exited with code {code}"
                should_log = True
        status = self.status()
        if should_log:
            action = "stop" if was_stopping else ("exit" if code in (0, None) else "crash")
            self._write_radio_event(action, status=status, expected=was_stopping)
        self._schedule_broadcast({"type": "exit", "code": code, "status": status})
        with self._state_lock:
            callbacks = list(self._exit_callbacks)
        for cb in callbacks:
            try:
                cb()
            except Exception:
                logging.exception("radio exit callback failed")

    def _stop_locked(self) -> dict[str, Any]:
        already_stopped = False
        with self._state_lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                self._stopping = False
                already_stopped = True
            else:
                self._stopping = True
        if already_stopped:
            return self.status()

        status = self.status()
        self._write_radio_event("stop_requested", status=status, detail="SIGTERM", expected=True)
        self._schedule_broadcast({"type": "status", "status": status})
        code: int | None = None
        should_log_stop = False
        try:
            proc.send_signal(signal.SIGTERM)
            code = proc.wait(timeout=self.stop_timeout_s())
        except subprocess.TimeoutExpired:
            self._append_log("Radio process did not exit after SIGTERM; sending SIGKILL")
            proc.kill()
            try:
                code = proc.wait(timeout=self.stop_timeout_s())
            except subprocess.TimeoutExpired:
                self._set_error("radio process did not exit after SIGKILL")
                self._write_radio_event(
                    "stop_failed",
                    detail="radio process did not exit after SIGKILL",
                    expected=True,
                )
        except OSError as exc:
            self._set_error(f"radio stop failed: {exc}")
            self._write_radio_event("stop_failed", detail=str(exc), expected=True)
        if code is not None:
            with self._state_lock:
                if self.proc is proc:
                    self.last_runtime_s = max(0.0, time.time() - self.started_at) if self.started_at else 0.0
                    self.last_exit_code = code
                    self.proc = None
                    self.started_at = None
                    self.last_stop_expected = True
                    self._stopping = False
                    should_log_stop = True
        status = self.status()
        if should_log_stop:
            self._write_radio_event("stop", status=status, expected=True)
        return status

    def start(self) -> dict[str, Any]:
        with self._action_lock:
            return self._start_locked()

    def stop(self) -> dict[str, Any]:
        with self._action_lock:
            return self._stop_locked()

    def restart(self) -> dict[str, Any]:
        with self._action_lock:
            self._stop_locked()
            return self._start_locked()

    def shutdown(self) -> None:
        self.stop()
