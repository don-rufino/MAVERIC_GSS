"""
mav_gss_lib.server.app -- FastAPI App Assembly

Creates the FastAPI application, wires in the shared runtime, mounts
the built frontend assets, and manages backend startup/shutdown tasks
such as queue restore, logging setup, TX socket initialization, and RX
broadcast lifetime.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from .state import WebRuntime

from mav_gss_lib.config import get_rx_zmq_addr, get_tx_zmq_addr
from mav_gss_lib.logging import SessionLog

from .api import router as api_router
from .ws.rx import router as rx_router
from .ws.radio import router as radio_router
from .tx.queue import sanitize_queue_items
from .ws.session import router as session_router
from .ws.preflight import (
    router as preflight_router,
    run_preflight_and_broadcast,
)
from .ws.update import schedule_update_check
from .ws.tx import router as tx_router
from .ws.alarms import router as alarms_router
from .api.parameters import router as parameters_router
from .api.mission import router as mission_router
from mav_gss_lib.transport import PUB_STATUS, zmq_cleanup
from mav_gss_lib.platform.alarms.dispatch import make_dispatch
from mav_gss_lib.platform.alarms.evaluators.container import evaluate_containers
from mav_gss_lib.platform.alarms.evaluators.platform import (
    PlatformAlarmInputs, evaluate_platform,
)
from mav_gss_lib.platform.alarms.setup import build_alarm_environment
from mav_gss_lib.server.api.tracking_ws import register_tracking_ws
from mav_gss_lib.server.tracking._fetch_loop import tle_fetch_loop
from mav_gss_lib.server.tracking._tick import DopplerBroadcaster, doppler_tick_loop
from mav_gss_lib.server.ws.alarms import WebRuntimeBroadcastTarget
from .state import WEB_DIR, create_runtime, get_runtime
from ._task_utils import log_task_exception


# =============================================================================
#  APP LIFESPAN
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI) -> "AsyncIterator[None]":
    """Manage backend startup and shutdown for one FastAPI app instance."""
    runtime = get_runtime(app)
    runtime.tx.queue, skipped = sanitize_queue_items(runtime.tx.load_queue(), runtime=runtime)
    runtime.tx.renumber_queue()
    if skipped:
        logging.warning("Dropped %d invalid queued item(s) during startup restore", skipped)

    import time as _time
    pending_instances_path = Path(runtime.log_dir) / ".pending_instances.jsonl"
    if runtime.tx_verifiers_enabled:
        runtime.platform.restore_verifiers(
            path=str(pending_instances_path),
            now_ms=int(_time.time() * 1000),
        )
    else:
        from mav_gss_lib.platform.tx.verifiers import write_instances
        runtime.platform.verifiers.clear_open()
        if pending_instances_path.exists():
            try:
                write_instances(pending_instances_path, [])
            except Exception as exc:
                logging.warning("pending_instances startup clear failed: %s", exc)

    tx_addr = get_tx_zmq_addr(runtime.platform_cfg)
    runtime.tx.restart_pub(tx_addr)
    runtime.radio.bind_loop(asyncio.get_running_loop())
    runtime._doppler_tick_task = asyncio.create_task(
        doppler_tick_loop(runtime, runtime.doppler_broadcaster),
        name="doppler-tick",
    )
    runtime._doppler_tick_task.add_done_callback(log_task_exception("doppler-tick"))
    runtime._tle_fetch_task = asyncio.create_task(
        tle_fetch_loop(runtime), name="tle-fetch",
    )
    runtime._tle_fetch_task.add_done_callback(log_task_exception("tle-fetch"))

    rx_addr = get_rx_zmq_addr(runtime.platform_cfg)
    runtime.rx.log = SessionLog(
        runtime.log_dir, rx_addr, runtime.version,
        mission_name=runtime.mission_name, mission_id=runtime.mission_id,
        station=runtime.station, operator=runtime.operator, host=runtime.host,
    )
    runtime.tx.log = runtime.rx.log
    runtime.rx.open_journal(runtime.rx.log.session_id)
    print(f"Session logging → {runtime.rx.log.jsonl_path}")

    env = build_alarm_environment(
        runtime.mission.spec_root, runtime.alarm_registry,
        runtime.rx.last_arrival_ms,
        now_ms=int(time.time() * 1000),
        mission_alarm_plugins=getattr(runtime.mission, "alarm_plugins", {}),
    )
    loop = asyncio.get_running_loop()

    # Audit-sink adapter: write_alarm on the RX session log.
    class _RxLogAuditSink:
        def __init__(self, runtime): self._runtime = runtime
        def write_alarm(self, change, ts_ms):
            log = getattr(self._runtime.rx, "log", None)
            if log is None:
                return
            try:
                log.write_alarm(change, ts_ms=ts_ms)
            except Exception:
                logging.exception("write_alarm")

    dispatch = make_dispatch(
        audit_sink=_RxLogAuditSink(runtime),
        broadcast_target=WebRuntimeBroadcastTarget(runtime),
        loop=loop,
    )
    runtime.bind_alarm_dispatch(dispatch, env.parameter_rules, env.plugins)
    runtime._alarm_tick_task = asyncio.create_task(
        _alarm_tick_loop(runtime, env.container_specs)
    )

    runtime.rx.start_receiver()
    runtime.rx.broadcast_task = asyncio.create_task(runtime.rx.broadcast_loop())
    runtime.rx.broadcast_task.add_done_callback(log_task_exception("rx-broadcast"))
    if runtime.radio.autostart():
        runtime.radio.start()

    # Kick off the update check on a worker thread so it overlaps with the
    # mission checks streaming over the preflight WS. Resolved at the end of
    # run_preflight_and_broadcast via _build_updates_event.
    schedule_update_check(runtime)

    # Schedule preflight to run AFTER server starts serving.
    # create_task() queues the coroutine; it executes once lifespan yields
    # and uvicorn begins accepting connections.
    runtime.preflight_task = asyncio.create_task(run_preflight_and_broadcast(runtime))
    runtime.preflight_task.add_done_callback(log_task_exception("preflight"))

    # Periodic verifier sweep — drives window_expired + timed_out transitions
    # when no RX traffic is arriving (e.g., no flight connected during ground
    # testing). Without this, stage stays 'released' forever and the UI
    # never turns red on timeout.
    runtime.verifier_sweep_task = asyncio.create_task(_verifier_sweep_loop(runtime))
    runtime.verifier_sweep_task.add_done_callback(log_task_exception("verifier-sweep"))
    yield

    await _shutdown_runtime(runtime)


async def _shutdown_runtime(runtime: "WebRuntime") -> None:
    """Tear down RX/TX/preflight state during lifespan shutdown."""
    runtime.rx.broadcast_stop = True
    runtime.rx.stop.set()
    if runtime.rx.thread_handle:
        runtime.rx.thread_handle.join(timeout=1.0)
    if runtime.rx.broadcast_task:
        try:
            await asyncio.wait_for(runtime.rx.broadcast_task, timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    preflight_task = getattr(runtime, "preflight_task", None)
    if preflight_task and not preflight_task.done():
        preflight_task.cancel()
        try:
            await asyncio.wait_for(preflight_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    sweep_task = getattr(runtime, "verifier_sweep_task", None)
    if sweep_task and not sweep_task.done():
        sweep_task.cancel()
        try:
            await asyncio.wait_for(sweep_task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    tick = getattr(runtime, "_alarm_tick_task", None)
    if tick is not None:
        tick.cancel()
        try:
            await tick
        except (asyncio.CancelledError, Exception):
            pass
    doppler_task = getattr(runtime, "_doppler_tick_task", None)
    if doppler_task is not None:
        doppler_task.cancel()
        try:
            await doppler_task
        except (asyncio.CancelledError, Exception):
            pass
    fetch_task = getattr(runtime, "_tle_fetch_task", None)
    if fetch_task is not None:
        fetch_task.cancel()
        try:
            await fetch_task
        except (asyncio.CancelledError, Exception):
            pass
    try:
        runtime.tracking.disengage()
    except Exception:
        logging.exception("tracking disengage")
    try:
        runtime.parameter_cache.flush()
    except Exception:
        logging.exception("parameter cache flush")
    try:
        runtime.rx.close_journal()
    except Exception:
        logging.exception("rx journal close")
    if runtime.rx.log:
        try:
            runtime.rx.log.close()
        except Exception:
            pass
    if runtime.tx.log:
        try:
            runtime.tx.log.close()
        except Exception:
            pass
    if runtime.tx.zmq_sock:
        try:
            zmq_cleanup(runtime.tx.zmq_monitor, PUB_STATUS, "OFFLINE", runtime.tx.zmq_sock, runtime.tx.zmq_ctx)
        except Exception:
            pass
    try:
        await asyncio.to_thread(runtime.radio.shutdown)
    except Exception:
        logging.exception("radio shutdown")


async def _verifier_sweep_loop(runtime: "WebRuntime") -> None:
    """Fire the verifier registry's sweep() once per second.

    Each tick:
      1. sweep() — mark expired-window verifiers as window_expired.
      2. consume_dirty() — broadcast every changed instance over /ws/tx so
         the UI sees the transition (including the final stage).
      3. finalize_settled() — pop instances where is_settled(inst). Emit
         one closing cmd_verifier record per instance and rewrite
         .pending_instances.jsonl.
    """
    import time as _time
    from pathlib import Path
    from mav_gss_lib.platform.tx.verifiers import write_instances

    # Stage-derived outcome string. Internal verifier outcomes can disagree
    # with the instance's overall stage (e.g. NACK windows may have
    # window_expired on a successful command), so the closing record
    # reports the *instance* outcome — the value the operator cares about.
    stage_outcome = {
        "complete":  "passed",
        "failed":    "failed",
        "timed_out": "window_expired",
    }

    while True:
        try:
            await asyncio.sleep(1.0)
            now_ms = int(_time.time() * 1000)
            registry = runtime.platform.verifiers

            registry.sweep(now_ms=now_ms)

            # Step 2: collect + broadcast dirty (transitions matter for UI).
            # Do this BEFORE finalize so the UI sees the terminal stage on
            # the row while it still exists in the registry. Capture the
            # list — we also use it to decide whether to persist below.
            dirty = list(registry.consume_dirty())
            for inst in dirty:
                asyncio.create_task(runtime.tx.broadcast_verifier_instance(inst))

            # Step 3: drain settled instances, emit one closing record each.
            finalized = registry.finalize_settled()
            tx_log = getattr(getattr(runtime, "tx", None), "log", None)
            for inst in finalized:
                if tx_log is None:
                    continue
                # Pick a representative match_event_id: the verifier that
                # decided the stage (passed-complete or passed-failed). Falls
                # back to None for pure timeouts.
                deciding = None
                if inst.stage == "complete":
                    for spec in inst.verifier_set.verifiers:
                        if spec.stage == "complete":
                            o = inst.outcomes.get(spec.verifier_id)
                            if o is not None and o.state == "passed":
                                deciding = o
                                break
                elif inst.stage == "failed":
                    for spec in inst.verifier_set.verifiers:
                        if spec.stage == "failed":
                            o = inst.outcomes.get(spec.verifier_id)
                            if o is not None and o.state == "passed":
                                deciding = o
                                break
                elapsed_ms = (deciding.matched_at_ms - inst.t0_ms) if deciding and deciding.matched_at_ms else (now_ms - inst.t0_ms)
                match_event_id = deciding.match_event_id if deciding else None
                try:
                    tx_log.write_cmd_verifier({
                        "seq": 0,  # closing record is out-of-band w.r.t. RX seq
                        "cmd_event_id": inst.cmd_event_id,
                        "instance_id": inst.instance_id,
                        "stage": inst.stage,        # complete | failed | timed_out
                        "verifier_id": "",          # closing record (no specific verifier)
                        "outcome": stage_outcome.get(inst.stage, "unknown"),
                        "elapsed_ms": elapsed_ms,
                        "match_event_id": match_event_id,
                    })
                except Exception as exc:
                    logging.warning("cmd_verifier finalize log failed: %s", exc)

            # Persist the open-instances snapshot whenever something
            # changed: dirty transitions OR settled drains. The previous
            # design persisted only on settle, which lost in-flight
            # transitions across crashes (e.g., complete-but-NACK-pending
            # instances would not survive a restart unless they later
            # settled).
            if dirty or finalized:
                try:
                    write_instances(
                        Path(runtime.log_dir) / ".pending_instances.jsonl",
                        registry.open_instances(),
                    )
                except Exception as exc:
                    logging.warning("pending_instances persist failed: %s", exc)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.warning("verifier sweep tick failed: %s", exc)


# =============================================================================
#  ALARM TICK
# =============================================================================

RADIO_ZMQ_STARTUP_GRACE_MS = 5_000


def _tick_once(runtime, container_specs, now_ms):
    """One tick of the platform + container evaluators. Pure-ish: takes
    the runtime as a state container, returns nothing, side-effects
    through ``runtime._alarm_dispatch``."""
    dispatch = runtime._alarm_dispatch
    if dispatch is None:
        return
    silence_s = (
        max(0.0, time.time() - runtime.rx.last_rx_at)
        if runtime.rx.last_rx_at > 0 else 0.0
    )
    radio_status = runtime.radio.status()
    radio_enabled = bool(radio_status.get("enabled"))
    radio_state = str(radio_status.get("state") or "")
    radio_running = radio_state.lower() == "running"
    started_at_ms = radio_status.get("started_at_ms")
    try:
        # GNU Radio can take a tick or two to bind/publish after Popen
        # succeeds. Suppress that startup-only RETRY blip, but let sustained
        # RX ZMQ failures alarm once the grace window has elapsed.
        radio_started_recently = (
            radio_running
            and started_at_ms is not None
            and now_ms - int(started_at_ms) < RADIO_ZMQ_STARTUP_GRACE_MS
        )
    except (TypeError, ValueError):
        radio_started_recently = False
    inputs = PlatformAlarmInputs(
        silence_s=silence_s,
        zmq_state=runtime.rx.status.get(),
        rx_zmq_expected=not radio_enabled or (
            radio_running and not radio_started_recently
        ),
        crc_event_ms=tuple(runtime.rx.crc_window),
        dup_event_ms=tuple(runtime.rx.dup_window),
        radio_enabled=radio_enabled,
        radio_autostart=bool(radio_status.get("autostart")),
        radio_state=radio_state,
    )
    for v in evaluate_platform(inputs, now_ms):
        dispatch.emit(runtime.alarm_registry.observe(v, now_ms), now_ms)
    for v in evaluate_containers(
        container_specs, runtime.rx.last_arrival_ms, now_ms,
    ):
        dispatch.emit(runtime.alarm_registry.observe(v, now_ms), now_ms)


async def _alarm_tick_loop(runtime, container_specs):
    while True:
        await asyncio.sleep(1.0)
        try:
            _tick_once(runtime, container_specs, int(time.time() * 1000))
        except Exception:
            logging.exception("alarm tick loop")


# =============================================================================
#  APP FACTORY
# =============================================================================

def create_app() -> FastAPI:
    """Create the configured FastAPI application and attach a WebRuntime."""
    runtime = create_runtime()
    app = FastAPI(title=f"{runtime.mission_name} GSS Web", lifespan=lifespan)
    app.state.runtime = runtime
    runtime.doppler_broadcaster = DopplerBroadcaster()
    register_tracking_ws(
        app,
        get_broadcaster=lambda: runtime.doppler_broadcaster,
        get_tracking=lambda: runtime.tracking,
    )

    if WEB_DIR.exists() and (WEB_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")

    app.include_router(api_router)
    app.include_router(rx_router)
    app.include_router(radio_router)
    app.include_router(tx_router)
    app.include_router(session_router)
    app.include_router(preflight_router)
    app.include_router(alarms_router)
    app.include_router(parameters_router)
    app.include_router(mission_router)

    if runtime.mission.http is not None:
        for router in runtime.mission.http.routers:
            app.include_router(router)
            logging.info("Mounted mission router: %s", router.prefix)

    if WEB_DIR.exists():

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str) -> FileResponse:
            file_path = WEB_DIR / full_path
            if file_path.is_file():
                return FileResponse(file_path)
            return FileResponse(
                WEB_DIR / "index.html",
                headers={"Cache-Control": "no-cache"},
            )

    return app
