"""End-to-end RX decode: ingest record → packet → walker.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from ..contract.mission import MissionSpec
from ..spec.runtime import DeclarativeWalker
from .packet_pipeline import PacketPipeline
from .records import RxDecodedRecord, RxIngestRecord, make_ingest_record


@dataclass(slots=True)
class RxResult(RxDecodedRecord):
    """Backward-compatible name for the decoded RX domain record."""


class RxPipeline:
    """Platform RX flow, independent of the web runtime.

    Ordering:
      1. packet normalize/parse/classify
      2. walker.extract → ParamUpdate stream
      3. return decoded domain record; cache/log/UI are server projections
    """

    def __init__(
        self,
        mission: MissionSpec,
        walker: DeclarativeWalker | None,
    ) -> None:
        self.mission = mission
        self.packet_pipeline = PacketPipeline(mission)
        self.walker = walker

    def process(
        self,
        ingest: RxIngestRecord | dict[str, Any],
        raw: bytes | None = None,
    ) -> RxResult:
        if not isinstance(ingest, RxIngestRecord):
            if raw is None:
                raise TypeError("raw bytes are required when processing meta")
            ingest = make_ingest_record(0, ingest, raw)

        packet = self.packet_pipeline.process(
            ingest.transport_meta,
            ingest.raw,
            event_id=ingest.event_id,
            received_at_ms=ingest.received_at_ms,
            received_mono_ns=ingest.received_mono_ns,
        )
        wp = getattr(packet.mission_payload, "walker_packet", None)
        matched_container_id: str | None = None
        if self.walker is not None and wp is not None:
            parent = self.walker.match_parent(wp)
            matched_container_id = parent.name if parent is not None else None
            try:
                packet.parameters = tuple(self.walker.extract(wp, packet.received_at_ms))
            except Exception:
                logging.exception(
                    "walker.extract raised; dropping parameters for this packet"
                )
                packet.parameters = ()
        else:
            packet.parameters = _mission_param_updates(packet)

        return RxResult(
            ingest=ingest,
            packet=packet,
            container_id=matched_container_id,
        )


def _mission_param_updates(packet) -> tuple:
    """Ready ParamUpdates from a mission payload that cannot be walked
    declaratively (e.g. frames that lost their start, so fields are not at
    fixed positions). Updates without a timestamp get the packet's receive
    time."""
    updates = getattr(packet.mission_payload, "param_updates", None) or ()
    return tuple(u if u.ts_ms else replace(u, ts_ms=packet.received_at_ms)
                 for u in updates)
