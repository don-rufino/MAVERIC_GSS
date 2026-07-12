"""Platform + mission config update appliers.

The allowlist specs live alongside (`.spec` for platform,
`..contract.mission.MissionConfigSpec` for mission). This module holds the
pure functions that merge an incoming update into an existing config,
respecting those specs.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import copy
from typing import Any

from ..contract.mission import MissionConfigSpec
from .spec import DEFAULT_PLATFORM_CONFIG_SPEC, PlatformConfigSpec

_NON_EDITABLE_SECTION_KEYS: dict[str, frozenset[str]] = {
    "tx": frozenset({"uplink_mode"}),
    # Selected by the active mission builder.  Keeping this out of operator
    # updates prevents a config PUT from silently restarting a mission with
    # another mission's decoder database.
    "radio": frozenset({"decoder_yml"}),
}


def apply_platform_config_update(
    platform_cfg: dict[str, Any],
    update: dict[str, Any],
    spec: PlatformConfigSpec = DEFAULT_PLATFORM_CONFIG_SPEC,
) -> None:
    """Apply a pre-split platform update to `platform_cfg` in place.

    Sections matching `spec.editable_sections` are deep-merged; the
    `general` bucket is whitelist-filtered by `spec.editable_general_keys`
    so stray runtime-derived or mission-only keys cannot sneak in.
    """
    for key in spec.editable_sections:
        value = update.get(key)
        if isinstance(value, dict):
            dst = platform_cfg.setdefault(key, {})
            _deep_merge_inplace(dst, _without_non_editable_keys(key, value))
    general_update = update.get("general")
    if isinstance(general_update, dict):
        dst_general = platform_cfg.setdefault("general", {})
        for gkey, gvalue in general_update.items():
            if gkey in spec.editable_general_keys:
                dst_general[gkey] = gvalue


def _without_non_editable_keys(
    section: str, value: dict[str, Any]
) -> dict[str, Any]:
    non_editable = _NON_EDITABLE_SECTION_KEYS.get(section)
    if not non_editable:
        return value
    return {k: v for k, v in value.items() if k not in non_editable}


def _deep_merge_inplace(base: dict, override: dict) -> None:
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge_inplace(base[k], v)
        else:
            base[k] = copy.deepcopy(v) if isinstance(v, (dict, list)) else v


def persist_mission_config(mission_cfg: dict[str, Any], spec: MissionConfigSpec) -> dict[str, Any]:
    """Return a new dict containing only operator-editable paths from `mission_cfg`.

    Mission identity constants (nodes, ptypes, mission_name, UI titles, etc.)
    live in the mission package (e.g. `defaults.py`) and are seeded at every
    `build(ctx)`. Persisting them would let a snapshot on disk silently
    override the code defaults — so this filter strips everything that isn't
    declared in `spec.editable_paths`. Wildcard paths (`ax25.*`) persist the
    full subtree at that prefix; leaf paths persist only that key.
    """

    out: dict[str, Any] = {}
    for path in spec.editable_paths:
        if path.endswith(".*"):
            prefix = path[:-2]
            value = _get_path(mission_cfg, prefix)
            if value is not None:
                _set_path(out, prefix, copy.deepcopy(value))
        else:
            value = _get_path(mission_cfg, path)
            if value is not None:
                _set_path(out, path, copy.deepcopy(value))
    return out


def _get_path(cfg: dict[str, Any], path: str) -> Any:
    cur: Any = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_path(cfg: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = cfg
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def apply_mission_config_update(
    current: dict[str, Any],
    update: dict[str, Any],
    spec: MissionConfigSpec,
) -> dict[str, Any]:
    """Return a new mission config with allowed update paths applied.

    Mission config is opaque to the platform except for mission-declared
    editability/protection paths. Paths use dotted keys.
    """

    merged = copy.deepcopy(current)
    _apply_allowed(merged, update, spec, prefix="")
    return merged


def _apply_allowed(target: dict[str, Any], update: dict[str, Any], spec: MissionConfigSpec, *, prefix: str) -> None:
    for key, value in update.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if _is_protected(path, spec):
            continue
        if isinstance(value, dict):
            if not _may_descend(path, spec):
                continue
            existing = target.get(key)
            if not isinstance(existing, dict):
                existing = {}
                target[key] = existing
            _apply_allowed(existing, value, spec, prefix=path)
            continue
        if _is_editable(path, spec):
            target[key] = copy.deepcopy(value)


def _is_protected(path: str, spec: MissionConfigSpec) -> bool:
    return path in spec.protected_paths or any(path.startswith(p + ".") for p in spec.protected_paths)


def _is_editable(path: str, spec: MissionConfigSpec) -> bool:
    if path in spec.editable_paths:
        return True
    for declared in spec.editable_paths:
        if declared.endswith(".*"):
            prefix = declared[:-2]
            if path == prefix or path.startswith(prefix + "."):
                return True
    return False


def _may_descend(path: str, spec: MissionConfigSpec) -> bool:
    for declared in spec.editable_paths:
        if declared.endswith(".*"):
            prefix = declared[:-2]
            if path == prefix or path.startswith(prefix + "."):
                return True
            if prefix.startswith(path + "."):
                return True
            continue
        if declared.startswith(path + "."):
            return True
    return False
