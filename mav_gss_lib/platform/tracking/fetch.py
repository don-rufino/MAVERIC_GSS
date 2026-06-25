"""TLE fetch from CelesTrak (primary) and Space-Track (fallback).

Pure and offline-testable: all outbound HTTP flows through an injected
`http_opener` (defaults to a urllib opener), so unit tests pass canned bytes and
the suite never touches the network. This is the ONLY backend module that
performs outbound HTTP — enforced by a guardrail test.

Author: Irfan Annuar - USC ISI SERC
"""
from __future__ import annotations

import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from typing import Callable, Literal

import numpy as np
from skyfield.api import load

from .propagation import (
    TrackingError,
    orbital_period_minutes,
    satellite_from_lines,
    tle_epoch_ms,
)

_LOG = logging.getLogger(__name__)

IdentifierKind = Literal["catnr", "intdes", "name"]

CELESTRAK_BASE = "https://celestrak.org/NORAD/elements/gp.php"
SPACETRACK_BASE = "https://www.space-track.org"
HTTP_TIMEOUT_S = 10.0
USER_AGENT = "MAVERIC-GSS-tle-fetch/1.0"

SEED_NORAD = "99999"          # pre-launch placeholder; never authoritative
MIN_PERIOD_MIN = 85.0         # LEO sanity band
MAX_PERIOD_MIN = 130.0
MAX_EPOCH_AGE_DAYS = 14.0

_CATNR_RE = re.compile(r"^\d{1,9}$")
_INTDES_RE = re.compile(r"^\d{4}-\d{3}[A-Z]{0,3}$")
_REJECT_PREFIXES = ("INVALID QUERY", "NO GP DATA FOUND", "<!DOCTYPE", "<HTML", "ERROR")

_TIMESCALE = load.timescale()

HttpOpener = Callable[..., object]


def detect_identifier(value: str) -> tuple[IdentifierKind, str]:
    text = (value or "").strip()
    if _CATNR_RE.match(text):
        return "catnr", text
    upped = text.upper()
    if _INTDES_RE.match(upped):
        return "intdes", upped
    return "name", text


def celestrak_url(kind: IdentifierKind, value: str) -> str:
    param = {"catnr": "CATNR", "intdes": "INTDES", "name": "NAME"}[kind]
    query = urllib.parse.urlencode({param: value, "FORMAT": "TLE"})
    return f"{CELESTRAK_BASE}?{query}"


def parse_tle_blocks(text: str) -> list[tuple[str, str, str]]:
    head = text.lstrip()[:40].upper()
    if any(head.startswith(p) for p in _REJECT_PREFIXES):
        return []
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out: list[tuple[str, str, str]] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
            out.append(("", lines[i], lines[i + 1]))
            i += 2
        elif (i + 2 < len(lines)
              and lines[i + 1].startswith("1 ")
              and lines[i + 2].startswith("2 ")):
            out.append((lines[i].strip(), lines[i + 1], lines[i + 2]))
            i += 3
        else:
            i += 1
    return out


def validate_tle(name: str, line1: str, line2: str, *, now_ms: int) -> int:
    """Return tle_epoch_ms if the TLE is propagable, LEO, fresh, and not the
    pre-launch seed; raise TrackingError otherwise."""
    norad = (line1 or "")[2:7].strip()
    if norad == SEED_NORAD:
        raise TrackingError("refusing pre-launch seed TLE (NORAD 99999)")
    sat = satellite_from_lines(name or "SAT", line1, line2)
    epoch_ms = tle_epoch_ms(sat)
    for t_ms in (now_ms, epoch_ms):
        t = _TIMESCALE.from_datetime(datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc))
        geo = sat.at(t)
        if getattr(geo, "message", None):
            raise TrackingError(f"TLE propagation error: {geo.message}")
        if not np.all(np.isfinite(np.asarray(geo.position.km, dtype=float))):
            raise TrackingError("TLE propagation produced non-finite position")
    period = orbital_period_minutes(sat)
    if not (MIN_PERIOD_MIN <= period <= MAX_PERIOD_MIN):
        raise TrackingError(f"orbital period {period:.1f} min outside LEO band")
    age_days = abs(now_ms - epoch_ms) / 86_400_000.0
    if age_days > MAX_EPOCH_AGE_DAYS:
        raise TrackingError(f"TLE epoch age {age_days:.1f} d exceeds {MAX_EPOCH_AGE_DAYS:.0f} d")
    return epoch_ms
