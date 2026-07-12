"""Shared helpers for MAVERIC operations-focused tests."""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CODE_DIR = TESTS_DIR.parent
ROOT_DIR = CODE_DIR.parent
sys.path.insert(0, str(CODE_DIR))

from mav_gss_lib import config as _config_module
from mav_gss_lib.platform.loader import load_mission_spec_from_split

# Cached on first attribute access (PEP 562). Tests that import
# CMD_DEFS / NODES trigger the load lazily, so importing this module has no
# side effects and doesn't require gss.yml to exist.
#
# IMPORTANT: loaders are dereferenced via module attributes every call
# instead of being aliased with a
# `from … import …`. That keeps monkey-patching from tests working —
# `from X import foo` would bake a local name at THIS module's import
# time and would silently shadow any later `X.foo = fake`.
#
# To INVALIDATE the cache after monkey-patching a loader (e.g. in a test
# that wants to observe a patched load_split_config), drop this module
# from sys.modules and re-import:
#
#     del sys.modules["ops_test_support"]
#     import ops_test_support  # fresh module, empty _cache
#
# The test TestOpsTestSupportImportIsSideEffectFree follows this pattern.
_cache: dict = {}


def _load() -> dict:
    if "cfg" not in _cache:
        platform_cfg, mission_id, mission_cfg = _config_module.load_split_config()
        # Mission defaults are seeded inside the mission's own build(ctx)
        # when load_mission_spec_from_split invokes it.
        spec = load_mission_spec_from_split(platform_cfg, mission_id, mission_cfg)
        commands = spec.commands
        _cache["cmd_defs"] = commands.schema() if commands is not None else {}
        _cache["nodes"] = getattr(commands, "nodes", None)
    return _cache


def __getattr__(name: str):
    # PEP 562 — invoked only for attributes that are NOT already defined
    # at module scope (i.e. CMD_DEFS, NODES).
    if name == "CMD_DEFS":
        return _load()["cmd_defs"]
    if name == "NODES":
        return _load()["nodes"]
    raise AttributeError(f"module 'ops_test_support' has no attribute {name!r}")


def __dir__() -> list[str]:
    # PEP 562 companion — makes `dir(ops_test_support)` include the lazy
    # attributes so tab-completion, hasattr(), and debugger introspection
    # behave as if the attributes were statically defined.
    return sorted(set(globals()) | {"CMD_DEFS", "NODES"})


# GNU Radio decode loopback coverage lives in tests/test_decode_loopback.py
# (gated behind MAVERIC_FULL_GR=1). The former decode_golay_via_* helpers
# here had no callers, ran at a non-production 1.92 Msps, and turned every
# failure mode into a skipped test.
__all__ = [
    "CMD_DEFS", "NODES",
    "TESTS_DIR", "CODE_DIR", "ROOT_DIR",
]
