"""SCION-X beacon telemetry from the local field map.

The beacon layout (field names, offsets, types) is team-supplied and kept
out of the repository: it is read at runtime from ``beacon_fields.json``
(gitignored; build it with ``build_beacon_fields``). Without the file the
mission still receives and logs every frame, it just extracts no values.

Frames usually arrive without their start (late key-up), so values are
produced only for fields whose bytes were all received. Conversions and
plausibility limits are the ones validated against N6RFM's report:

  EPS bus voltages/currents (INA3221): value >> 3, 8 mV / 4 mA per LSB,
      low three bits must be zero; bus <= 26 V, powered rails within 15 %
      of the nominal in their name (3V3 -> 3.3 V), |I| <= 5 A
  fuel gauge: mV / mA; the 2S pack reads 5-9 V; SOC in 0.01 %
  one-byte temperatures: signed degC in -40..+85
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from mav_gss_lib.platform.contract.parameters import ParamUpdate

FIELDS_PATH = Path(__file__).resolve().parent / "beacon_fields.json"
INFO_OFFSET = 16                       # field offsets count from the APID (frame byte 16)
FRAME_BYTES = 274

_RAIL_RE = re.compile(r"(\d+(?:\.\d+)?)V(\d)?\b")
_DOMAINS = (
    ("hyperscan", "hyperscan"), ("seuv", "seuv"), ("aprs", "aprs"), ("cip", "cip"),
    ("xband", "xband"), ("uhf", "uhf"), ("eps", "eps"), ("adcs", "adcs"), ("rws", "adcs"),
    ("eci", "adcs"), ("rate sensor", "adcs"), ("angular", "adcs"), ("sun vector", "adcs"),
    ("sd ", "obc"), ("cmd", "obc"), ("fsw", "obc"), ("obc", "obc"),
)


@dataclass(frozen=True)
class BeaconField:
    name: str
    var: str
    ctype: str
    size: int
    offset: int
    endian: str

    @property
    def frame_byte(self) -> int:
        return INFO_OFFSET + self.offset

    @property
    def byteorder(self) -> str:
        return "big" if self.endian.lower().startswith("big") else "little"


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> tuple[BeaconField, ...]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return tuple(BeaconField(f["name"], f.get("var", f["name"]), f["ctype"], int(f["size"]),
                             int(f["offset"]), f.get("endian", "Little")) for f in data["fields"])


def load_fields(path: Path | None = None) -> tuple[BeaconField, ...]:
    """Field map, or () when the local file is absent/unreadable."""
    path = FIELDS_PATH if path is None else path
    try:
        return _load(str(path), path.stat().st_mtime)
    except (OSError, ValueError, KeyError):
        return ()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _domain(f: BeaconField) -> str:
    # an EPS_ prefix names the power system's own channel even when the
    # rail it feeds is another subsystem (an EPS UHF rail is EPS data)
    if f.name.upper().startswith("EPS_") or f.var.upper().startswith("EPS_"):
        return "eps"
    probe = f"{f.name} {f.var}".lower()
    for key, domain in _DOMAINS:
        if key in probe:
            return domain
    return "obc"


def param_names(fields: tuple[BeaconField, ...]) -> dict[BeaconField, str]:
    """Stable "<domain>.<key>" per field: the variable name when it is a
    single identifier, else the field name (bitfield rows list several
    variables); repeated rows (x/y/z, per-wheel) get their offset."""
    names: dict[BeaconField, str] = {}
    seen: dict[str, int] = {}
    for f in fields:
        base = f.var if "\n" not in f.var else f.name
        name = f"{_domain(f)}.{_slug(base)}"
        seen[name] = seen.get(name, 0) + 1
        names[f] = name
    for f in fields:
        if seen[names[f]] > 1:
            names[f] = f"{names[f]}_{f.offset}"
    return names


def rail_nominal_v(name: str) -> float | None:
    head = name.split(" ")[0]
    m = _RAIL_RE.search(head)
    if not m:
        return None
    whole = m.group(1)
    return float(f"{whole}.{m.group(2)}") if m.group(2) and "." not in whole else float(whole)


def convert(f: BeaconField, raw: bytes) -> tuple[Any, str, bool]:
    """(engineering value, unit, plausible) for one field's bytes."""
    n = f.name
    u = int.from_bytes(raw, f.byteorder)
    if f.ctype == "float" and f.size == 4:
        return struct.unpack("<f" if f.byteorder == "little" else ">f", raw)[0], "", True
    if n.startswith("EPS_") and "Gauge" not in n and n.endswith("Voltage"):
        v = round((u >> 3) * 0.008, 3)
        nominal = rail_nominal_v(n)
        ok = not (u & 0x07) and v <= 26.0 and not (nominal and v > 0.5 and abs(v - nominal) > 0.15 * nominal)
        return v, "V", ok
    if n.startswith("EPS_") and "Gauge" not in n and n.endswith("Current"):
        c = u >> 3
        c = c - 0x2000 if c & 0x1000 else c
        a = round(c * 0.004, 3)
        return a, "A", not (u & 0x07) and abs(a) <= 5.0
    if "(Gauge)" in n and "Voltage" in n:
        v = round(u / 1000, 3)
        return v, "V", 5.0 <= v <= 9.0
    if "(Gauge)" in n and "Current" in n:
        return round(int.from_bytes(raw, f.byteorder, signed=True) / 1000, 3), "A", True
    if "SOC" in n:
        return round(u / 100, 2), "%", u / 100 <= 100.0
    if "Temperature" in n and f.size == 1:
        t = int.from_bytes(raw, f.byteorder, signed=True)
        return t, "degC", -40 <= t <= 85
    signed = not f.ctype.startswith("u")
    return int.from_bytes(raw, f.byteorder, signed=signed), "", True


@dataclass
class BeaconTelemetry:
    params: list[ParamUpdate]
    fields_received: int
    fields_total: int
    implausible: list[str]


def decode(frame: bytes, start_byte: int, fields: tuple[BeaconField, ...] | None = None) -> BeaconTelemetry:
    """Values for every field fully inside frame[start_byte : start_byte+len]."""
    fields = load_fields() if fields is None else fields
    names = param_names(fields)
    params: list[ParamUpdate] = []
    implausible: list[str] = []
    end = start_byte + len(frame)
    for f in fields:
        a = f.frame_byte
        if a < start_byte or a + f.size > min(end, FRAME_BYTES - 2):
            continue
        raw = frame[a - start_byte:a - start_byte + f.size]
        value, unit, ok = convert(f, raw)
        if not ok:
            implausible.append(names[f])
        params.append(ParamUpdate(name=names[f], value=value, ts_ms=0, unit=unit))
    return BeaconTelemetry(params, len(params), len(fields), implausible)
