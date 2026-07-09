"""Shared git + subprocess + re-exec helpers for the updater.

Stdlib-only. Used by check.py, apply.py, and bootstrap.py.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .status import (
    DEFAULT_BRANCH,
    REPO_ROOT,
    REPO_URL,
    SubprocessFailed,
    _ALL_PIP_MODULES,
)


def _run_git(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    # GIT_TERMINAL_PROMPT=0 prevents the credential helper from blocking on an
    # interactive prompt when a private repo needs auth — we want a fast,
    # capturable failure, not a hung subprocess.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _ensure_git_repo(timeout_s: float) -> Optional[str]:
    """Auto-heal a zip-extracted tree into a real clone of REPO_URL.

    GitHub's "Download ZIP" produces a source snapshot with no .git directory,
    so the updater's git commands fail downstream with "not a git repository"
    and the preflight row renders as "could not reach origin". This helper
    detects that state and bootstraps a working clone in place: init, add
    origin, fetch DEFAULT_BRANCH, then force-checkout so the local branch
    tracks origin. Tracked files are overwritten (they match the zip anyway
    on first launch, or get updated to latest if the zip is stale). Untracked
    operator files (gss.yml, commands.yml, .mav_dev, logs) are gitignored
    and therefore absent from origin/main's tree, so they are preserved.

    If a prior run left a partial .git (e.g., init succeeded but checkout
    bailed on untracked files), rev-parse HEAD will fail — in that case we
    wipe .git and retry the full sequence rather than silently skipping.

    Returns None on success or when .git already resolves HEAD (idempotent).
    On any failure returns a short error string suitable for
    UpdateStatus.fetch_error.
    """
    git_dir = REPO_ROOT / ".git"
    if git_dir.exists():
        try:
            r = _run_git(["rev-parse", "HEAD"], timeout=5.0)
            if r.returncode == 0:
                return None
        except Exception:
            pass
        try:
            shutil.rmtree(git_dir)
        except Exception as exc:
            return f"could not remove partial .git: {exc}"

    # --force on checkout bypasses the "untracked working tree files would
    # be overwritten" guard. Necessary here because the zip's files are all
    # untracked until the first commit lands, and they collide with every
    # path in origin/main's tree.
    steps: list[tuple[list[str], float]] = [
        (["init"], 5.0),
        (["remote", "add", "origin", REPO_URL], 5.0),
        (["fetch", "origin", DEFAULT_BRANCH], timeout_s),
        (["checkout", "--force", "-B", DEFAULT_BRANCH, f"origin/{DEFAULT_BRANCH}"], 10.0),
        (["branch", f"--set-upstream-to=origin/{DEFAULT_BRANCH}", DEFAULT_BRANCH], 5.0),
    ]
    for args, tmo in steps:
        try:
            r = _run_git(args, timeout=tmo)
        except subprocess.TimeoutExpired:
            return f"git {args[0]} timeout during repo init"
        except Exception as exc:
            return f"git {args[0]} failed: {exc}"
        if r.returncode != 0:
            stderr = (r.stderr or "").strip()
            return f"git {args[0]} failed: {stderr or 'non-zero exit'}"
    return None


def _clean_dist_strays() -> None:
    """Delete untracked files under `mav_gss_lib/web/dist/assets/`.

    Vite writes content-hashed bundle filenames (e.g. `index-Djcvb32_.js`).
    When a remote commit changes the hash, `git pull --ff-only` on a machine
    that happens to have a differently-hashed leftover refuses to proceed,
    and even when it does, the stray file leaves `git status --porcelain`
    non-empty — which flips `working_tree_dirty` and disables the APPLY
    UPDATE button ("commit or stash local changes to enable").

    `dist/assets/` is purely build output — nothing operator-editable lives
    there — so pruning strays is always safe. `git clean -f` only touches
    untracked files; tracked and modified files are left alone. Scope is
    tight to `dist/assets/` so this can't reach anything else.

    Silent no-op on any failure: the caller still runs its dirty check
    afterwards, so a failed clean just surfaces as "dirty" as before.
    """
    try:
        _run_git(["clean", "-f", "mav_gss_lib/web/dist/assets/"], timeout=5.0)
    except Exception:
        pass


def _scan_missing_pip_deps() -> list[str]:
    """Return the subset of _ALL_PIP_MODULES currently not importable.

    This is the runtime drift detector — it scans the full requirements.txt set,
    not just the bootstrap-critical four, so non-critical package drift (e.g.,
    `crcmod` deleted from site-packages) is still surfaced in the updates check.
    Bootstrap uses _CRITICAL_MODULES directly for a narrower pre-FastAPI check.
    """
    missing: list[str] = []
    for mod in _ALL_PIP_MODULES:
        if importlib.util.find_spec(mod) is None:
            missing.append(mod)
    return missing


def _stream_subprocess(
    cmd: list[str],
    broadcast: Callable[[dict], None],
    phase: str,
    timeout: float,
) -> None:
    """Run subprocess, stream stdout line-by-line, coalesce broadcasts to <=1/s.

    NEVER broadcasts 'ok' or 'fail' — only 'running' updates. Caller is
    responsible for terminal state broadcasts.

    Wall-clock timeout protection: a daemon watchdog thread kills the process
    if it runs longer than `timeout` seconds. This is required because
    `for line in proc.stdout` blocks indefinitely when the subprocess produces
    no output — an in-loop time check cannot fire inside a blocking read.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    timed_out = threading.Event()

    def _watchdog() -> None:
        if proc.poll() is None:
            timed_out.set()
            try:
                proc.kill()
            except Exception:
                pass

    watchdog = threading.Timer(timeout, _watchdog)
    watchdog.daemon = True
    watchdog.start()

    stderr_tail: list[str] = []
    last_broadcast = 0.0
    latest_line = ""

    assert proc.stdout is not None
    try:
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                print(line, flush=True)
                stderr_tail.append(line)
                if len(stderr_tail) > 200:
                    stderr_tail = stderr_tail[-200:]
                latest_line = line
                now = time.monotonic()
                if now - last_broadcast >= 1.0:
                    last_broadcast = now
                    try:
                        broadcast({
                            "type": "update_phase",
                            "phase": phase,
                            "status": "running",
                            "detail": latest_line,
                        })
                    except Exception:
                        pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

        proc.wait()
    finally:
        watchdog.cancel()

    if timed_out.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout)

    if proc.returncode == 0:
        return

    tail = "\n".join(stderr_tail[-10:])
    raise SubprocessFailed(tail or f"{cmd[0]} exited with code {proc.returncode}")


def _reexec(
    python: "str | Path | None" = None,
    extra_env: Optional[dict[str, str]] = None,
    argv: "Optional[list[str]]" = None,
) -> None:
    """Replace current process with a fresh invocation of MAV_WEB.py. Never returns.

    `argv` overrides the re-executed script argv (defaults to sys.argv) —
    the mission switcher uses it to swap the `--mission` flag.
    """
    if extra_env:
        for k, v in extra_env.items():
            os.environ[k] = v
    interpreter = str(python) if python is not None else sys.executable
    script_argv = list(argv) if argv is not None else sys.argv
    os.execv(interpreter, [interpreter, *script_argv])
