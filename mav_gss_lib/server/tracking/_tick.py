"""Single 1 Hz tick loop for Doppler. Drives tracking.doppler() once per
tick, then fans out the result to in-process WebSocket subscribers. ZMQ
delivery to the flowgraph happens inside tracking.doppler() via the active
sink, so this loop only manages the WS fan-out explicitly."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, AsyncIterator

from mav_gss_lib.config import get_tracking_control

if TYPE_CHECKING:
    from mav_gss_lib.server.state import WebRuntime


_LOG = logging.getLogger(__name__)


class DopplerBroadcaster:
    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[dict]] = []
        self._lock = asyncio.Lock()
        self._latest: dict | None = None

    @property
    def latest(self) -> dict | None:
        return self._latest

    async def subscribe(self) -> AsyncIterator[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=8)
        async with self._lock:
            self._queues.append(queue)
            if self._latest is not None:
                queue.put_nowait(self._latest)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                if queue in self._queues:
                    self._queues.remove(queue)

    async def publish(self, message: dict) -> None:
        if message.get("type") == "doppler":
            self._latest = message
        async with self._lock:
            queues = list(self._queues)
        for queue in queues:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass


async def doppler_tick_loop(
    runtime: "WebRuntime",
    broadcaster: DopplerBroadcaster,
    *,
    period_s_override: float | None = None,
) -> None:
    while True:
        try:
            correction = await asyncio.to_thread(runtime.tracking.doppler)
            # Re-stamp mode at publish time. The tick reads `_doppler_mode`
            # at the start of the Skyfield computation; if a disengage HTTP
            # broadcast lands during that window, the in-flight tick would
            # otherwise publish a stale `connected` frame after it.
            correction["mode"] = runtime.tracking.doppler_mode
            await broadcaster.publish({
                "type": "doppler",
                "doppler": correction,
                "ts_ms": int(time.time() * 1000),
            })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOG.warning("doppler tick failed: %s", exc)
            await broadcaster.publish({
                "type": "error",
                "error": str(exc),
            })
        # Re-read each iteration so /api/config edits take effect without restart.
        period_s = period_s_override if period_s_override is not None else _resolve_period(runtime)
        await asyncio.sleep(period_s)


def _resolve_period(runtime: "WebRuntime") -> float:
    with runtime.cfg_lock:
        control = get_tracking_control(runtime.platform_cfg or {})
    return max(0.1, control["tick_period_s"])


__all__ = ["DopplerBroadcaster", "doppler_tick_loop"]
