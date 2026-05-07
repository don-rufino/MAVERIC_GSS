"""Platform verifier types.

Shapes the verifier state machine consumes; no behavior. The registry that
applies behavior lives in the same module (added in a later task) and pairs
with `CheckWindow` timers and persistence.

Stage is derived from the full verifier outcome map, not a linear chain.
See docs/superpowers/specs/2026-04-24-command-verification-design.md §4.4
for the derivation rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


VerifierStage = Literal["received", "accepted", "complete", "failed"]
VerifierState = Literal["pending", "passed", "failed", "window_expired"]
InstanceStage = Literal[
    "released", "received", "accepted", "complete", "failed", "timed_out",
]


@dataclass(frozen=True, slots=True)
class CheckWindow:
    start_ms: int
    stop_ms: int


@dataclass(frozen=True, slots=True)
class VerifierSpec:
    verifier_id: str          # opaque mission-assigned
    stage: VerifierStage
    check_window: CheckWindow
    display_label: str        # e.g. "UPPM" — mission-provided
    display_tone: str         # 'info' | 'success' | 'warning' | 'danger'


@dataclass(frozen=True, slots=True)
class VerifierSet:
    verifiers: tuple[VerifierSpec, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for v in self.verifiers:
            if v.verifier_id in seen:
                raise ValueError(f"duplicate verifier_id in VerifierSet: {v.verifier_id}")
            seen.add(v.verifier_id)


@dataclass(frozen=True, slots=True)
class VerifierOutcome:
    state: VerifierState
    matched_at_ms: int | None = None
    match_event_id: str | None = None

    @classmethod
    def pending(cls) -> "VerifierOutcome":
        return cls(state="pending")

    @classmethod
    def passed(cls, *, matched_at_ms: int, match_event_id: str) -> "VerifierOutcome":
        return cls(state="passed", matched_at_ms=matched_at_ms, match_event_id=match_event_id)

    @classmethod
    def failed(cls, *, matched_at_ms: int, match_event_id: str) -> "VerifierOutcome":
        return cls(state="failed", matched_at_ms=matched_at_ms, match_event_id=match_event_id)

    @classmethod
    def window_expired(cls) -> "VerifierOutcome":
        return cls(state="window_expired")


@dataclass(slots=True)
class CommandInstance:
    instance_id: str
    correlation_key: tuple
    t0_ms: int
    cmd_event_id: str
    verifier_set: VerifierSet
    outcomes: dict[str, VerifierOutcome] = field(default_factory=dict)
    stage: InstanceStage = "released"


# ─── Registry ─────────────────────────────────────────────────────────


def _derive_stage(inst: CommandInstance) -> InstanceStage:
    """Compute an instance's stage from its verifier outcomes.

    Rules (spec §4.4):
      0. Empty VerifierSet ("verification disabled" or fixture missions):
         terminal as Complete. There's nothing to wait
         for; keeping it non-terminal would block the admission gate
         indefinitely.
      1. Any FailedVerifier passed → Failed (NACK wins, even after Complete).
      2. Else CompleteVerifier passed → Complete.
      3. Else an ACK-only set with any ReceivedVerifier passed → Complete.
      4. Else all verifier windows closed → TimedOut.
      5. Else transient: Accepted | Received | Released.
    """
    if not inst.verifier_set.verifiers:
        return "complete"

    received_specs  = [v for v in inst.verifier_set.verifiers if v.stage == "received"]
    failed_specs    = [v for v in inst.verifier_set.verifiers if v.stage == "failed"]
    complete_specs  = [v for v in inst.verifier_set.verifiers if v.stage == "complete"]

    # 1. Failed — any NACK passed.
    for spec in failed_specs:
        if inst.outcomes.get(spec.verifier_id, VerifierOutcome.pending()).state == "passed":
            return "failed"

    # 2. Complete — any CompleteVerifier passed.
    for spec in complete_specs:
        if inst.outcomes.get(spec.verifier_id, VerifierOutcome.pending()).state == "passed":
            return "complete"

    received_passed = sum(
        1 for v in received_specs
        if inst.outcomes.get(v.verifier_id, VerifierOutcome.pending()).state == "passed"
    )
    if received_specs and not complete_specs and received_passed > 0:
        return "complete"

    # 3. TimedOut — every window closed (passed or window_expired, not pending).
    all_closed = all(
        inst.outcomes.get(v.verifier_id, VerifierOutcome.pending()).state != "pending"
        for v in inst.verifier_set.verifiers
    )
    if all_closed:
        return "timed_out"

    # 4. Transient.
    if received_specs and received_passed == len(received_specs):
        return "accepted"
    if received_passed > 0:
        return "received"
    return "released"


_TERMINAL: tuple[InstanceStage, ...] = ("complete", "failed", "timed_out")


def is_settled(inst: CommandInstance) -> bool:
    """Canonical 'this instance is done' predicate.

    True iff the instance has reached a terminal stage AND every declared
    verifier outcome has been decided (no pending). Late-NACK semantics
    require waiting for every window to close before considering an
    instance done — this is the single check every subsystem uses.
    """
    if inst.stage not in _TERMINAL:
        return False
    for spec in inst.verifier_set.verifiers:
        outcome = inst.outcomes.get(spec.verifier_id, VerifierOutcome.pending())
        if outcome.state == "pending":
            return False
    return True


class VerifierRegistry:
    """In-memory registry of open command instances.

    Platform-owned, mission-agnostic. All mutation sites run on the asyncio
    event loop:
      - TxService.register(instance) on publish
      - RxService.broadcast_loop after match_verifiers (NOT the ZMQ SUB
        thread — that thread only hands raw frames off via queue.Queue)
      - Sweeper task for window_expired transitions
      - Admission gate via lookup_open(correlation_key)

    Concurrency: mutations are currently asyncio-serialized. A `threading.Lock`
    is held on every access anyway — belt-and-suspenders against future
    refactors that might introduce true cross-thread access. The lock adds
    negligible overhead (microseconds) and makes the code robust to callers
    that forget the architectural invariant.

    Terminal instances remain in `open_instances()` until `finalize_terminals()`
    is called — lets the UI observe the final state before the row becomes a
    pure history entry. The sweeper calls finalize at end of its pass.
    """

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self._by_id: dict[str, CommandInstance] = {}
        self._dirty: set[str] = set()

    def register(self, instance: CommandInstance) -> None:
        with self._lock:
            self._by_id[instance.instance_id] = instance
            self._dirty.add(instance.instance_id)

    def apply(self, instance_id: str, verifier_id: str, outcome: VerifierOutcome) -> None:
        with self._lock:
            inst = self._by_id.get(instance_id)
            if inst is None:
                return
            inst.outcomes[verifier_id] = outcome
            inst.stage = _derive_stage(inst)
            self._dirty.add(instance_id)

    def open_instances(self) -> list[CommandInstance]:
        with self._lock:
            return list(self._by_id.values())

    def lookup_open(self, correlation_key: tuple) -> CommandInstance | None:
        with self._lock:
            for inst in self._by_id.values():
                if inst.correlation_key == correlation_key and not is_settled(inst):
                    return inst
            return None

    def finalize_terminals(self) -> list[CommandInstance]:
        """Drop and return terminal instances. Caller logs them."""
        with self._lock:
            terminal_ids = [i.instance_id for i in self._by_id.values() if i.stage in _TERMINAL]
            return [self._by_id.pop(i) for i in terminal_ids]

    def finalize_settled(self) -> list[CommandInstance]:
        """Drop instances where ``is_settled(inst)`` holds.

        Returns the dropped instances so the caller can log + persist.
        """
        with self._lock:
            settled_ids = [i for i, inst in self._by_id.items() if is_settled(inst)]
            return [self._by_id.pop(i) for i in settled_ids]

    def sweep(self, *, now_ms: int) -> None:
        """Mark every pending verifier whose window has closed as window_expired.

        Called periodically (e.g., once per second) from a platform task.
        Called also at RX/TX critical points to keep the UI snappy.
        Marks an instance dirty only when at least one verifier transitioned
        — _derive_stage is pure given the outcome map, so re-running it
        without changes is a no-op.
        """
        with self._lock:
            for inst in self._by_id.values():
                changed = False
                for spec in inst.verifier_set.verifiers:
                    current = inst.outcomes.get(spec.verifier_id, VerifierOutcome.pending())
                    if current.state != "pending":
                        continue
                    deadline = inst.t0_ms + spec.check_window.stop_ms
                    if now_ms >= deadline:
                        inst.outcomes[spec.verifier_id] = VerifierOutcome.window_expired()
                        changed = True
                if changed:
                    inst.stage = _derive_stage(inst)
                    self._dirty.add(inst.instance_id)

    def consume_dirty(self) -> list[CommandInstance]:
        """Return and clear the set of instances touched since last call.

        Used by the broadcast layer to avoid redundant /ws/tx messages.
        """
        with self._lock:
            touched = [self._by_id[i] for i in self._dirty if i in self._by_id]
            self._dirty.clear()
            return touched


# ─── Persistence ──────────────────────────────────────────────────────
#
# `.pending_instances.jsonl` format: one instance per line as a JSON object.
# Rewrite-on-transition (simpler than append + gc); dozens of lines max,
# sub-ms write cost.
#
# Terminal instances are dropped from the file (they're already in the log).
# On restore, elapsed time since t0 computes window_expired retroactively
# for any verifier whose check_window.stop_ms has passed.

import json


def serialize_instance(inst: CommandInstance) -> str:
    obj = {
        "instance_id": inst.instance_id,
        "correlation_key": list(inst.correlation_key),
        "t0_ms": inst.t0_ms,
        "cmd_event_id": inst.cmd_event_id,
        "verifier_set": {
            "verifiers": [
                {
                    "verifier_id": v.verifier_id,
                    "stage": v.stage,
                    "check_window": {
                        "start_ms": v.check_window.start_ms,
                        "stop_ms": v.check_window.stop_ms,
                    },
                    "display_label": v.display_label,
                    "display_tone": v.display_tone,
                }
                for v in inst.verifier_set.verifiers
            ],
        },
        "outcomes": {
            vid: {
                "state": o.state,
                "matched_at_ms": o.matched_at_ms,
                "match_event_id": o.match_event_id,
            }
            for vid, o in inst.outcomes.items()
        },
        "stage": inst.stage,
    }
    return json.dumps(obj)


def parse_instance(obj: dict) -> CommandInstance:
    vs = VerifierSet(verifiers=tuple(
        VerifierSpec(
            verifier_id=v["verifier_id"],
            stage=v["stage"],
            check_window=CheckWindow(
                start_ms=v["check_window"]["start_ms"],
                stop_ms=v["check_window"]["stop_ms"],
            ),
            display_label=v["display_label"],
            display_tone=v["display_tone"],
        )
        for v in obj["verifier_set"]["verifiers"]
    ))
    outcomes = {
        vid: VerifierOutcome(
            state=o["state"],
            matched_at_ms=o.get("matched_at_ms"),
            match_event_id=o.get("match_event_id"),
        )
        for vid, o in obj["outcomes"].items()
    }
    return CommandInstance(
        instance_id=obj["instance_id"],
        correlation_key=tuple(obj["correlation_key"]),
        t0_ms=obj["t0_ms"],
        cmd_event_id=obj["cmd_event_id"],
        verifier_set=vs,
        outcomes=outcomes,
        stage=obj["stage"],
    )


def write_instances(path, instances: list[CommandInstance]) -> None:
    """Atomic rewrite. Settled instances are excluded (live in the log,
    not the registry). Terminal-but-unsettled instances (open NACK / TLM
    windows) MUST persist so a post-restart late NACK can override."""
    import os as _os
    lines = [serialize_instance(i) for i in instances if not is_settled(i)]
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    _os.replace(tmp, str(path))


def restore_instances(path, *, now_ms: int) -> list[CommandInstance]:
    """Load open instances. For each, mark window_expired for any verifier
    whose check_window.stop_ms has passed since t0, then re-derive stage.
    Drop any instance that is fully settled (terminal AND every outcome
    decided) — terminal-but-unsettled instances must be restored so a
    post-restart late NACK can still override."""
    from pathlib import Path as _Path
    p = _Path(path)
    if not p.exists():
        return []
    restored: list[CommandInstance] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            inst = parse_instance(json.loads(line))
        except Exception:
            continue
        # Do NOT skip terminal-on-disk instances — they may be terminal-
        # but-unsettled (open NACK window). Re-evaluate every verifier
        # window against the current wall clock; let _derive_stage update
        # the stage from the resulting outcome map.
        for spec in inst.verifier_set.verifiers:
            current = inst.outcomes.get(spec.verifier_id, VerifierOutcome.pending())
            if current.state != "pending":
                continue
            if now_ms - inst.t0_ms >= spec.check_window.stop_ms:
                inst.outcomes[spec.verifier_id] = VerifierOutcome.window_expired()
        inst.stage = _derive_stage(inst)
        # Single drop predicate: settled across the restart boundary.
        if is_settled(inst):
            continue
        restored.append(inst)
    return restored
