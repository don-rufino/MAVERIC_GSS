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
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from mav_gss_lib.config import get_tracking_control, resolve_project_path

from .._broadcast import broadcast_safe

if TYPE_CHECKING:
    from ..state import WebRuntime


DEFAULT_RADIO_SCRIPT = "gnuradio/MAV_DUO.py"
DEFAULT_LOG_LINES = 1000
DEFAULT_STOP_TIMEOUT_S = 30.0
TERMINAL_FINALIZE_TIMEOUT_S = 10.0
HEALTH_STALE_AFTER_S = 30.0
DEFAULT_IQ_DISK_RESERVE_BYTES = 10_000_000_000
POST_FIR_IQ_MAX_BYTES = 8_000_000_000
RAW_IQ_MAX_BYTES = 50_000_000_000
_FREQ_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(ghz|mhz|khz|hz)?\s*$", re.IGNORECASE)


def _stamp_log_line(line: str) -> str:
    # The persistent run-log header is UTC; keep every line in that same
    # clock domain so a pass can be correlated with tracking and SigMF data
    # without first knowing the station host's local timezone.
    ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    return f"{ts} {line}" if line else ts


class _RunLog:
    """One radio run's persistent stdout log file.

    Owned by that run's reader/waiter threads, so a superseded process can
    only ever close ITS OWN file — never the replacement run's (the restart
    race this class exists to prevent). Failure-safe: any I/O error disables
    the instance and the radio keeps running.
    """

    def __init__(self, path: Path | None, handle) -> None:
        self.path = path
        self._handle = handle
        self._lock = threading.Lock()

    def write(self, stamped: str) -> None:
        with self._lock:
            if self._handle is None:
                return
            try:
                self._handle.write(stamped + "\n")
                self._handle.flush()
            except Exception as exc:
                logging.warning("radio run log write failed (%s); disabling", exc)
                self._close_quietly()

    def close(self, note: str) -> None:
        with self._lock:
            if self._handle is None:
                return
            try:
                self._handle.write(f"# {note}\n")
            except Exception:
                pass
            self._close_quietly()

    def _close_quietly(self) -> None:
        try:
            if self._handle is not None:
                self._handle.close()
        except Exception:
            pass
        self._handle = None


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
        self._stream_health: dict[str, Any] | None = None
        self._tx_health: dict[str, Any] | None = None
        self._capture_storage: dict[str, Any] | None = None
        self._run_log_path: Path | None = None
        # Set only after the current run's waiter has drained stdout,
        # published its terminal state, and completed exit callbacks. Public
        # start/stop/restart actions are serialized by _action_lock and wait
        # on this barrier, so a replacement process cannot be installed while
        # the previous run can still disengage tracking or publish stale data.
        self._terminal_done: threading.Event | None = None

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

    def iq_disk_reserve_bytes(self) -> int:
        """Free space that IQ recording is never allowed to consume.

        The optional config value is expressed in decimal GB to match the
        recorder caps and operator-facing storage figures. Keeping the
        fallback here avoids making capture safety depend on a UI/config
        migration; existing station configs immediately get the reserve.
        """
        cfg = self.config()
        try:
            return max(
                0,
                int(float(cfg.get("iq_disk_reserve_gb", 10.0)) * 1_000_000_000),
            )
        except (TypeError, ValueError):
            return DEFAULT_IQ_DISK_RESERVE_BYTES

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
            radio_cfg = platform_cfg.get("radio") if isinstance(platform_cfg.get("radio"), dict) else {}
            iq_record = bool(radio_cfg.get("iq_record", False))
            iq_raw_record = bool(radio_cfg.get("iq_raw_record", False))
            rx_gain = radio_cfg.get("rx_gain")
            decoder_yml = radio_cfg.get("decoder_yml")
            build_sha = str(general.get("build_sha") or "")
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
        # Mission-specific gr-satellites SatYAML (seeded by the mission's
        # build(ctx), like radio.script); MAV_DUO falls back to
        # MAVERIC_DECODER.yml when unset. Repo-root-relative or absolute,
        # like every other config path — absolutized here because the child
        # runs from gnuradio/, where a relative value would resolve wrongly.
        if isinstance(decoder_yml, str) and decoder_yml.strip():
            env["GSS_DECODER_YML"] = str(resolve_project_path(decoder_yml.strip()))
        if iq_record:
            env["GSS_IQ_RECORD"] = "1"
        if iq_raw_record:
            env["GSS_IQ_RAW_RECORD"] = "1"
        # Destination rides along unconditionally so the config toggle is the
        # only on/off difference the flowgraph ever sees.
        env["GSS_IQ_DIR"] = str(resolve_project_path(log_dir_raw) / "iq")
        # Boot RX gain (GUI slider still overrides live) — also stamped into
        # SigMF capture provenance by the flowgraph recorders.
        if isinstance(rx_gain, (int, float)):
            env["GSS_RX_GAIN"] = str(float(rx_gain))
        if build_sha:
            env["GSS_BUILD_SHA"] = build_sha
        # Overflows are counted flowgraph-side via rx_time stream tags and
        # reported in STREAM_HEALTH lines; UHD's raw O/U fastpath characters
        # arrive without line boundaries and only garble the log stream.
        env["UHD_LOG_FASTPATH_DISABLE"] = "1"
        # Mission id rides into the flowgraph so waterfall captures carry the
        # active mission in their filenames.
        mission_id = str(self.runtime.mission_id or "")
        if mission_id:
            env["GSS_MISSION"] = mission_id
        return env

    def _plan_capture_storage(self, env: dict[str, str]) -> str:
        """Bound enabled IQ recorders to space available above the reserve.

        This is deliberately fail-open for the radio and fail-closed for the
        optional recorders: an unavailable/full capture destination disables
        recording for that run but never prevents receiving/decoding. The
        flowgraphs consume the two injected byte caps and stop their own file
        writers when they reach them.

        Returns an operator-facing notice when recording was constrained.
        """
        post_requested = env.get("GSS_IQ_RECORD") == "1"
        raw_requested = env.get("GSS_IQ_RAW_RECORD") == "1"
        if not post_requested and not raw_requested:
            with self._state_lock:
                self._capture_storage = None
            return ""

        iq_dir = Path(env["GSS_IQ_DIR"])
        reserve = self.iq_disk_reserve_bytes()
        requested_post = POST_FIR_IQ_MAX_BYTES if post_requested else 0
        requested_raw = RAW_IQ_MAX_BYTES if raw_requested else 0
        requested_total = requested_post + requested_raw

        try:
            iq_dir.mkdir(parents=True, exist_ok=True)
            free = int(shutil.disk_usage(iq_dir).free)
        except OSError as exc:
            env.pop("GSS_IQ_RECORD", None)
            env.pop("GSS_IQ_RAW_RECORD", None)
            env.pop("GSS_IQ_MAX_BYTES", None)
            env.pop("GSS_IQ_RAW_MAX_BYTES", None)
            warning = f"IQ recording disabled: capture storage unavailable ({exc})"
            plan = {
                "path": str(iq_dir),
                "requested_bytes": requested_total,
                "allocated_bytes": 0,
                "reserve_bytes": reserve,
                "free_bytes": None,
                "constrained": True,
                "warning": warning,
            }
            with self._state_lock:
                self._capture_storage = plan
            return warning

        available = max(0, free - reserve)
        allocatable = min(requested_total, available)
        if allocatable >= requested_total:
            post_cap = requested_post
            raw_cap = requested_raw
        elif requested_total:
            # Scale both explicitly requested products together. This avoids
            # silently starving one recorder merely because it was allocated
            # second, while preserving the operator's 8:50 cap ratio.
            post_cap = (
                int(allocatable * requested_post / requested_total)
                if post_requested else 0
            )
            raw_cap = allocatable - post_cap if raw_requested else 0
        else:
            post_cap = raw_cap = 0

        if post_cap > 0:
            env["GSS_IQ_MAX_BYTES"] = str(post_cap)
        else:
            env.pop("GSS_IQ_RECORD", None)
            env.pop("GSS_IQ_MAX_BYTES", None)
        if raw_cap > 0:
            env["GSS_IQ_RAW_MAX_BYTES"] = str(raw_cap)
        else:
            env.pop("GSS_IQ_RAW_RECORD", None)
            env.pop("GSS_IQ_RAW_MAX_BYTES", None)

        allocated = post_cap + raw_cap
        constrained = allocated < requested_total
        warning = ""
        if allocated == 0:
            warning = (
                "IQ recording disabled: no capture space remains above the "
                f"{reserve / 1e9:.1f} GB disk reserve"
            )
        elif constrained:
            warning = (
                f"IQ capture caps reduced to {allocated / 1e9:.1f} GB total "
                f"to preserve a {reserve / 1e9:.1f} GB disk reserve"
            )
        plan = {
            "path": str(iq_dir),
            "requested_bytes": requested_total,
            "allocated_bytes": allocated,
            "post_fir_max_bytes": post_cap,
            "raw_max_bytes": raw_cap,
            "reserve_bytes": reserve,
            "free_bytes": free,
            "constrained": constrained,
            "warning": warning,
        }
        with self._state_lock:
            self._capture_storage = plan
        return warning

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
            stream_health = dict(self._stream_health) if self._stream_health else None
            tx_health = dict(self._tx_health) if self._tx_health else None
            capture_storage = (
                dict(self._capture_storage) if self._capture_storage else None
            )

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

        if stream_health is not None:
            now_ms = int(time.time() * 1000)
            report_ms = int(stream_health.get("ts_ms") or 0)
            age_s = max(0.0, (now_ms - report_ms) / 1000.0) if report_ms else 0.0
            stream_health["age_s"] = age_s
            stream_health["stale"] = bool(
                running
                and report_ms
                and age_s > HEALTH_STALE_AFTER_S
            )
        if tx_health is not None:
            now_ms = int(time.time() * 1000)
            report_ms = int(tx_health.get("ts_ms") or 0)
            age_s = max(0.0, (now_ms - report_ms) / 1000.0) if report_ms else 0.0
            tx_health["age_s"] = age_s
            tx_health["stale"] = bool(
                running
                and report_ms
                and age_s > HEALTH_STALE_AFTER_S
            )

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
            "stream_health": stream_health,
            "tx_health": tx_health,
            "capture_storage": capture_storage,
            "log_file": str(self._run_log_path) if self._run_log_path else None,
        }

    def log_snapshot(self) -> list[str]:
        with self._state_lock:
            self._resize_log_if_needed()
            return list(self._log)

    def _append_log(self, line: str) -> None:
        self._publish_log_line(_stamp_log_line(line))

    def _publish_log_line(self, stamped: str) -> None:
        with self._state_lock:
            self._resize_log_if_needed()
            self._log.append(stamped)
        self._schedule_broadcast({"type": "log", "line": stamped})

    # -- persistent per-run stdout log ------------------------------------
    # Everything the Radio page shows also lands in
    # <log_dir>/radio/radio_<mission>_<start>.log (same artifact convention
    # as waterfalls/ and iq/), so flowgraph stdout — decoder database
    # selection, STREAM_HEALTH, UHD chatter, recorder prints — survives
    # restarts and is greppable per pass. Each run's reader/waiter threads
    # own their _RunLog instance, so a superseded process can only ever
    # close ITS OWN file — never the replacement run's. File logging must
    # never take the radio down: every failure path disables it and
    # carries on.

    def _open_run_log(self, cmd: list[str]) -> "_RunLog":
        with self.runtime.cfg_lock:
            platform_cfg = self.runtime.platform_cfg
            general = platform_cfg.get("general") if isinstance(platform_cfg.get("general"), dict) else {}
            log_dir_raw = str(general.get("log_dir", "logs"))
        mission = str(self.runtime.mission_id or "radio")
        try:
            radio_dir = resolve_project_path(log_dir_raw) / "radio"
            radio_dir.mkdir(parents=True, exist_ok=True)
            start = time.gmtime()
            stem = f"radio_{mission}_{time.strftime('%Y%m%dT%H%M%SZ', start)}"
            path = radio_dir / f"{stem}.log"
            # A restart within the same second must not append into the old
            # run's file — each run gets its own.
            counter = 1
            while path.exists():
                counter += 1
                path = radio_dir / f"{stem}_{counter}.log"
            handle = open(path, "a", encoding="utf-8")
            handle.write("# MAVERIC GSS radio stdout log\n")
            handle.write(f"# started: {time.strftime('%Y-%m-%dT%H:%M:%SZ', start)}"
                         f"  mission: {mission}\n")
            handle.write(f"# command: {' '.join(cmd)}\n")
            handle.flush()
            self._run_log_path = path
            return _RunLog(path, handle)
        except Exception as exc:
            logging.warning("radio run log unavailable (%s); continuing without", exc)
            self._run_log_path = None
            return _RunLog(None, None)

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

    def _wait_for_terminal(self, done: threading.Event | None) -> bool:
        if done is None or done.is_set():
            return True
        if done.wait(timeout=TERMINAL_FINALIZE_TIMEOUT_S):
            return True
        self._set_error("previous radio process did not finish terminal cleanup")
        return False

    def _start_locked(self) -> dict[str, Any]:
        # A process may already have exited and cleared self.proc while its
        # terminal broadcasts/callbacks are still running. Do not let a new
        # run enter that gap: the old tracking.disengage callback would then
        # act on the replacement process.
        with self._state_lock:
            previous_proc = self.proc
            previous_done = self._terminal_done
        if previous_proc is not None and previous_proc.poll() is None:
            return self.status()
        if not self._wait_for_terminal(previous_done):
            return self.status()

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
        capture_notice = self._plan_capture_storage(env)
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

        terminal_done = threading.Event()
        with self._state_lock:
            self.proc = proc
            self.started_at = time.time()
            self.last_exit_code = None
            self.last_error = ""
            self.last_stop_expected = False
            self._stopping = False
            self._command_snapshot = list(cmd)
            self._stream_health = None
            self._tx_health = None
            self._terminal_done = terminal_done
            self._resize_log_if_needed()
            self._log.clear()

        run_log = self._open_run_log(list(cmd))
        if capture_notice:
            stamped = _stamp_log_line(capture_notice)
            run_log.write(stamped)
            self._publish_log_line(stamped)

        self._reader_thread = threading.Thread(
            target=self._reader,
            args=(proc, run_log),
            daemon=True,
            name="radio-log",
        )
        self._wait_thread = threading.Thread(
            target=self._waiter,
            args=(proc, run_log, self._reader_thread, terminal_done),
            daemon=True,
            name="radio-wait",
        )
        self._reader_thread.start()
        self._wait_thread.start()
        status = self.status()
        self._write_radio_event("start", status=status, detail=command_text)
        self._schedule_broadcast({"type": "status", "status": status})
        return status

    def _reader(self, proc: subprocess.Popen[str],
                run_log: "_RunLog | None" = None) -> None:
        stream = proc.stdout
        if stream is None:
            return
        try:
            for line in stream:
                stamped = _stamp_log_line(line.rstrip("\n"))
                if run_log is not None:
                    run_log.write(stamped)
                # A superseded run keeps draining into its own file but must
                # stay out of the live surfaces (UI stream, health status).
                if self.proc is proc:
                    self._ingest_stream_health(stamped)
                    self._ingest_tx_health(stamped)
                    self._publish_log_line(stamped)
        except Exception as exc:
            logging.warning("radio stdout reader failed: %s", exc)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _ingest_stream_health(self, line: str) -> None:
        """Parse the flowgraph's structured pre-FIR health reports
        (`STREAM_HEALTH {json}`) into the status surface."""
        marker = line.find("STREAM_HEALTH ")
        if marker < 0:
            return
        try:
            payload = json.loads(line[marker + len("STREAM_HEALTH "):])
        except (ValueError, TypeError):
            return
        if isinstance(payload, dict):
            payload["ts_ms"] = int(time.time() * 1000)
            with self._state_lock:
                self._stream_health = payload
            # Live delivery: the frontend polls only while the websocket is
            # down, so each health report must push a status update itself
            # (the transition-only broadcasts would leave it stale).
            self._schedule_broadcast({"type": "status", "status": self.status()})

    def _ingest_tx_health(self, line: str) -> None:
        """Parse structured UHD async-event totals (`TX_HEALTH {json}`).

        The flowgraph retains UHD fastpath suppression because raw ``U``
        characters have no line boundaries. Its async-metadata monitor emits
        these JSON records instead, preserving underflow/time/sequence counts
        without corrupting stdout or turning them into an RX alarm.
        """
        marker = line.find("TX_HEALTH ")
        if marker < 0:
            return
        try:
            payload = json.loads(line[marker + len("TX_HEALTH "):])
        except (ValueError, TypeError):
            return
        if isinstance(payload, dict):
            payload["ts_ms"] = int(time.time() * 1000)
            with self._state_lock:
                self._tx_health = payload
            self._schedule_broadcast({"type": "status", "status": self.status()})

    def _waiter(self, proc: subprocess.Popen[str],
                run_log: "_RunLog | None" = None,
                reader_thread: threading.Thread | None = None,
                terminal_done: threading.Event | None = None) -> None:
        try:
            code = proc.wait()
            # Let this run's reader drain the pipe tail before the exit trailer
            # closes the file, so the last stdout lines are never lost. Do not
            # time this join out: _stop_locked has its own bounded wait on the
            # terminal event and will abort a restart rather than allowing a
            # stale reader to overlap the replacement run.
            if reader_thread is not None:
                reader_thread.join()
            if run_log is not None:
                run_log.close(f"process exited code={code}")
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
            if not should_log:
                # Superseded by a run installed outside the serialized public
                # actions (primarily a defensive/test path). Keep this exit
                # silent and touch only its own run log.
                return
            status = self.status()
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
        finally:
            # This is deliberately last: start/restart waits for callbacks as
            # well as process reaping, preventing the old run from changing
            # tracking state underneath its replacement.
            if terminal_done is not None:
                terminal_done.set()

    def _stop_locked(self) -> dict[str, Any]:
        already_stopped = False
        with self._state_lock:
            proc = self.proc
            terminal_done = self._terminal_done
            if proc is None or proc.poll() is not None:
                self._stopping = False
                already_stopped = True
            else:
                self._stopping = True
        if already_stopped:
            self._wait_for_terminal(terminal_done)
            return self.status()

        status = self.status()
        self._write_radio_event("stop_requested", status=status, detail="SIGTERM", expected=True)
        self._schedule_broadcast({"type": "status", "status": status})
        code: int | None = None
        try:
            proc.send_signal(signal.SIGTERM)
            code = proc.wait(timeout=self.stop_timeout_s())
        except subprocess.TimeoutExpired:
            self._append_log("Radio process did not exit after SIGTERM; sending SIGKILL")
            try:
                proc.kill()
            except OSError as exc:
                self._set_error(f"radio SIGKILL failed: {exc}")
                self._write_radio_event(
                    "stop_failed",
                    detail=str(exc),
                    expected=True,
                )
            else:
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
        # The waiter is the single owner of terminal state, audit output, and
        # exit callbacks. Wait even when signal/kill raced with a natural
        # exit and raised OSError; if the child is genuinely still alive the
        # bounded terminal wait fails closed and restart will not proceed.
        self._wait_for_terminal(terminal_done)
        return self.status()

    def start(self) -> dict[str, Any]:
        with self._action_lock:
            return self._start_locked()

    def stop(self) -> dict[str, Any]:
        with self._action_lock:
            return self._stop_locked()

    def restart(self) -> dict[str, Any]:
        with self._action_lock:
            stopped = self._stop_locked()
            with self._state_lock:
                terminal_done = self._terminal_done
            if terminal_done is not None and not terminal_done.is_set():
                return stopped
            return self._start_locked()

    def shutdown(self) -> None:
        self.stop()
