"""
mav_gss_lib.config -- Shared Configuration Loader

Reads gss.yml from the mav_gss_lib package directory and returns split
runtime state `(platform_cfg, mission_id, mission_cfg)`. Operator files must
use the native split-state `{platform, mission}` shape. Mission-specific
defaults are seeded by the active mission's own `build(ctx)` at MissionSpec
load time. Falls back to hardcoded platform defaults if the file is missing.

Author:  Irfan Annuar - USC ISI SERC
"""

import copy
import json
import os
import tempfile
from pathlib import Path

import yaml

from mav_gss_lib.constants import (
    DEFAULT_MISSION,
    DEFAULT_RX_ZMQ_ADDR,
    DEFAULT_TX_ZMQ_ADDR,
)

# Resolve library/project directories relative to this file, not CWD
_LIB_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_GSS_PATH = _LIB_DIR / "gss.yml"


def _read_version() -> str:
    """Single source of truth: web/package.json."""
    pkg_json = _LIB_DIR / "web" / "package.json"
    try:
        with open(pkg_json) as f:
            return json.load(f).get("version", "0.0.0")
    except (OSError, ValueError):
        return "0.0.0"


def _read_build_sha() -> str:
    """Short git SHA of the working tree, resolved at module import.

    Runtime-derived (not baked into the JS bundle), so a backend-only
    commit no longer dirties dist/. Returned via /api/config as
    general.build_sha and displayed on the preflight screen.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if result.returncode == 0:
            return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


_DEFAULTS = {
    "tx": {
        "zmq_addr":  DEFAULT_TX_ZMQ_ADDR,
        "delay_ms":  500,
    },
    "rx": {
        "zmq_addr": DEFAULT_RX_ZMQ_ADDR,
        "tx_blackout_ms": 0,
    },
    "radio": {
        "enabled": True,
        "autostart": False,
        "script": "gnuradio/MAV_DUO.py",
        "log_lines": 1000,
        "stop_timeout_s": 8.0,
    },
    "tracking": {
        "control": {
            "rx_zmq_addr": "tcp://127.0.0.1:52003",
            "tx_zmq_addr": "tcp://127.0.0.1:52004",
            "tick_period_s": 1.0,
            # Fixed RF-LO placement relative to each nominal frequency. Doppler
            # is applied as a DSP shift around these parked LOs so the AD9361
            # synthesizers are never retuned mid-pass (retunes + near-identical
            # RX/TX LO frequencies generate beat spurs and injection pulling).
            # Must match gnuradio/MAV_DUO's rx_lo_offset / tx_lo_offset
            # variables (separate process; no shared source possible).
            "rx_lo_offset_hz": 250_000.0,
            "tx_lo_offset_hz": -400_000.0,
        },
    },
    "general": {
        "mission":      DEFAULT_MISSION,
        "version":      _read_version(),
        "build_sha":    _read_build_sha(),
        "log_dir":      "logs",
        "generated_commands_dir": "generated_commands",
    },
    "auth": {
        "require_token_for_reads": False,
    },
    "stations": {},
}

_PLATFORM_GENERAL_KEYS = {"log_dir", "generated_commands_dir"}
_NATIVE_TOP_KEYS = {"platform", "mission"}


def deep_merge(base: dict, override: dict) -> dict:
    """Merge *override* into *base*, returning a new dict.

    Uses copy.deepcopy on *base* so the returned dict does not alias any
    nested dicts in *base* (important when base is _DEFAULTS — otherwise
    a later in-place mutation would corrupt module-level defaults).
    """
    merged = copy.deepcopy(base)
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = deep_merge(merged[k], v)
        else:
            merged[k] = copy.deepcopy(v) if isinstance(v, (dict, list)) else v
    return merged


def _native_operator_config(raw: dict, *, default_mission: str = DEFAULT_MISSION) -> dict:
    """Return a validated native `{platform, mission}` operator config."""
    if not raw:
        return {"platform": {}, "mission": {"id": default_mission, "config": {}}}
    extra_top_keys = set(raw) - _NATIVE_TOP_KEYS
    if extra_top_keys:
        keys = ", ".join(sorted(str(k) for k in extra_top_keys))
        raise ValueError(f"gss.yml has unsupported top-level key(s): {keys}")
    if "platform" not in raw and "mission" not in raw:
        raise ValueError("gss.yml must use native split shape: {platform, mission}")

    native = copy.deepcopy(raw)
    platform = native.get("platform", {})
    if platform is None:
        platform = {}
    if not isinstance(platform, dict):
        raise ValueError("gss.yml platform section must be a mapping")

    mission = native.get("mission", {})
    if mission is None:
        mission = {}
    if not isinstance(mission, dict):
        raise ValueError("gss.yml mission section must be a mapping")

    mission_config = mission.get("config", {})
    if mission_config is None:
        mission_config = {}
    if not isinstance(mission_config, dict):
        raise ValueError("gss.yml mission.config section must be a mapping")

    mission["id"] = str(mission.get("id") or default_mission)
    mission["config"] = mission_config
    native["platform"] = platform
    native["mission"] = mission
    return native


def load_split_config(path: str | None = None) -> tuple[dict, str, dict]:
    """Load operator config as native split state.

    Returns (platform_cfg, mission_id, mission_cfg) derived from the operator
    file and the platform defaults. Operator files must use the native
    `{platform, mission}` shape.
    """
    if path is None:
        path = str(_DEFAULT_GSS_PATH)
    raw = {}
    if os.path.isfile(path):
        with open(path, "r") as f:
            loaded = yaml.safe_load(f)
        if loaded is None:
            raw = {}
        elif isinstance(loaded, dict):
            raw = loaded
        else:
            raise ValueError("gss.yml must be a mapping")
    native = _native_operator_config(raw)

    platform_defaults = copy.deepcopy(_DEFAULTS)
    platform_defaults.pop("general", None)
    platform_cfg = deep_merge(platform_defaults, native.get("platform", {}))
    platform_general = platform_cfg.setdefault("general", {})
    defaults_general = copy.deepcopy(_DEFAULTS["general"])
    operator_general = native.get("platform", {}).get("general", {})
    if isinstance(operator_general, dict):
        defaults_general.update(operator_general)
    platform_general.update(defaults_general)
    platform_general.pop("mission", None)

    mission_section = native.get("mission", {})
    if not isinstance(mission_section, dict):
        mission_section = {}
    mission_id = str(mission_section.get("id") or _DEFAULTS["general"]["mission"])
    mission_cfg = copy.deepcopy(mission_section.get("config", {}))
    if not isinstance(mission_cfg, dict):
        mission_cfg = {}
    return platform_cfg, mission_id, mission_cfg


def split_to_persistable(platform_cfg: dict, mission_id: str, mission_cfg: dict) -> dict:
    """Convert runtime split state back into on-disk native operator shape.

    Filters platform.general down to the keys the operator is allowed to
    persist (strips runtime-derived version/build_sha/mission and any
    stray mission-general snapshots left in platform_cfg).
    """
    persistable_platform = copy.deepcopy(platform_cfg)
    tx = persistable_platform.get("tx")
    if isinstance(tx, dict):
        tx.pop("uplink_mode", None)
    general = persistable_platform.get("general")
    if isinstance(general, dict):
        persistable_platform["general"] = {
            key: value
            for key, value in general.items()
            if key in _PLATFORM_GENERAL_KEYS
        }
        if not persistable_platform["general"]:
            persistable_platform.pop("general", None)
    stations = persistable_platform.get("stations")
    if isinstance(stations, dict) and not stations:
        persistable_platform.pop("stations", None)
    auth = persistable_platform.get("auth")
    if isinstance(auth, dict) and not auth.get("require_token_for_reads"):
        persistable_platform.pop("auth", None)
    return {
        "platform": persistable_platform,
        "mission": {
            "id": mission_id,
            "config": copy.deepcopy(mission_cfg),
        },
    }


def resolve_project_path(path_value: str | Path, *, base_dir: str | Path | None = None) -> Path:
    """Resolve a config path relative to the chosen base directory when needed."""
    path = Path(path_value)
    if path.is_absolute():
        return path
    root = _PROJECT_ROOT if base_dir is None else Path(base_dir)
    return (root / path).resolve()


def get_rx_zmq_addr(cfg: dict) -> str:
    return cfg.get("rx", {}).get("zmq_addr", DEFAULT_RX_ZMQ_ADDR)


def get_tx_zmq_addr(cfg: dict) -> str:
    return cfg.get("tx", {}).get("zmq_addr", DEFAULT_TX_ZMQ_ADDR)


def get_generated_commands_dir(cfg: dict) -> Path:
    """Return the resolved import/export directory for queue JSONL files."""
    general = cfg.get("general", {})
    raw = general.get("generated_commands_dir", "generated_commands")
    return resolve_project_path(raw)


def get_operator_config_path() -> Path:
    """Return the on-disk path for the operator gss.yml (used by /api/selfcheck)."""
    return _DEFAULT_GSS_PATH


def save_operator_config(cfg: dict, path: str | None = None) -> None:
    """Atomically write current config back to YAML.

    Writes to a temp file first, then renames — prevents truncated files
    if the process is killed mid-write.
    """
    if path is None:
        path = str(_DEFAULT_GSS_PATH)
    dir_name = os.path.dirname(path) or "."
    try:
        prev_mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        prev_mode = 0o664
    fd, tmp = tempfile.mkstemp(suffix=".tmp", dir=dir_name)
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
        os.chmod(tmp, prev_mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
