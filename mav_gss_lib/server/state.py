"""
mav_gss_lib.server.state -- Web Runtime State Container

Owns the long-lived mutable backend state used by the FastAPI app:
active config, protocol objects, mission spec, RX/TX services, and
queue/logging limits.

This module is the construction point for the web runtime and the
shared state accessors used across API routes, websocket handlers,
and background services.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Optional

if TYPE_CHECKING:
    from fastapi import FastAPI, Request, WebSocket

from mav_gss_lib.config import (
    get_generated_commands_dir,
    load_split_config,
)
from mav_gss_lib.constants import DEFAULT_MISSION_NAME
from mav_gss_lib.identity import capture_host, capture_operator, capture_station
from mav_gss_lib.platform import PlatformRuntime
from mav_gss_lib.platform.alarms.dispatch import AlarmDispatch
from mav_gss_lib.platform.alarms.evaluators.parameter import (
    PluginRegistry, evaluate_parameter,
)
from mav_gss_lib.platform.alarms.registry import AlarmRegistry
from mav_gss_lib.platform.alarms.schema import AlarmRule
from mav_gss_lib.platform.parameter_cache import ParameterCache
from ._atomics import AtomicStatus
from .radio import RadioService
from .rx.service import RxService
from .tracking.service import TrackingService
from .tx.service import TxService

if TYPE_CHECKING:
    from mav_gss_lib.updater import UpdateStatus

WEB_DIR = Path(__file__).resolve().parents[1] / "web" / "dist"
HOST = "127.0.0.1"
PORT = 8080
MAX_PACKETS = 500
MAX_HISTORY = 500
MAX_QUEUE = 200


@dataclass
class Session:
    session_id: str
    session_tag: str
    started_at: str
    session_generation: int
    operator: str
    host: str
    station: str


class WebRuntime:
    """Own mutable backend state for one FastAPI app instance."""

    def __init__(self) -> None:
        self.session_token = secrets.token_urlsafe(24)
        self.max_packets = MAX_PACKETS
        self.max_history = MAX_HISTORY
        self.max_queue = MAX_QUEUE

        # Split runtime state is primary. `platform_cfg` and `mission_cfg` are
        # the authoritative live state; `mission_id` is the active mission.
        # `mission_id` is NOT mirrored into `platform_cfg["general"]` — that
        # would be a platform/mission boundary leak.
        # Split runtime state is primary. Mission defaults (nodes, ptypes,
        # mission-declared RX/TX params, ax25/csp placeholders, ui titles) are
        # seeded by the mission's own `build(ctx)` inside `from_split(...)` —
        # operator values in gss.yml win. The platform does not merge mission
        # YAML any more; missions own their defaults in code.
        self.platform_cfg, self.mission_id, self.mission_cfg = load_split_config()

        # Ephemeral mode: redirect every disk-write path to a tempdir
        # BEFORE the platform runtime resolves log_dir / data_dir, and
        # block the gss.yml / mission.yml save-back. Operator opt-in via
        # GSS_EPHEMERAL=1 or `MAV_WEB.py --ephemeral`.
        from mav_gss_lib.server import ephemeral as _ephemeral
        self.config_save_disabled: bool = False
        self._ephemeral_root = None
        self._ephemeral_cleanup = None
        if _ephemeral.is_active():
            self._ephemeral_root, self._ephemeral_cleanup = _ephemeral.apply(
                self.platform_cfg
            )
            self.config_save_disabled = True
            print(
                f"EPHEMERAL MODE: writes redirected to {self._ephemeral_root}/, "
                f"config save-back disabled"
            )

        self.operator = capture_operator()
        self.host = capture_host()
        self.station = capture_station(self.platform_cfg, self.host)

        self.alarm_registry = AlarmRegistry()
        self.alarm_clients: list = []
        self.alarm_clients_lock = threading.Lock()
        # Live until lifespan calls bind_alarm_dispatch — keeps cache writes
        # silent (no audit, no broadcast) during construction.
        self._alarm_dispatch: AlarmDispatch | None = None
        self._parameter_rules: dict[str, tuple[AlarmRule, ...]] = {}
        self._alarm_plugins: PluginRegistry = PluginRegistry({})

        self.platform = PlatformRuntime.from_split(
            self.platform_cfg, self.mission_id, self.mission_cfg,
            on_parameter_apply=self._evaluate_parameter_alarms,
        )
        self.mission = self.platform.mission
        # Populated from MissionSpec.parse_warnings so the /ws/preflight
        # payload can surface non-fatal spec authoring warnings at startup.
        self.parse_warnings: tuple = tuple(
            getattr(self.mission, "parse_warnings", ())
        )

        self.tx_status = AtomicStatus()

        # tx_blackout_until: cross-thread float read/write. CPython float assign
        # is GIL-atomic; writer (TxService send loop) and reader (RX drop filter)
        # see a coherent value without a lock. Seconds since epoch.
        self.tx_blackout_until: float = 0.0

        self.shutdown_task = None
        self.had_clients = False
        self.cfg_lock = threading.Lock()
        self.session = Session(
            session_id=uuid.uuid4().hex,
            session_tag="untitled",
            started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            session_generation=1,
            operator=self.operator,
            host=self.host,
            station=self.station,
        )
        self.session_clients: list = []
        self.session_lock = threading.Lock()

        # Preflight state
        self.preflight_results: list[dict] = []
        self.preflight_done: bool = False
        self.preflight_running: bool = False   # single-run guard
        self.preflight_task = None             # asyncio.Task reference
        self.preflight_clients: list = []
        self.preflight_lock = threading.Lock()

        # Updater state — populated at lifespan start via schedule_update_check
        self.update_status_future: Optional[asyncio.Future] = None
        self.update_status: Optional["UpdateStatus"] = None
        self.update_lock = threading.Lock()
        self.update_in_progress: bool = False
        # True after the first successful preflight; gates concurrent update checks.
        self.preflight_completed: bool = False

        self.rx = RxService(self)
        self.tx = TxService(self)
        self.radio = RadioService(self)
        self.tracking = TrackingService(self)
        self.radio.add_exit_callback(self.tracking.disengage)
        self.doppler_broadcaster = None  # set by create_app at startup

    def queue_file(self) -> Path:
        return Path(self.log_dir) / ".pending_queue.jsonl"

    def generated_commands_dir(self) -> Path:
        return get_generated_commands_dir(self.platform_cfg)

    # --- typed split-state accessors (primary read API) ---

    @property
    def log_dir(self) -> str:
        general = self.platform_cfg.get("general") or {}
        return str(general.get("log_dir", "logs"))

    @property
    def version(self) -> str:
        general = self.platform_cfg.get("general") or {}
        return str(general.get("version", ""))

    @property
    def build_sha(self) -> str:
        general = self.platform_cfg.get("general") or {}
        return str(general.get("build_sha", ""))

    @property
    def mission_name(self) -> str:
        name = self.mission_cfg.get("mission_name") if isinstance(self.mission_cfg, dict) else None
        return str(name) if name else DEFAULT_MISSION_NAME

    @property
    def frame_label(self) -> str:
        """The active mission's declared uplink frame label.

        Sourced from `mission.framing.uplink_label` so the field reflects
        what the framer chain actually emits, not an operator-selectable
        mode. Falls back to "raw" only if a mission ships without a
        framing block (which DeclarativeFramer construction will refuse,
        so this is defensive).
        """
        spec_root = getattr(self.mission, "spec_root", None)
        framing = getattr(spec_root, "framing", None) if spec_root is not None else None
        return getattr(framing, "uplink_label", None) or "raw"

    @property
    def rx_frequency(self) -> str:
        rx = self.platform_cfg.get("rx") or {}
        return str(rx.get("frequency", ""))

    @property
    def tx_frequency(self) -> str:
        tx = self.platform_cfg.get("tx") or {}
        return str(tx.get("frequency", ""))

    @property
    def tx_delay_ms(self) -> int:
        tx = self.platform_cfg.get("tx") or {}
        return int(tx.get("delay_ms", 500))

    @property
    def tx_blackout_ms(self) -> int:
        rx = self.platform_cfg.get("rx") or {}
        return int(rx.get("tx_blackout_ms", 0) or 0)

    @property
    def parameter_cache(self) -> ParameterCache:
        return self.platform.parameter_cache

    def bind_alarm_dispatch(
        self,
        dispatch: AlarmDispatch,
        parameter_rules: Mapping[str, tuple[AlarmRule, ...]],
        plugins: PluginRegistry,
    ) -> None:
        """Install the live alarm dispatch + compiled parameter rules.

        Called by lifespan once the spec_root, dispatch sinks, and event
        loop are all available.
        """
        self._alarm_dispatch = dispatch
        self._parameter_rules = dict(parameter_rules)
        self._alarm_plugins = plugins

    def _evaluate_parameter_alarms(self, updates) -> None:
        """ParameterCache.on_apply callback — drives parameter rule
        evaluation and dispatches changes. No-op until bind_alarm_dispatch
        has been called."""
        dispatch = self._alarm_dispatch
        if dispatch is None:
            return
        now_ms = int(time.time() * 1000)
        for u in updates:
            if self.alarm_registry.carrier_stale_for(u.name):
                continue
            rules = self._parameter_rules.get(u.name)
            if not rules:
                continue
            base = f"param.{u.name}"
            for verdict in evaluate_parameter(
                base, rules, u.value, plugins=self._alarm_plugins,
            ):
                ch = self.alarm_registry.observe(verdict, now_ms)
                dispatch.emit(ch, now_ms)


def create_runtime() -> WebRuntime:
    """Create a fresh WebRuntime with loaded config and initialized services."""
    return WebRuntime()


def ensure_runtime(runtime: WebRuntime | None) -> WebRuntime:
    """Return *runtime* if provided, otherwise create a new runtime."""
    return runtime if runtime is not None else create_runtime()


def get_runtime(holder: "FastAPI | Request | WebSocket") -> WebRuntime:
    """Extract the attached WebRuntime from a FastAPI app/request/websocket."""
    for candidate in (holder, getattr(holder, "app", None)):
        if candidate is not None \
           and hasattr(candidate, "state") \
           and hasattr(candidate.state, "runtime"):
            return candidate.state.runtime
    raise RuntimeError(
        f"web runtime is not attached to app.state (holder={type(holder).__name__})"
    )
