"""Unified session log — JSONL entries for RX, TX and audit events.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

from typing import Any, Iterable

from mav_gss_lib.constants import DEFAULT_MISSION_NAME

from ._base import _BaseLog


class SessionLog(_BaseLog):
    """Unified session log — JSONL event stream."""

    def __init__(
        self,
        log_dir: str,
        zmq_addr: str,
        version: str = "",
        mission_name: str = DEFAULT_MISSION_NAME,
        *,
        mission_id: str = "",
        station: str = "",
        operator: str = "",
        host: str = "",
    ) -> None:
        super().__init__(log_dir, "session", version, "Session Log", zmq_addr,
                         mission_name=mission_name, mission_id=mission_id,
                         station=station, operator=operator, host=host)

    def write_packet(
        self,
        record: dict[str, Any],
        packet: Any,
        *,
        parameter_records: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        """Write one rx_packet record + any parameter-event rows.

        *record* is the pre-built JSONL envelope from
        ``mav_gss_lib.platform.log_records.rx_packet_record``. *parameter_records*
        is the iterable of flat parameter events (one per ``ParamUpdate``)
        from ``parameter_records``. Both land in the same JSONL file;
        the packet record goes first so an ingest streaming through the
        file sees the parent before its children.
        """
        self.write_jsonl(record)
        if parameter_records:
            for p in parameter_records:
                self.write_jsonl(p)

    def write_mission_command(
        self,
        record: dict[str, Any],
        *,
        raw_cmd: bytes,
        wire: bytes,
        log_text: list[str] | None = None,
    ) -> None:
        """Write one mission-built TX command event."""
        self.write_jsonl(record)

    def write_cmd_verifier(self, record: dict[str, Any]) -> None:
        """Write one cmd_verifier event into the same session JSONL stream."""
        from mav_gss_lib.platform._log_envelope import new_event_id, ts_iso
        import time as _time

        ts_ms = int(_time.time() * 1000)
        envelope: dict[str, Any] = {
            "event_id": new_event_id(),
            "event_kind": "cmd_verifier",
            "session_id": self.session_id,
            "ts_ms": ts_ms,
            "ts_iso": ts_iso(ts_ms),
            "seq": record.get("seq", 0),
            "v": self._version,
            "mission_id": self.mission_id,
            "operator": self._operator,
            "station": self._station,
        }
        envelope.update(record)
        envelope["event_kind"] = "cmd_verifier"
        self.write_jsonl(envelope)

    def write_alarm(self, change: Any, ts_ms: int) -> None:
        """Append one alarm-transition record using the unified envelope.

        Alarm events are out-of-band with respect to the RX packet stream, so
        ``seq`` is 0 by convention.  SQL ingest must not join on
        ``(session_id, seq)`` for ``event_kind="alarm"``; use
        ``(session_id, alarm.id, ts_ms)`` instead.
        """
        from mav_gss_lib.platform._log_envelope import new_event_id, ts_iso
        ev = change.event
        record = {
            "event_id": new_event_id(),
            "event_kind": "alarm",
            "session_id": self.session_id,
            "ts_ms": ts_ms,
            "ts_iso": ts_iso(ts_ms),
            "seq": 0,
            "v": self._version,
            "mission_id": self._mission_id,
            "operator": self._operator,
            "station": self._station,
            "alarm": {
                "id": ev.id,
                "source": str(ev.source),
                "label": ev.label,
                "detail": ev.detail,
                "severity": ev.severity.name.lower(),
                "state": str(ev.state),
                "prev_state": (
                    str(change.prev_state) if change.prev_state is not None else None
                ),
                "prev_severity": (
                    change.prev_severity.name.lower()
                    if change.prev_severity is not None else None
                ),
                "removed": change.removed,
                "first_seen_ms": ev.first_seen_ms,
                "last_transition_ms": ev.last_transition_ms,
                "context": ev.context,
                "operator": change.operator,
            },
        }
        self.write_jsonl(record)

    def write_radio_event(
        self,
        action: str,
        *,
        state: str = "",
        pid: int | None = None,
        exit_code: int | None = None,
        command: list[str] | None = None,
        script: str = "",
        cwd: str = "",
        detail: str = "",
        expected: bool | None = None,
        ts_ms: int | None = None,
    ) -> None:
        """Append one GNU Radio supervisor lifecycle event."""
        from mav_gss_lib.platform.log_records import radio_event_record
        import time as _time

        stamp = ts_ms if ts_ms is not None else int(_time.time() * 1000)
        self.write_jsonl(radio_event_record(
            action,
            session_id=self.session_id,
            ts_ms=stamp,
            version=self._version,
            mission_id=self._mission_id,
            operator=self._operator,
            station=self._station,
            state=state,
            pid=pid,
            exit_code=exit_code,
            command=command,
            script=script,
            cwd=cwd,
            detail=detail,
            expected=expected,
        ))

    def write_tracking_event(
        self,
        action: str,
        *,
        mode: str = "",
        prev_mode: str = "",
        station_id: str = "",
        rx_zmq_addr: str = "",
        tx_zmq_addr: str = "",
        detail: str = "",
        ts_ms: int | None = None,
    ) -> None:
        """Append one tracking-subsystem lifecycle event (Doppler engage / disengage)."""
        from mav_gss_lib.platform.log_records import tracking_event_record
        import time as _time

        stamp = ts_ms if ts_ms is not None else int(_time.time() * 1000)
        self.write_jsonl(tracking_event_record(
            action,
            session_id=self.session_id,
            ts_ms=stamp,
            version=self._version,
            mission_id=self._mission_id,
            operator=self._operator,
            station=self._station,
            mode=mode,
            prev_mode=prev_mode,
            station_id=station_id,
            rx_zmq_addr=rx_zmq_addr,
            tx_zmq_addr=tx_zmq_addr,
            detail=detail,
        ))

    def write_tracking_sample(self, doppler: dict[str, Any], *, source: str) -> None:
        """Append one tracking-sample event: az/el + requested Doppler-corrected
        RX/TX frequencies at one instant. *doppler* is the dict
        TrackingService.doppler() returns. *source* is "tick" for a
        background sample, "rx_decode" for one taken at downlink decode time,
        or "tx_attempt" for one taken at TX send time — see
        platform.log_records.tracking_sample_record."""
        from mav_gss_lib.platform.log_records import tracking_sample_record

        self.write_jsonl(tracking_sample_record(
            doppler,
            session_id=self.session_id,
            version=self._version,
            source=source,
            mission_id=self._mission_id,
            operator=self._operator,
            station=self._station,
        ))
