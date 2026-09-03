import asyncio
import threading
import unittest
from unittest.mock import MagicMock

from mav_gss_lib.server.tracking._tick import DopplerBroadcaster, _cadence_due, doppler_tick_loop


class _Runtime:
    def __init__(self, *, log_cadence: str = "tick", log_decimation_s: float = 5.0) -> None:
        self.platform_cfg = {"tracking": {"control": {
            "rx_zmq_addr": "tcp://127.0.0.1:0",
            "tx_zmq_addr": "tcp://127.0.0.1:0",
            "tick_period_s": 0.05,
            "log_cadence": log_cadence,
            "log_decimation_s": log_decimation_s,
        }}}
        self.cfg_lock = threading.Lock()
        self.tracking = MagicMock()
        self.tracking.doppler_mode = "connected"
        self.tracking.doppler.return_value = {"mode": "connected", "rx_tune_hz": 1.0}
        self.tracking.last_error = ""
        self.rx = MagicMock()
        self.tx = MagicMock()


class DopplerTickLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_invokes_doppler_each_tick(self) -> None:
        runtime = _Runtime()
        broadcaster = DopplerBroadcaster()
        task = asyncio.create_task(
            doppler_tick_loop(runtime, broadcaster, period_s_override=0.05)
        )
        await asyncio.sleep(0.18)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertGreaterEqual(runtime.tracking.doppler.call_count, 2)

    async def test_loop_swallows_errors_and_keeps_running(self) -> None:
        runtime = _Runtime()
        runtime.tracking.doppler.side_effect = [
            RuntimeError("boom"),
            {"mode": "connected"},
            {"mode": "connected"},
        ]
        broadcaster = DopplerBroadcaster()
        task = asyncio.create_task(
            doppler_tick_loop(runtime, broadcaster, period_s_override=0.05)
        )
        await asyncio.sleep(0.18)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertGreaterEqual(runtime.tracking.doppler.call_count, 2)

    async def test_loop_broadcasts_doppler_messages(self) -> None:
        runtime = _Runtime()
        broadcaster = DopplerBroadcaster()
        received: list[dict] = []

        async def consumer() -> None:
            async for msg in broadcaster.subscribe():
                received.append(msg)
                if any(m.get("type") == "doppler" for m in received):
                    return

        consumer_task = asyncio.create_task(consumer())
        loop_task = asyncio.create_task(
            doppler_tick_loop(runtime, broadcaster, period_s_override=0.05)
        )
        try:
            await asyncio.wait_for(consumer_task, timeout=1.0)
        finally:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
        self.assertTrue(any(m.get("type") == "doppler" for m in received))

    async def test_loop_logs_tracking_sample_every_tick_in_tick_mode(self) -> None:
        runtime = _Runtime(log_cadence="tick")
        broadcaster = DopplerBroadcaster()
        task = asyncio.create_task(
            doppler_tick_loop(runtime, broadcaster, period_s_override=0.05)
        )
        await asyncio.sleep(0.18)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # "tick" cadence logs every call, so the sample count should track
        # the doppler() call count, not lag behind it via decimation.
        self.assertGreaterEqual(runtime.rx.log.write_tracking_sample.call_count, 2)
        _, kwargs = runtime.rx.log.write_tracking_sample.call_args
        self.assertEqual(kwargs["source"], "tick")

    async def test_loop_throttles_tracking_sample_in_default_cadence(self) -> None:
        runtime = _Runtime(log_cadence="tx_throttled", log_decimation_s=1.0)
        broadcaster = DopplerBroadcaster()
        task = asyncio.create_task(
            doppler_tick_loop(runtime, broadcaster, period_s_override=0.05)
        )
        # ~5 ticks at 0.05s each, well under the 1.0s decimation interval.
        await asyncio.sleep(0.22)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Only the first tick should have logged; the rest are throttled.
        self.assertEqual(runtime.rx.log.write_tracking_sample.call_count, 1)


class CadenceDueTests(unittest.TestCase):
    def test_tick_cadence_always_due(self) -> None:
        control = {"log_cadence": "tick", "log_decimation_s": 5.0}
        self.assertTrue(_cadence_due(control, last_logged_ms=1_000, now_ms=1_001))

    def test_throttled_cadence_respects_first_call(self) -> None:
        control = {"log_cadence": "tx_throttled", "log_decimation_s": 5.0}
        self.assertTrue(_cadence_due(control, last_logged_ms=None, now_ms=1_000))

    def test_throttled_cadence_waits_for_interval(self) -> None:
        control = {"log_cadence": "tx_throttled", "log_decimation_s": 5.0}
        self.assertFalse(_cadence_due(control, last_logged_ms=1_000, now_ms=3_000))
        self.assertTrue(_cadence_due(control, last_logged_ms=1_000, now_ms=6_001))


if __name__ == "__main__":
    unittest.main()
