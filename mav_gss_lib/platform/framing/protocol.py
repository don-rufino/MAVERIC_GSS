"""Generic framer contract used by missions to compose a wire stack.

A `Framer` wraps a single protocol layer (CSP v1, AX.25, ASM+Golay, ...).
Missions select and order framers — the platform does not know or care which
layers a mission picks. A `FramerChain` composes a list of framers in
application order: the first entry is innermost (applied first), the last
entry is outermost (applied last).

Each framer exposes:
    * `frame_label`     — short display tag surfaced in the TX log envelope
    * `frame(payload)`  — wrap `payload` in this layer
    * `overhead()`      — bytes this layer adds to a payload (informational)
    * `max_payload()`   — max input size this layer accepts (None = unbounded)
    * `log_fields()`    — JSONL-safe metadata about this layer's runtime state
    * `log_line()`      — one-line human-readable banner for the text TX log

The chain reduces these to chain-level `frame()`, `max_payload()`,
`log_fields()`, and `log_lines()` so mission code stays a thin composer.
"""

from __future__ import annotations

from typing import Any, Iterable, Protocol, runtime_checkable


def _deep_merge_log_fields(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """Recursive merge: src wins on leaf collisions; nested dicts merge."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge_log_fields(dst[k], v)
        else:
            dst[k] = v


@runtime_checkable
class Framer(Protocol):
    """One protocol layer in a wire stack."""

    frame_label: str

    def frame(self, payload: bytes) -> bytes: ...
    def overhead(self) -> int: ...
    def max_payload(self) -> int | None: ...
    def log_fields(self) -> dict[str, Any]: ...
    def log_line(self) -> str | None: ...


class FramerChain:
    """Ordered composition of framers.

    Layers in the list are listed innermost-first: `FramerChain([CSP, Golay])`
    means CSP wraps the raw payload, Golay wraps the CSP packet.
    """

    __slots__ = ("framers", "frame_label")

    def __init__(self, framers: Iterable[Framer]) -> None:
        self.framers: list[Framer] = list(framers)
        self.frame_label = " + ".join(f.frame_label for f in self.framers)

    def frame(self, payload: bytes) -> bytes:
        out = payload
        for f in self.framers:
            out = f.frame(out)
        return out

    def overhead(self) -> int:
        return sum(f.overhead() for f in self.framers)

    def max_payload(self) -> int | None:
        cap: int | None = None
        overhead_below = 0
        for f in self.framers:
            f_cap = f.max_payload()
            if f_cap is not None:
                adjusted = f_cap - overhead_below
                if cap is None or adjusted < cap:
                    cap = adjusted
            overhead_below += f.overhead()
        return cap

    def log_fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in self.framers:
            _deep_merge_log_fields(out, f.log_fields())
        return out

    def log_lines(self) -> list[str]:
        return [line for f in self.framers if (line := f.log_line()) is not None]
