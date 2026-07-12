"""Live GNU Radio wrapper for the coherent AX100 Mode-5 detector.

The heavy detector is deliberately kept off GNU Radio's scheduler thread.
``work()`` only copies post-FIR samples into a bounded queue and returns; one
worker owns the rolling detector.  The block also arbitrates the production
gr-satellites PDUs and coherent results so one physical burst reaches the GSS
once.  The normally faster production result wins when it arrives first; the
reverse race suppresses its late duplicate rather than emitting twice.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import pmt
import yaml
from gnuradio import gr


MODE5_FRAMING = "AX100 ASM+Golay"
CPM_CFO_SPAN_HZ = 10_000.0
QUEUE_SECONDS = 10.0
PRODUCTION_CACHE_SECONDS = 600.0
PRODUCTION_CACHE_STREAM_SECONDS = 120.0
PRODUCTION_MATCH_SECONDS = 1.0
PRODUCTION_NEGATIVE_SLACK_SECONDS = 0.25
PRODUCTION_PEER_TOLERANCE_SECONDS = 0.1
CPM_EMISSION_CACHE_SECONDS = 30.0
MAX_CONSECUTIVE_ERRORS = 3

# Live-enable only waveforms already justified by the tranche-5 evidence.
# ROADS 4k8/1200 recovered real golden IQ; NUSHSat 1k2/575 recovered multiple
# independently production-decoded July-12 beacons after adding its measured
# clock and phase-dynamics hypotheses; MAVERIC 9k6/3200 is the mission target
# and passes the calibrated synthetic detector gates. Other Mode-5 profiles
# remain on the production gr-satellites path until real-IQ replay proves their
# coherent model.
LIVE_CPM_WAVEFORMS = frozenset({
    (1_200.0, 575.0),
    (4_800.0, 1_200.0),
    (9_600.0, 3_200.0),
})

# Real-IQ waveform fits used only after the nominal SatYAML declaration has
# selected an approved live branch. NUSHSat's transmitter clock measured
# about -250 ppm across the July-12 capture; using that clock recovered more
# independently decoded beacons than the nominal 1200-baud model.
LIVE_BAUD_OVERRIDES = {
    (1_200.0, 575.0): 1_199.7,
}
LIVE_CFO_SPANS_HZ = {
    (1_199.7, 575.0): 650.0,
}


@dataclass(frozen=True)
class LiveHypothesis:
    baud: float
    deviation: float
    transmitter: str


@dataclass(frozen=True)
class _Chunk:
    sample_start: int
    samples: np.ndarray
    reset_offsets: tuple[int, ...]


@dataclass(frozen=True)
class _ProductionFrame:
    digest: bytes
    payload: bytes
    sample_cursor: int
    seen_mono: float


@dataclass(frozen=True)
class _CpmEmission:
    digest: bytes
    payload: bytes
    sample_end: int
    seen_mono: float


def load_mode5_hypotheses(decoder_yml: str | Path) -> tuple[LiveHypothesis, ...]:
    """Read unique Mode-5 waveform hypotheses in SatYAML declaration order."""
    path = Path(decoder_yml)
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    transmitters = document.get("transmitters")
    if not isinstance(transmitters, dict):
        raise ValueError(f"decoder database has no transmitter map: {path}")

    result: list[LiveHypothesis] = []
    seen: set[tuple[float, float]] = set()
    for label, raw in transmitters.items():
        if not isinstance(raw, dict):
            continue
        if str(raw.get("framing") or "") != MODE5_FRAMING:
            continue
        if str(raw.get("modulation") or "").upper() != "FSK":
            continue
        try:
            baud = float(raw["baudrate"])
            deviation = float(raw["deviation"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(baud) and baud > 0
                and math.isfinite(deviation) and deviation > 0):
            continue
        key = (baud, deviation)
        if key in seen:
            continue
        seen.add(key)
        result.append(LiveHypothesis(
            baud=baud,
            deviation=deviation,
            transmitter=str(label),
        ))
    return tuple(result)


class CpmDetectorSink(gr.sync_block):
    """Non-blocking post-FIR CPM sink plus cross-decoder PDU arbiter."""

    def __init__(
        self,
        decoder_yml: str,
        samp_rate: float = 200_000.0,
        *,
        detector_factory: Callable[[tuple[LiveHypothesis, ...]], object] | None = None,
        queue_limit_samples: int | None = None,
    ) -> None:
        gr.sync_block.__init__(
            self, name="coherent_cpm", in_sig=[np.complex64], out_sig=None)
        self.message_port_register_in(pmt.intern("production"))
        self.message_port_register_out(pmt.intern("out"))
        self.set_msg_handler(pmt.intern("production"), self._handle_production)

        self._decoder_yml = str(decoder_yml)
        self._samp_rate = float(samp_rate)
        if self._samp_rate != 200_000.0:
            raise ValueError("coherent CPM requires the post-FIR 200 ksps stream")
        self._queue_limit_samples = int(
            queue_limit_samples or round(QUEUE_SECONDS * self._samp_rate))
        if self._queue_limit_samples <= 0:
            raise ValueError("queue_limit_samples must be positive")

        self._detector_factory = detector_factory
        self._condition = threading.Condition()
        self._chunks: deque[_Chunk] = deque()
        self._queued_samples = 0
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._accepting = False
        self._publishing = False
        self._stop_timed_out = False
        self._rx_time_key = pmt.intern("rx_time")
        self._rx_time_seen = False
        self._latest_stream_sample = 0

        self._publish_lock = threading.Lock()
        self._arbiter_lock = threading.Lock()
        self._production: deque[_ProductionFrame] = deque()
        self._cpm_emitted: deque[_CpmEmission] = deque()
        self._last_health_mono = 0.0
        self._counters = {
            "chunks_dropped": 0,
            "samples_dropped": 0,
            "discontinuity_resets": 0,
            "decoded": 0,
            "production_forwarded": 0,
            "production_suppressed": 0,
            "production_peer_suppressed": 0,
            "cpm_forwarded": 0,
            "cpm_suppressed": 0,
            "detector_errors": 0,
        }

        try:
            parsed = load_mode5_hypotheses(self._decoder_yml)
            self._hypotheses = tuple(
                LiveHypothesis(
                    baud=LIVE_BAUD_OVERRIDES.get(
                        (item.baud, item.deviation), item.baud),
                    deviation=item.deviation,
                    transmitter=item.transmitter,
                )
                for item in parsed
                if (item.baud, item.deviation) in LIVE_CPM_WAVEFORMS
            )
            self._skipped_hypotheses = tuple(
                item for item in parsed
                if (item.baud, item.deviation) not in LIVE_CPM_WAVEFORMS
            )
            self._profile_error = (
                "no live-approved CPM waveform"
                if parsed and not self._hypotheses else ""
            )
            self._cfo_span_hz = max(
                (LIVE_CFO_SPANS_HZ.get(
                    (item.baud, item.deviation), CPM_CFO_SPAN_HZ)
                 for item in self._hypotheses),
                default=CPM_CFO_SPAN_HZ,
            )
        except Exception as exc:
            self._hypotheses = ()
            self._skipped_hypotheses = ()
            self._profile_error = repr(exc)
            self._cfo_span_hz = CPM_CFO_SPAN_HZ

    # -- lifecycle -----------------------------------------------------

    def start(self) -> bool:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return self._accepting and self._publishing
            self._chunks.clear()
            self._queued_samples = 0
            self._cancel.clear()
            self._stop_timed_out = False
            self._accepting = True
            with self._publish_lock:
                self._publishing = True
            with self._arbiter_lock:
                self._production.clear()
                self._cpm_emitted.clear()
            self._rx_time_seen = False
            if not self._hypotheses:
                detail = self._profile_error or "no AX100 Mode-5 branches"
                self._health("disabled", detail=detail, force=True)
                return True
            self._thread = threading.Thread(
                target=self._worker_main,
                name="cpm-detector",
                daemon=True,
            )
            self._thread.start()
        return True

    def stop(self) -> bool:
        with self._condition:
            self._accepting = False
            with self._publish_lock:
                self._publishing = False
            self._cancel.set()
            self._chunks.clear()
            self._queued_samples = 0
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
            if thread.is_alive():
                self._stop_timed_out = True
                self._health("stop_timeout", force=True)
        with self._condition:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None
        return True

    # -- scheduler path ------------------------------------------------

    def work(self, input_items, output_items):
        samples = input_items[0]
        count = len(samples)
        if count == 0:
            return 0
        sample_start = int(self.nitems_read(0))
        sample_end = sample_start + count
        reset_offsets: list[int] = []
        for tag in self.get_tags_in_range(
                0, sample_start, sample_end, self._rx_time_key):
            offset = int(tag.offset)
            if self._rx_time_seen:
                reset_offsets.append(offset)
            else:
                self._rx_time_seen = True

        with self._condition:
            self._latest_stream_sample = sample_end
            if not self._accepting or not self._hypotheses:
                return count
            copied = np.asarray(samples, dtype=np.complex64).copy()
            self._enqueue_locked(_Chunk(
                sample_start=sample_start,
                samples=copied,
                reset_offsets=tuple(reset_offsets),
            ))
            self._condition.notify()
        return count

    def _enqueue_locked(self, chunk: _Chunk) -> None:
        incoming = len(chunk.samples)
        if incoming > self._queue_limit_samples:
            trim = incoming - self._queue_limit_samples
            self._counters["chunks_dropped"] += 1
            self._counters["samples_dropped"] += trim
            chunk = _Chunk(
                sample_start=chunk.sample_start + trim,
                samples=chunk.samples[trim:].copy(),
                reset_offsets=tuple(
                    value for value in chunk.reset_offsets
                    if value >= chunk.sample_start + trim),
            )
            incoming = len(chunk.samples)
        while self._chunks and self._queued_samples + incoming > self._queue_limit_samples:
            old = self._chunks.popleft()
            self._queued_samples -= len(old.samples)
            self._counters["chunks_dropped"] += 1
            self._counters["samples_dropped"] += len(old.samples)
        self._chunks.append(chunk)
        self._queued_samples += incoming

    # -- worker --------------------------------------------------------

    def _make_detector(self):
        if self._detector_factory is not None:
            return self._detector_factory(self._hypotheses)
        from cpm_detector import BurstStreamDetector, Hypothesis

        hypotheses = tuple(Hypothesis(
            item.baud, item.deviation, item.transmitter)
            for item in self._hypotheses)
        return BurstStreamDetector(
            hypotheses,
            cfo_span_hz=self._cfo_span_hz,
            max_candidates=8,
            cancelled=self._cancel.is_set,
        )

    def _worker_main(self) -> None:
        try:
            detector = self._make_detector()
        except Exception as exc:
            self._counters["detector_errors"] += 1
            with self._condition:
                self._accepting = False
            self._health("disabled", detail=repr(exc), force=True)
            return

        self._health("running", force=True)
        expected: int | None = 0
        consecutive_errors = 0
        while not self._cancel.is_set():
            with self._condition:
                while not self._chunks and not self._cancel.is_set():
                    self._condition.wait(timeout=0.5)
                if self._cancel.is_set():
                    break
                chunk = self._chunks.popleft()
                self._queued_samples -= len(chunk.samples)

            try:
                if expected is not None and chunk.sample_start != expected:
                    detector.reset(chunk.sample_start)
                    self._counters["discontinuity_resets"] += 1
                cursor = chunk.sample_start
                index = 0
                for reset_at in sorted(set(chunk.reset_offsets)):
                    split = max(0, min(len(chunk.samples), reset_at - chunk.sample_start))
                    if split > index:
                        self._emit_results(detector.feed(
                            chunk.samples[index:split], sample_start=cursor))
                    detector.reset(reset_at)
                    self._counters["discontinuity_resets"] += 1
                    index = split
                    cursor = reset_at
                if index < len(chunk.samples):
                    self._emit_results(detector.feed(
                        chunk.samples[index:], sample_start=cursor))
                expected = chunk.sample_start + len(chunk.samples)
                consecutive_errors = 0
                self._health("running")
            except Exception as exc:
                if self._cancel.is_set():
                    break
                consecutive_errors += 1
                self._counters["detector_errors"] += 1
                expected = None
                try:
                    detector.reset(chunk.sample_start + len(chunk.samples))
                except Exception:
                    pass
                self._health("error", detail=repr(exc), force=True)
                traceback.print_exc()
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    with self._condition:
                        self._accepting = False
                    self._health(
                        "disabled",
                        detail=f"{consecutive_errors} consecutive detector errors",
                        force=True,
                    )
                    break

    # -- PDU arbitration ----------------------------------------------

    @staticmethod
    def _payload_from_pdu(message) -> bytes | None:
        try:
            body = pmt.cdr(message) if pmt.is_pair(message) else pmt.PMT_NIL
            if not pmt.is_u8vector(body):
                return None
            return bytes(pmt.u8vector_elements(body))
        except Exception:
            return None

    @staticmethod
    def _digest(payload: bytes) -> bytes:
        return hashlib.blake2s(payload, digest_size=16).digest()

    def _handle_production(self, message) -> None:
        payload = self._payload_from_pdu(message)
        now = time.monotonic()
        suppress = False
        peer_suppress = False
        if payload is not None:
            digest = self._digest(payload)
            cursor = int(self._latest_stream_sample)
            with self._arbiter_lock:
                self._prune_arbiter_locked(now, cursor)
                index = self._matching_emission_index_locked(
                    payload, digest, cursor)
                if index is not None:
                    del self._cpm_emitted[index]
                    suppress = True
                elif self._matching_production_peer_index_locked(
                        payload, digest, cursor) is not None:
                    peer_suppress = True
                else:
                    self._production.append(_ProductionFrame(
                        digest=digest,
                        payload=payload,
                        sample_cursor=cursor,
                        seen_mono=now,
                    ))
        if suppress:
            self._counters["production_suppressed"] += 1
            return
        if peer_suppress:
            self._counters["production_peer_suppressed"] += 1
            return
        if self._publish(message):
            self._counters["production_forwarded"] += 1

    def _prune_arbiter_locked(self, now: float, cursor: int) -> None:
        wall_cutoff = now - PRODUCTION_CACHE_SECONDS
        sample_cutoff = cursor - int(round(
            PRODUCTION_CACHE_STREAM_SECONDS * self._samp_rate))
        self._production = deque(
            item for item in self._production
            if item.seen_mono >= wall_cutoff
            and item.sample_cursor >= sample_cutoff
        )
        emission_cutoff = now - CPM_EMISSION_CACHE_SECONDS
        sample_span = int(round(
            (PRODUCTION_MATCH_SECONDS + PRODUCTION_NEGATIVE_SLACK_SECONDS)
            * self._samp_rate))
        self._cpm_emitted = deque(
            item for item in self._cpm_emitted
            if item.seen_mono >= emission_cutoff
            and cursor - item.sample_end <= sample_span
        )

    def _same_physical_burst(self, production_cursor: int,
                             cpm_sample_end: int) -> bool:
        delta = int(production_cursor) - int(cpm_sample_end)
        negative_slack = int(round(
            PRODUCTION_NEGATIVE_SLACK_SECONDS * self._samp_rate))
        positive_span = int(round(
            PRODUCTION_MATCH_SECONDS * self._samp_rate))
        return -negative_slack <= delta <= positive_span

    def _matching_production_index_locked(
            self, payload: bytes, digest: bytes,
            sample_end: int) -> int | None:
        candidates = [
            (abs(item.sample_cursor - sample_end), index)
            for index, item in enumerate(self._production)
            if item.digest == digest and item.payload == payload
            and self._same_physical_burst(item.sample_cursor, sample_end)
        ]
        return min(candidates)[1] if candidates else None

    def _matching_production_peer_index_locked(
            self, payload: bytes, digest: bytes,
            production_cursor: int) -> int | None:
        tolerance = int(round(
            PRODUCTION_PEER_TOLERANCE_SECONDS * self._samp_rate))
        candidates = [
            (abs(production_cursor - item.sample_cursor), index)
            for index, item in enumerate(self._production)
            if item.digest == digest and item.payload == payload
            and abs(production_cursor - item.sample_cursor) <= tolerance
        ]
        return min(candidates)[1] if candidates else None

    def _matching_emission_index_locked(
            self, payload: bytes, digest: bytes,
            production_cursor: int) -> int | None:
        candidates = [
            (abs(production_cursor - item.sample_end), index)
            for index, item in enumerate(self._cpm_emitted)
            if item.digest == digest and item.payload == payload
            and self._same_physical_burst(production_cursor, item.sample_end)
        ]
        return min(candidates)[1] if candidates else None

    def _emit_results(self, results: Iterable[object]) -> None:
        if self._cancel.is_set():
            return
        for result in results:
            if self._cancel.is_set():
                return
            self._counters["decoded"] += 1
            payload = bytes(result.payload)
            digest = self._digest(payload)
            now = time.monotonic()
            emission: _CpmEmission | None = None
            with self._arbiter_lock:
                self._prune_arbiter_locked(
                    now, int(self._latest_stream_sample))
                index = self._matching_production_index_locked(
                    payload, digest, int(result.sample_end))
                if index is not None:
                    del self._production[index]
                else:
                    emission = _CpmEmission(
                        digest=digest,
                        payload=payload,
                        sample_end=int(result.sample_end),
                        seen_mono=now,
                    )
                    self._cpm_emitted.append(emission)
            if emission is None:
                self._counters["cpm_suppressed"] += 1
                continue
            metadata = {
                "transmitter": str(result.transmitter),
                "decoder": "coherent_cpm",
                "baud": float(result.baud),
                "deviation": float(result.deviation),
                "cfo_hz": float(result.cfo_hz),
                "sync_score": float(result.sync_score),
                "sample_start": int(result.sample_start),
                "sample_end": int(result.sample_end),
            }
            message = pmt.cons(
                pmt.to_pmt(metadata),
                pmt.init_u8vector(len(payload), list(payload)),
            )
            if not self._publish(message):
                with self._arbiter_lock:
                    try:
                        self._cpm_emitted.remove(emission)
                    except ValueError:
                        pass
                return
            self._counters["cpm_forwarded"] += 1

    def _publish(self, message) -> bool:
        """Publish only while started, serialized against shutdown."""
        with self._publish_lock:
            if not self._publishing:
                return False
            self.message_port_pub(pmt.intern("out"), message)
            return True

    # -- diagnostics ---------------------------------------------------

    def _health(self, state: str, *, detail: str = "", force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_health_mono < 10.0:
            return
        self._last_health_mono = now
        with self._condition:
            queued = self._queued_samples
        payload = {
            "state": state,
            "detail": detail,
            "profile": Path(self._decoder_yml).name,
            "hypotheses": len(self._hypotheses),
            "cfo_span_hz": self._cfo_span_hz,
            "unvalidated_hypotheses_skipped": len(self._skipped_hypotheses),
            "queue_samples": queued,
            **self._counters,
        }
        print("CPM_HEALTH " + json.dumps(payload, sort_keys=True), flush=True)
