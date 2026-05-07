"""Shared base class for RX/TX session logs.

`_BaseLog` owns the JSONL file I/O contract: background writer thread,
new-session swap (preflight + commit), atomic rename, and close-on-empty
cleanup. `SessionLog` extends this with event-specific convenience
methods.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import json
import logging as _logging
import os
import queue
import re
import sys
import threading
from datetime import datetime
from typing import Any

from mav_gss_lib.constants import DEFAULT_MISSION_NAME

_log = _logging.getLogger(__name__)


def _compose_log_paths(log_dir: str, prefix: str, tag: str,
                       station: str = "", operator: str = "") -> tuple[str, str]:
    """Return (jsonl_path, session_id) under log_dir/json.

    The session_id equals the file stem — callers use it to stamp every
    JSONL record so SQL ingest has a stable session key matching the
    filename on disk.

    *tag*, *station*, *operator* must be pre-sanitized — callers handle it."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    parts = [prefix, ts]
    if station:
        parts.append(station)
    if operator:
        parts.append(operator)
    if tag:
        parts.append(tag)
    name = "_".join(parts)
    return (
        os.path.join(log_dir, "json", f"{name}.jsonl"),
        name,
    )


class _BaseLog:
    """Shared JSONL log infrastructure.

    All file I/O runs on a dedicated background thread so that callers
    (the FastAPI lifespan + RX/TX async broadcast loops) never block on
    disk flushes. Once :meth:`close` has been called, subsequent
    ``write_jsonl`` calls become silent no-ops — the writer thread is
    gone, so queuing would otherwise lose the record.
    """

    _SENTINEL = None  # poison pill to stop the writer thread

    def __init__(
        self,
        log_dir: str,
        prefix: str,
        version: str,
        mode: str,
        zmq_addr: str,
        mission_name: str = DEFAULT_MISSION_NAME,
        *,
        mission_id: str = "",
        station: str = "",
        operator: str = "",
        host: str = "",
    ) -> None:
        self._log_dir = log_dir
        self._prefix = prefix
        self._version = version
        self._mode = mode
        self._zmq_addr = zmq_addr
        self._mission_name = mission_name
        self._mission_id = mission_id
        self._station = station
        self._operator = operator
        self._host = host
        self._q_lock = threading.Lock()
        self._closed = False
        self.session_id = ""
        os.makedirs(os.path.join(log_dir, "json"), exist_ok=True)
        self._open_files()

    @property
    def mission_id(self) -> str:
        """Active mission id stamped onto JSONL records by the platform builders."""
        return self._mission_id

    def set_zmq_addr(self, zmq_addr: str) -> None:
        """Update the ZMQ endpoint embedded in subsequent session headers."""
        self._zmq_addr = zmq_addr

    _FLUSH_EVERY_N = 64
    _FLUSH_EVERY_S = 0.5

    def _writer_loop(self) -> None:
        """Drain the write queue until sentinel, flushing in batches."""
        unflushed = 0
        while True:
            try:
                item = self._q.get(timeout=self._FLUSH_EVERY_S)
            except queue.Empty:
                if unflushed > 0:
                    self._flush_handles()
                    unflushed = 0
                continue

            if item is self._SENTINEL:
                while not self._q.empty():
                    try:
                        remaining = self._q.get_nowait()
                        if remaining is self._SENTINEL:
                            continue
                        self._process_item(remaining)
                    except Exception:
                        break
                self._flush_handles()
                break

            try:
                self._process_item(item)
            except Exception as e:
                print(f"WARNING: log write failed ({e}), continuing", file=sys.stderr)
                continue
            unflushed += 1
            if unflushed >= self._FLUSH_EVERY_N:
                self._flush_handles()
                unflushed = 0

    def _flush_handles(self) -> None:
        try:
            self._jsonl_f.flush()
        except (ValueError, OSError):
            pass

    def _process_item(self, item: tuple[str, Any]) -> None:
        kind, data = item
        if kind == "jsonl":
            self._jsonl_f.write(data)

    def write_jsonl(self, record: dict[str, Any]) -> None:
        """Queue one pre-built envelope dict for the writer thread to persist.

        Becomes a no-op after :meth:`close` — queuing would silently lose the
        record because the writer thread has already exited.
        """
        with self._q_lock:
            if self._closed:
                return
            self._q.put(("jsonl", json.dumps(record) + "\n"))

    def _open_files(self, tag: str = "") -> None:
        """Open new log file with fresh timestamp, start writer thread."""
        tag      = re.sub(r'[^\w\-.]', '_', tag.strip()).strip('_') if tag else ""
        station  = re.sub(r'[^\w\-.]', '_', self._station.strip()).strip('_') if self._station else ""
        operator = re.sub(r'[^\w\-.]', '_', self._operator.strip()).strip('_') if self._operator else ""
        self.jsonl_path, self.session_id = _compose_log_paths(
            self._log_dir, self._prefix, tag, station=station, operator=operator,
        )
        self._jsonl_f = open(self.jsonl_path, "a")
        self._q = queue.Queue()
        self._writer = threading.Thread(target=self._writer_loop, name="log-writer", daemon=True)
        self._writer.start()

    def prepare_new_session(self, tag: str = "") -> dict[str, Any]:
        """Open new log file WITHOUT closing old one (prepare phase)."""
        if self._closed:
            raise RuntimeError("cannot start new session on a closed log")
        tag      = re.sub(r'[^\w\-.]', '_', tag.strip()).strip('_') if tag else ""
        station  = re.sub(r'[^\w\-.]', '_', self._station.strip()).strip('_') if self._station else ""
        operator = re.sub(r'[^\w\-.]', '_', self._operator.strip()).strip('_') if self._operator else ""
        new_jsonl_path, new_session_id = _compose_log_paths(
            self._log_dir, self._prefix, tag, station=station, operator=operator,
        )
        new_jsonl_f = None
        try:
            os.makedirs(os.path.join(self._log_dir, "json"), exist_ok=True)
            new_jsonl_f = open(new_jsonl_path, "a")
        except Exception:
            if new_jsonl_f:
                try: new_jsonl_f.close()
                except Exception: pass
            try:
                if os.path.isfile(new_jsonl_path):
                    os.remove(new_jsonl_path)
            except OSError:
                pass
            raise
        return {
            "jsonl_path": new_jsonl_path,
            "session_id": new_session_id,
            "jsonl_f": new_jsonl_f,
        }

    def commit_new_session(self, prepared: dict[str, Any]) -> None:
        """Swap to new file handle from *prepared* dict (commit phase)."""
        if self._closed:
            try:
                prepared["jsonl_f"].close()
            except Exception:
                pass
            raise RuntimeError("cannot commit new session on a closed log")
        old_jsonl = self.jsonl_path
        with self._q_lock:
            self._q.put(self._SENTINEL)
        self._writer.join(timeout=5.0)
        if self._writer.is_alive():
            try:
                prepared["jsonl_f"].close()
            except Exception:
                pass
            path = prepared.get("jsonl_path")
            try:
                if path and os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
            raise RuntimeError("old writer thread did not stop within timeout — commit aborted")
        self._jsonl_f.close()
        try:
            if os.path.isfile(old_jsonl) and os.path.getsize(old_jsonl) == 0:
                os.remove(old_jsonl)
        except OSError:
            pass
        self.jsonl_path = prepared["jsonl_path"]
        self.session_id = prepared["session_id"]
        self._jsonl_f = prepared["jsonl_f"]
        self._q = queue.Queue()
        self._writer = threading.Thread(target=self._writer_loop,
                                        name="log-writer", daemon=True)
        self._writer.start()

    def compute_rename_paths(self, tag: str) -> str | None:
        """Compute new file path for a rename operation. Returns new_jsonl."""
        tag = re.sub(r'[^\w\-.]', '_', tag.strip()).strip('_')
        if not tag:
            return None
        base, ext = os.path.splitext(self.jsonl_path)
        return f"{base}_{tag}{ext}"

    def rename_preflight(self, tag: str) -> str:
        """Check that the rename target does not already exist.

        Returns new_jsonl on success or raises FileExistsError.
        """
        new_jsonl = self.compute_rename_paths(tag)
        if new_jsonl is None:
            raise ValueError("empty tag after sanitization")
        if os.path.exists(new_jsonl):
            raise FileExistsError(f"target already exists: {new_jsonl}")
        return new_jsonl

    def prepare_rename(self, tag: str) -> dict[str, Any]:
        """Phase 1: write the rewritten content to a sidecar file at the
        new path. Old file and writer state are untouched. Returns a dict
        the caller passes verbatim to ``commit_rename`` or ``rollback_rename``.

        On any IOError the sidecar is cleaned up and the exception
        propagates; the log remains fully functional with the old path.
        """
        tag = re.sub(r'[^\w\-.]', '_', tag.strip()).strip('_')
        if not tag:
            raise ValueError("empty tag after sanitization")
        old_jsonl = self.jsonl_path
        base, ext = os.path.splitext(old_jsonl)
        new_jsonl = f"{base}_{tag}{ext}"
        if os.path.exists(new_jsonl):
            raise FileExistsError(new_jsonl)
        new_session_id = os.path.splitext(os.path.basename(new_jsonl))[0]

        # Drain the writer so old_jsonl reflects every accepted write.
        # Hold _q_lock so no concurrent write_jsonl slips a record in
        # after the sentinel.
        with self._q_lock:
            self._q.put(self._SENTINEL)
            self._writer.join(timeout=5.0)
            if self._writer.is_alive():
                # Writer is genuinely hung — we can't safely cancel a
                # Python thread. Mark the log fatally closed so future
                # write_jsonl calls become silent no-ops (matches the
                # post-close() semantics already in _BaseLog) and
                # surface the failure to the caller.
                self._closed = True
                try: self._jsonl_f.close()
                except Exception: pass
                raise RuntimeError("writer did not stop within timeout — log marked closed")
            self._jsonl_f.close()

            try:
                # Stream-rewrite old_jsonl → new_jsonl with replaced session_id.
                with open(old_jsonl, "r") as src, open(new_jsonl, "w") as dst:
                    for line in src:
                        line = line.rstrip("\n")
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            if isinstance(rec, dict) and "session_id" in rec:
                                rec["session_id"] = new_session_id
                            dst.write(json.dumps(rec) + "\n")
                        except Exception:
                            dst.write(line + "\n")
            except Exception:
                # Sidecar incomplete — clean up, restart writer on old file,
                # let the caller see the exception.
                try: os.remove(new_jsonl)
                except FileNotFoundError: pass
                self._jsonl_f = open(old_jsonl, "a")
                self._restart_writer_locked()
                raise

            # Restart the writer on the OLD file — until commit_rename runs
            # the old file is still canonical.
            self._jsonl_f = open(old_jsonl, "a")
            self._restart_writer_locked()

        return {
            "old_jsonl_path": old_jsonl,
            "new_jsonl_path": new_jsonl,
            "old_session_id": self.session_id,
            "new_session_id": new_session_id,
        }

    def commit_rename(self, prepared: dict[str, Any]) -> None:
        """Phase 2: swap to the new file. Re-runs the content rewrite to
        capture any records written between prepare and commit, then
        atomically replaces the sidecar with the merged content, opens
        the new writer handle, and only THEN deletes the old file.

        Failure handling: normal IO failures recover to a live writer on
        whichever file still exists (preferring old). Writer-stop timeout
        is fatal — log marked _closed; future write_jsonl is no-op.
        """
        old_jsonl = prepared["old_jsonl_path"]
        new_jsonl = prepared["new_jsonl_path"]
        new_session_id = prepared["new_session_id"]
        tmp_path = new_jsonl + ".tmp"

        with self._q_lock:
            self._q.put(self._SENTINEL)
            self._writer.join(timeout=5.0)
            if self._writer.is_alive():
                self._closed = True
                try: self._jsonl_f.close()
                except Exception: pass
                raise RuntimeError("writer did not stop within timeout — log marked closed")
            self._jsonl_f.close()

            new_handle = None
            try:
                # 1. Re-rewrite into a temp, atomic-replace.
                with open(old_jsonl, "r") as src, open(tmp_path, "w") as dst:
                    for line in src:
                        line = line.rstrip("\n")
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            if isinstance(rec, dict) and "session_id" in rec:
                                rec["session_id"] = new_session_id
                            dst.write(json.dumps(rec) + "\n")
                        except Exception:
                            dst.write(line + "\n")
                os.replace(tmp_path, new_jsonl)

                # 2. Prove the new handle is openable BEFORE deleting old.
                new_handle = open(new_jsonl, "a")

                # 3. Now safe to remove the old file — new handle is live.
                try:
                    os.remove(old_jsonl)
                except FileNotFoundError:
                    pass

                # 4. Commit: swap state + start writer on new handle.
                self.jsonl_path = new_jsonl
                self.session_id = new_session_id
                self._jsonl_f = new_handle
                new_handle = None  # ownership transferred
                self._restart_writer_locked()
            except Exception:
                # Recovery: prefer old (it isn't removed unless step 3 succeeded).
                try: os.remove(tmp_path)
                except FileNotFoundError: pass
                if new_handle is not None:
                    try: new_handle.close()
                    except Exception: pass
                if os.path.exists(old_jsonl):
                    canonical = old_jsonl
                    # Leave self.jsonl_path / session_id pointing at old.
                else:
                    canonical = new_jsonl
                    self.jsonl_path = new_jsonl
                    self.session_id = new_session_id
                self._jsonl_f = open(canonical, "a")
                self._restart_writer_locked()
                raise

    def rollback_rename(self, prepared: dict[str, Any]) -> None:
        """Phase 2 (failure path): delete the sidecar. Old file/state intact.

        Safe to call without ever calling commit_rename; idempotent if the
        sidecar is already gone.

        Guard against the post-commit-recovery state: if commit_rename's
        recovery branch swapped state to new_jsonl (because the old file
        was already removed by the time the failure landed), the new file
        is now the canonical log on disk. Deleting it here would destroy
        live data — detect and skip.
        """
        new_jsonl = prepared["new_jsonl_path"]
        if self.jsonl_path == new_jsonl:
            return
        try: os.remove(new_jsonl)
        except FileNotFoundError: pass

    def _restart_writer_locked(self) -> None:
        """Caller holds ``self._q_lock``. Replaces the queue + worker."""
        self._q = queue.Queue()
        self._writer = threading.Thread(target=self._writer_loop,
                                        name="log-writer", daemon=True)
        self._writer.start()

    def close(self) -> None:
        """Stop the writer thread and close the file handle.

        Drains queued items, flushes, then unlinks an empty file so sessions
        that never received a packet don't litter the log directory. After
        close returns, further :meth:`write_jsonl` calls are silent no-ops,
        guaranteed by the ``_closed`` flag set under the queue lock alongside
        the sentinel enqueue.
        """
        with self._q_lock:
            if self._closed:
                return
            self._closed = True
            self._q.put(self._SENTINEL)
        self._writer.join(timeout=5.0)
        if self._writer.is_alive():
            _log.warning(
                "log writer thread did not stop within 5s — file handle for %s may leak",
                self.jsonl_path,
            )
            return
        self._jsonl_f.close()
        try:
            if os.path.isfile(self.jsonl_path) and os.path.getsize(self.jsonl_path) == 0:
                os.remove(self.jsonl_path)
        except OSError:
            pass
