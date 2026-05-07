"""Ephemeral mode — redirect every server-side disk-write path to a tempdir.

Activated by ``GSS_EPHEMERAL=1`` (or ``MAV_WEB.py --ephemeral``).
Used when an operator wants to drive a fake_flight test session through
the live UI without leaving session logs, downloaded files, queue
persistence, parameter cache, or accidental config edits in real
operations state.

What gets redirected:
- ``general.log_dir``                  -> ``<tmp>/logs`` (catches session
                                          JSONL, queue persistence, RX
                                          journal, parameter cache,
                                          verifier instances, downloaded
                                          files via ``data_dir = log_dir``)
- ``general.generated_commands_dir``   -> ``<tmp>/generated``

What gets blocked separately (via ``runtime.config_save_disabled``):
- ``gss.yml`` save-back from ``/api/config``
- ``mission.yml`` save-back from ``/api/config``

A ``shutil.rmtree`` cleanup is registered with ``atexit`` so the tempdir
disappears on process exit. Failures are silenced — best-effort.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable


def is_active() -> bool:
    """``True`` iff ``GSS_EPHEMERAL`` is set to a truthy value."""
    return os.environ.get("GSS_EPHEMERAL", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def apply(platform_cfg: dict) -> tuple[Path, Callable[[], None]]:
    """Redirect log-dir + generated-commands-dir to a fresh tempdir.

    Mutates ``platform_cfg["general"]`` in place. Returns
    ``(tmp_root, cleanup)`` where ``cleanup`` is also registered with
    ``atexit`` for automatic process-exit removal.

    Caller is responsible for setting ``runtime.config_save_disabled``
    so the redirected paths aren't written into the real ``gss.yml``
    on the next ``/api/config`` save.
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="gss-ephemeral-"))
    general = platform_cfg.setdefault("general", {})
    general["log_dir"] = str(tmp_root / "logs")
    general["generated_commands_dir"] = str(tmp_root / "generated")

    def cleanup() -> None:
        shutil.rmtree(tmp_root, ignore_errors=True)

    atexit.register(cleanup)
    return tmp_root, cleanup
