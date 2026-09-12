"""Generic TX frequency-offset search sequencer.

A provisional acquisition tool: on top of whatever TX correction is
already live, step by a bounded outward zig-zag from a configurable base
on every aperiodic uplink send, searching for an offset that lets a
command land within the receiver's actual capture range. Purely
mechanical here — the base/step/max-deviation values, and whether
running this is worth it at all, are an operational decision made per
mission via config; this module knows nothing about any particular
mission's radio or spectrum constraints.
"""

from __future__ import annotations

from dataclasses import dataclass


def _clamp(value: float, limit: float) -> float:
    if limit <= 0:
        return 0.0
    return max(-limit, min(limit, value))


def _build_sequence(base_hz: float, step_hz: float, max_deviation_hz: float) -> list[float]:
    """Bounded outward zig-zag: base, base+step, base-step, base+2*step, ...

    Every value is clamped to stay within ``max_deviation_hz`` of *zero*,
    not of ``base`` — this is meant to model a physical/regulatory ceiling
    on the absolute transmit offset, so re-centering the base can never
    push a generated step past it.
    """
    base_hz = _clamp(base_hz, max_deviation_hz)
    if step_hz <= 0 or max_deviation_hz <= 0:
        return [base_hz]
    seen: set[float] = set()
    sequence: list[float] = []
    max_k = int(max_deviation_hz // step_hz) + 2
    for k in range(max_k + 1):
        for signed_k in ((0,) if k == 0 else (k, -k)):
            value = round(base_hz + signed_k * step_hz, 6)
            if abs(value) > max_deviation_hz + 1e-6 or value in seen:
                continue
            seen.add(value)
            sequence.append(value)
    return sequence or [0.0]


@dataclass(frozen=True, slots=True)
class OffsetSweepConfig:
    base_hz: float
    step_hz: float
    max_deviation_hz: float


class OffsetSweep:
    """Cycles through a bounded outward zig-zag of TX offsets.

    ``advance()`` moves to the next offset in the sequence — meant to be
    called once per aperiodic uplink send, never on a timer — and wraps
    back to the start once the sequence is exhausted. ``reset()`` rebuilds
    the sequence around a new configuration (e.g. re-centering on the
    last confirmed-working offset) and starts over from its first value.
    """

    def __init__(self, config: OffsetSweepConfig) -> None:
        self._sequence = _build_sequence(config.base_hz, config.step_hz, config.max_deviation_hz)
        self._index = 0

    @property
    def current_offset_hz(self) -> float:
        return self._sequence[self._index]

    def advance(self) -> float:
        self._index = (self._index + 1) % len(self._sequence)
        return self.current_offset_hz

    def reset(self, config: OffsetSweepConfig) -> None:
        self._sequence = _build_sequence(config.base_hz, config.step_hz, config.max_deviation_hz)
        self._index = 0


__all__ = ["OffsetSweep", "OffsetSweepConfig"]
