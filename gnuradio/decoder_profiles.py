"""Dependency-free decoder-profile selection for GNU Radio entry points."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


SYNCWORD_OPTIONS = "--syncword_threshold 6"
# The GSS uplink chain always ends in AX100 Mode 5 ASM+Golay framing.
UPLINK_FRAMING = "AX100 ASM+Golay"
DEFAULT_TX_BAUD = 9600


def resolve_mav_duo_decoder(
    script_dir: str | os.PathLike[str],
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Resolve MAV_DUO's exact decoder path and a log-friendly source.

    ``GSS_DECODER_YML`` is the deliberate standalone override. Otherwise the
    active mission must ship ``decoders/<MISSION>_DECODER.yml`` beside MAV_DUO.
    There is intentionally no cross-mission fallback.
    """
    env = os.environ if environ is None else environ
    explicit = (env.get("GSS_DECODER_YML") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"GSS_DECODER_YML does not exist: {path}")
        return str(path), "GSS_DECODER_YML"

    mission = (env.get("GSS_MISSION") or "maveric").strip().upper()
    path = Path(script_dir) / "decoders" / f"{mission}_DECODER.yml"
    if not path.is_file():
        raise FileNotFoundError(
            f"no decoder database for mission {mission.lower()!r}; expected {path}"
        )
    return str(path), f"mission {mission.lower()}"


def decoder_options(decoder_yml: str | os.PathLike[str]) -> str:
    """Return the gr-satellites options supported by this profile family.

    ``--syncword_threshold`` is registered only when the database contains an
    AX100 or FX.25 deframer. Passing it to a pure AX.25 profile aborts argument
    parsing, so the selected database must drive the options string.
    """
    try:
        text = Path(decoder_yml).read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        payload = line.split("#", 1)[0]
        if payload.strip().startswith("framing:") and (
            "AX100" in payload or "FX.25" in payload
        ):
            return SYNCWORD_OPTIONS
    return ""


def tx_baudrate(decoder_yml: str | os.PathLike[str]) -> int:
    """Return the TX modulator baud for the selected mission profile.

    The uplink rate is the first transmitter whose framing is the AX100
    ASM+Golay chain the GSS frames. Profiles without one (pure AX.25,
    Astrocast FX.25) never uplink and keep DEFAULT_TX_BAUD.
    """
    import yaml

    try:
        profile = yaml.safe_load(Path(decoder_yml).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return DEFAULT_TX_BAUD
    transmitters = profile.get("transmitters") if isinstance(profile, dict) else None
    for transmitter in (transmitters or {}).values():
        if isinstance(transmitter, dict) and transmitter.get("framing") == UPLINK_FRAMING:
            return int(transmitter["baudrate"])
    return DEFAULT_TX_BAUD
