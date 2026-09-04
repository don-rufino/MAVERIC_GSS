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


def _active_gss_path() -> Path:
    """Operator config path for the active mission (GSS_MISSION env).

    maveric (or unset) keeps the legacy gss.yml; any other mission gets its
    own gss.<id>.yml so per-mission platform state (frequencies, TLE, radio
    script) never cross-contaminates between missions.
    """
    mission = os.environ.get("GSS_MISSION", "").strip()
    if mission and mission != DEFAULT_MISSION:
        return _LIB_DIR / f"gss.{mission}.yml"
    return _DEFAULT_GSS_PATH


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
        "verifiers_enabled": True,
    },
    "rx": {
        "zmq_addr": DEFAULT_RX_ZMQ_ADDR,
        "tx_blackout_ms": 0,
    },
    "radio": {
        # No default `script` here: the flowgraph is mission-specific, so
        # each mission's build() seeds its own (MAVERIC -> MAV_DUO.py via the
        # RadioService DEFAULT_RADIO_SCRIPT fallback, astrocast -> MAV_ASTROCAST.py).
        # A platform default would pre-fill the key and defeat a mission's
        # setdefault, so a non-MAVERIC mission could never override it.
        "enabled": True,
        "autostart": False,
        "log_lines": 1000,
        "stop_timeout_s": 30.0,
        # Gate for the flowgraph's SigMF IQ recorder; RadioService injects it
        # as GSS_IQ_RECORD (destination GSS_IQ_DIR = <log_dir>/iq) at start.
        "iq_record": False,
        # Diagnostic raw 1 Msps pre-decimation capture (GSS_IQ_RAW_RECORD,
        # ~8 MB/s, nominal 50 GB maximum). For impulse/QRM analysis the
        # post-FIR recording cannot support.
        "iq_raw_record": False,
        # Minimum free space retained when either IQ recorder is enabled.
        # Radio startup reduces the requested 8/50 GB caps as necessary;
        # capture may stop early, but decoding remains live.
        "iq_disk_reserve_gb": 10.0,
        # Boot gain for the flowgraph RX chain (GSS_RX_GAIN). The GUI slider
        # still allows live override; this makes the ops value reproducible
        # instead of a per-pass manual step.
        "rx_gain": 40,
        # RX channel half-bandwidth in Hz (GSS_RX_BW_HZ): the flowgraph
        # rebuilds both the channel-filter cutoff and the GUI display span
        # from it at radio start. 50e3 = the mandated +/-50 kHz (100 kHz-wide
        # channel).
        "rx_bw": 50_000.0,
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
            # RadioService injects these into the MAV_DUO child process as
            # GSS_RX_LO_OFFSET_HZ / GSS_TX_LO_OFFSET_HZ; the flowgraph's
            # literal fallbacks apply only when it is launched by hand.
            "rx_lo_offset_hz": 250_000.0,
            "tx_lo_offset_hz": -400_000.0,
            # How often *background* tracking_sample events (az/el, requested
            # Doppler frequencies, source="tick") get written to the session
            # log. Every RX decode and TX attempt always gets its own exact
            # sample regardless of this setting — see
            # RxProjectionRunner._log_rx_tracking_sample and
            # TxService._log_tx_tracking_sample — so "off" only removes the
            # dead-air trace between those, not the RX/TX capture itself.
            # "off" (default): no background samples at all. "tick": logs
            # every 1 Hz tick. "tx_throttled": a decimated background rate
            # every log_decimation_s, opt in per session when chasing
            # something like a frequency-offset investigation where the
            # gaps between packets matter too.
            "log_cadence": "off",
            "log_decimation_s": 5.0,
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
    explicit_path = path is not None
    if path is None:
        path = str(_active_gss_path())
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
    radio_cfg = platform_cfg.get("radio")
    if isinstance(radio_cfg, dict) and radio_cfg.get("stop_timeout_s") == 8.0:
        # The former default can kill a first-run Matplotlib waterfall render
        # while its font cache is being built. Migrate persisted old defaults
        # in memory; the next ordinary config save writes the new value back.
        radio_cfg["stop_timeout_s"] = _DEFAULTS["radio"]["stop_timeout_s"]
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
    forced_mission = os.environ.get("GSS_MISSION", "").strip()
    if explicit_path:
        # Tools/tests targeting a specific file honor that file's mission.id.
        mission_id = forced_mission or str(mission_section.get("id") or DEFAULT_MISSION)
    else:
        # Real launches: the active mission is GSS_MISSION (set by the mission
        # switcher / --mission) or MAVERIC. gss.yml's mission.id does NOT
        # select a non-default mission — otherwise a hand-edited mission.id
        # would run e.g. astrocast out of gss.yml, and the first config save
        # (a TLE fetch, a settings edit) would persist that mission's
        # csp-less config over gss.yml and silently drop MAVERIC's routing.
        # Non-MAVERIC missions get their own gss.<id>.yml via GSS_MISSION.
        mission_id = forced_mission or DEFAULT_MISSION
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
    radio = persistable_platform.get("radio")
    if isinstance(radio, dict):
        # Runtime-derived from mission identity; never persist a profile that
        # can become stale when this file is copied or the mission changes.
        radio.pop("decoder_yml", None)
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


def get_tracking_control(platform_cfg: dict) -> dict:
    """Live tracking.control merged over the canonical defaults, type-coerced.

    The single reader for the control block: every key in
    _DEFAULTS["tracking"]["control"] flows through automatically, so a new
    control key can never be silently dropped by a hand-written whitelist
    and fallback values cannot diverge from the defaults.
    """
    control = (platform_cfg.get("tracking") or {}).get("control") or {}
    merged: dict = {}
    for key, default in _DEFAULTS["tracking"]["control"].items():
        try:
            merged[key] = type(default)(control.get(key, default))
        except (TypeError, ValueError):
            # A malformed value must degrade to the canonical default, not
            # raise: the 1 Hz doppler tick loop reads this and a raised
            # exception there kills the task for the rest of the session.
            merged[key] = default
    return merged


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
    """Return the on-disk path for the active operator config (used by /api/selfcheck)."""
    return _active_gss_path()


def save_operator_config(cfg: dict, path: str | None = None) -> None:
    """Atomically write current config back to YAML.

    Writes to a temp file first, then renames — prevents truncated files
    if the process is killed mid-write.
    """
    if path is None:
        path = str(_active_gss_path())
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
