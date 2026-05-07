"""AlarmDispatch transition-only audit and broadcast behavior."""
from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace

from mav_gss_lib.platform.alarms.contract import (
    AlarmChange,
    AlarmEvent,
    AlarmSource,
    AlarmState,
    Severity,
)
from mav_gss_lib.platform.alarms.dispatch import AlarmDispatch


class _Sink:
    def __init__(self) -> None:
        self.records: list[tuple[AlarmChange, int]] = []

    def write_alarm(self, change: AlarmChange, ts_ms: int) -> None:
        self.records.append((change, ts_ms))


class _Target:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def broadcast_text(self, text: str) -> None:
        self.messages.append(json.loads(text))


def _change(
    *,
    detail: str,
    now_ms: int,
    state: AlarmState = AlarmState.UNACKED_ACTIVE,
    severity: Severity = Severity.WARNING,
    prev_state: AlarmState | None = AlarmState.UNACKED_ACTIVE,
    prev_severity: Severity | None = Severity.WARNING,
    removed: bool = False,
    operator: str = "",
) -> AlarmChange:
    return AlarmChange(
        event=AlarmEvent(
            id="platform.silence",
            source=AlarmSource.PLATFORM,
            label="RX SILENCE",
            detail=detail,
            severity=severity,
            state=state,
            first_seen_ms=1000,
            last_eval_ms=now_ms,
            last_transition_ms=now_ms,
            context={},
        ),
        prev_state=prev_state,
        prev_severity=prev_severity,
        removed=removed,
        operator=operator,
    )


class TestAlarmDispatch(unittest.TestCase):
    def test_no_audit_write_on_detail_only_change(self) -> None:
        writes: list[tuple[str, int]] = []

        class _DetailSink:
            def write_alarm(self, change: AlarmChange, ts_ms: int) -> None:
                writes.append((change.event.detail, ts_ms))

        class _NullBroadcast:
            async def broadcast_text(self, text: str) -> None:
                return None

        d = AlarmDispatch(
            audit_sink=_DetailSink(),
            broadcast_target=_NullBroadcast(),
            loop=None,
        )
        ev_a = AlarmEvent(
            id="container.eps_hk.stale",
            source=AlarmSource.CONTAINER,
            label="EPS HK STALE",
            detail="no packet for 30m",
            severity=Severity.WARNING,
            state=AlarmState.UNACKED_ACTIVE,
            first_seen_ms=0,
            last_eval_ms=0,
            last_transition_ms=0,
            context={},
        )
        ev_b = replace(ev_a, detail="no packet for 31m", last_eval_ms=60_000)
        d.emit(AlarmChange(event=ev_a, prev_state=None, prev_severity=None,
                           removed=False, operator=""), now_ms=0)
        d.emit(AlarmChange(event=ev_b, prev_state=AlarmState.UNACKED_ACTIVE,
                           prev_severity=Severity.WARNING,
                           removed=False, operator=""), now_ms=60_000)
        # First emit writes (initial transition); second must not.
        self.assertEqual(len(writes), 1)

    def test_state_transition_audits_inside_detail_throttle_window(self) -> None:
        sink = _Sink()
        dispatch = AlarmDispatch(
            audit_sink=sink,
            broadcast_target=_Target(),
            loop=None,
        )

        dispatch.emit(_change(
            detail="no packet for 200s",
            now_ms=1000,
            prev_state=None,
            prev_severity=None,
        ), 1000)
        dispatch.emit(_change(
            detail="rx resumed",
            now_ms=2000,
            state=AlarmState.UNACKED_CLEARED,
            prev_state=AlarmState.UNACKED_ACTIVE,
            prev_severity=Severity.WARNING,
        ), 2000)

        self.assertEqual([ts for _, ts in sink.records], [1000, 2000])

    def test_broadcasts_every_change_even_when_audit_is_throttled(self) -> None:
        sink = _Sink()
        target = _Target()
        loop = asyncio.new_event_loop()
        try:
            dispatch = AlarmDispatch(
                audit_sink=sink,
                broadcast_target=target,
                loop=loop,
            )

            dispatch.emit(_change(
                detail="no packet for 200s",
                now_ms=1000,
                prev_state=None,
                prev_severity=None,
            ), 1000)
            dispatch.emit(_change(detail="no packet for 201s", now_ms=2000), 2000)

            loop.run_until_complete(asyncio.sleep(0.01))

            self.assertEqual(len(sink.records), 1)
            self.assertEqual(len(target.messages), 2)
            self.assertEqual(target.messages[0]["event"]["id"], "platform.silence")
            self.assertEqual(target.messages[1]["event"]["detail"], "no packet for 201s")
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
