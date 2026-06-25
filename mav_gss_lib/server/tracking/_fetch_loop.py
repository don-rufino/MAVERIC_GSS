"""Optional auto-refresh loop for TLE fetch.

Mirrors the doppler tick: a lifespan-managed asyncio task that re-reads live
config each cycle (so toggling auto_refresh/interval needs no restart), runs the
blocking fetch off the event loop via to_thread with a hard timeout, and warns-
and-continues on error. Fetch is OFF by default; the loop is inert until an
operator enables auto_refresh AND sets an identifier.

Author: Irfan Annuar - USC ISI SERC
"""
from __future__ import annotations

import asyncio
import logging

_LOG = logging.getLogger(__name__)

_POLL_S = 60.0                 # how often we check whether a refresh is due
_FETCH_HARD_TIMEOUT_S = 30.0   # ceiling on a single refresh attempt


async def tle_fetch_loop(runtime, *, poll_s_override: float | None = None) -> None:
    last_fetch_ms = 0.0
    import time as _time
    while True:
        try:
            svc = getattr(runtime, "tle_fetch", None)
            settings = svc.settings() if svc is not None else None
            due = False
            if settings is not None and settings.auto_refresh and settings.identifier.strip():
                now_ms = _time.time() * 1000
                interval_ms = settings.refresh_interval_hours * 3_600_000.0
                due = (last_fetch_ms == 0.0) or (now_ms - last_fetch_ms >= interval_ms)
            if due:
                last_fetch_ms = _time.time() * 1000
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(svc.refresh_and_persist),
                        timeout=_FETCH_HARD_TIMEOUT_S,
                    )
                    if result.get("ok"):
                        _LOG.info("TLE auto-refresh ok via %s", result.get("via"))
                    else:
                        _LOG.info("TLE auto-refresh: %s", result.get("detail"))
                except asyncio.TimeoutError:
                    _LOG.warning("TLE auto-refresh timed out")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.warning("tle fetch loop tick failed: %s", exc)
        await asyncio.sleep(poll_s_override if poll_s_override is not None else _POLL_S)


__all__ = ["tle_fetch_loop"]
