"""Runtime TLE-fetch service.

Bridges the pure fetch logic (platform/tracking/fetch.py) to the live runtime
config. `fetch_preview` returns a parsed TLE without persisting (the manual
button fills the editor; the operator Saves). `refresh_and_persist` writes the
validated TLE into platform.tracking.tle and saves gss.yml — but ONLY when the
current method is not "manual" (manual always wins) and config save is enabled.

Author: Irfan Annuar - USC ISI SERC
"""
from __future__ import annotations

import copy
import logging
import os
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable

from mav_gss_lib.config import save_operator_config, split_to_persistable
from mav_gss_lib.platform.config import persist_mission_config
from mav_gss_lib.platform.tracking.fetch import FetchResult, TleFetchSettings, fetch_tle

_LOG = logging.getLogger(__name__)
FetchFn = Callable[..., FetchResult]


def _now_ms() -> int:
    return int(time.time() * 1000)


class TleFetchService:
    def __init__(self, runtime, *, fetch_fn: FetchFn = fetch_tle) -> None:
        self.runtime = runtime
        self._fetch_fn = fetch_fn

    def settings(self) -> TleFetchSettings:
        with self.runtime.cfg_lock:
            tracking = (self.runtime.platform_cfg or {}).get("tracking") or {}
            raw = dict(tracking.get("tle_fetch") or {})
        try:
            interval = max(1.0, float(raw.get("refresh_interval_hours", 12)))
        except (TypeError, ValueError):
            interval = 12.0
        return TleFetchSettings(
            identifier=str(raw.get("identifier", "")),
            auto_refresh=bool(raw.get("auto_refresh", False)),
            refresh_interval_hours=interval,
            provider=str(raw.get("provider", "celestrak")),
        )

    def _run_fetch(self) -> FetchResult:
        return self._fetch_fn(self.settings(), now_ms=_now_ms())

    def fetch_preview(self, identifier: str | None = None) -> dict:
        # Manual preview is returned to the caller directly and deliberately does
        # NOT touch _status, which tracks only the auto-refresh lifecycle.
        # `identifier` lets the UI fetch its unsaved draft (e.g. a
        # Target-satellite selection) without a Save round-trip first;
        # None keeps the saved config identifier.
        settings = self.settings()
        if isinstance(identifier, str) and identifier.strip():
            settings = replace(settings, identifier=identifier.strip())
        result = self._fetch_fn(settings, now_ms=_now_ms())
        return self._as_dict(result)

    def refresh_and_persist(self) -> dict:
        result = self._run_fetch()
        if not result.ok:
            self._status = self._as_dict(result)
            return self._status
        snapshot = None
        locked = False
        with self.runtime.cfg_lock:
            tracking = self.runtime.platform_cfg.setdefault("tracking", {})
            current = tracking.get("tle") or {}
            if current.get("method") == "manual":
                locked = True
            else:
                via = result.via or "fetch"
                tracking["tle"] = {
                    "source": f"{via} (fetched {_iso(result.tle_epoch_ms or _now_ms())})",
                    "name": result.name or current.get("name", ""),
                    "line1": result.line1,
                    "line2": result.line2,
                    "method": "fetched",
                    "fetched_at_ms": _now_ms(),
                }
                snapshot = self._persistable_snapshot()
        if locked:
            self._status = self._as_dict(
                FetchResult(ok=False, via=result.via,
                            detail="manual TLE locked — auto-refresh skipped"))
            return self._status
        self._save(snapshot)
        self._status = self._as_dict(result)
        return self._status

    def status(self) -> dict:
        return getattr(self, "_status", self._as_dict(FetchResult(ok=False, detail="no fetch yet")))

    @staticmethod
    def spacetrack_env_status() -> dict:
        """Non-secret booleans: are the Space-Track env creds present? (values never exposed)."""
        return {
            "identity_set": bool(os.environ.get("SPACETRACK_IDENTITY")),
            "password_set": bool(os.environ.get("SPACETRACK_PASSWORD")),
        }

    def _persistable_snapshot(self):
        mission_persistable = persist_mission_config(
            self.runtime.mission_cfg, self.runtime.mission.config,
        ) if getattr(self.runtime, "mission", None) is not None else copy.deepcopy(self.runtime.mission_cfg)
        return split_to_persistable(
            self.runtime.platform_cfg, self.runtime.mission_id, mission_persistable,
        )

    def _save(self, snapshot) -> None:
        if getattr(self.runtime, "config_save_disabled", False):
            return
        try:
            save_operator_config(snapshot)
        except Exception:
            _LOG.exception("tle fetch config save failed")

    @staticmethod
    def _as_dict(result: FetchResult) -> dict:
        age_days = None
        if result.tle_epoch_ms:
            age_days = round(abs(_now_ms() - result.tle_epoch_ms) / 86_400_000.0, 2)
        return {
            "ok": result.ok,
            "detail": result.detail,
            "via": result.via,
            "name": result.name,
            "line1": result.line1,
            "line2": result.line2,
            "tle_epoch_ms": result.tle_epoch_ms,
            "age_days": age_days,
            "candidates": list(result.candidates),
            "ts_ms": _now_ms(),
        }


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


__all__ = ["TleFetchService"]
