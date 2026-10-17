"""Station-wide catalog of the USRPs the flowgraph has run on.

Maps each motherboard serial to an operator-given name ("SDR1"). It is one
list per machine, shared by every mission, stored in ``radios.yml`` next to
the operator configs. Serials are private station data: the file is
gitignored (guarded by a test) and nothing in the repo seeds it.
"""

from __future__ import annotations

import logging
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml

RADIOS_PATH = Path(__file__).resolve().parents[2] / "radios.yml"


class RadioCatalog:
    """serial -> {name, product, last_seen_ms, host}. ``path=None`` keeps it
    in memory only (ephemeral mode and tests)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._radios: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if self.path is None or not self.path.exists():
            return {}
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            logging.exception("radio catalog: could not read %s", self.path)
            return {}
        radios = data.get("radios") if isinstance(data, dict) else None
        if not isinstance(radios, dict):
            return {}
        return {str(k): dict(v) if isinstance(v, dict) else {} for k, v in radios.items()}

    def _save_locked(self) -> None:
        if self.path is None:
            return
        fd, tmp = tempfile.mkstemp(suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"radios": self._radios}, handle, default_flow_style=False, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _save(self) -> None:
        try:
            self._save_locked()
        except OSError:
            logging.exception("radio catalog: could not write %s", self.path)

    def seen(self, serial: str, product: str = "") -> str:
        """Record that the flowgraph started on `serial`; returns its name ("" if unnamed)."""
        with self._lock:
            entry = self._radios.setdefault(serial, {"name": ""})
            entry["product"] = product or entry.get("product", "")
            entry["last_seen_ms"] = int(time.time() * 1000)
            entry["host"] = socket.gethostname()
            self._save()
            return str(entry.get("name") or "")

    def name(self, serial: str | None) -> str:
        if not serial:
            return ""
        with self._lock:
            return str((self._radios.get(serial) or {}).get("name") or "")

    def set_names(self, names: dict[str, str]) -> None:
        """Rename known serials; unknown serials are ignored."""
        with self._lock:
            for serial, name in names.items():
                if serial in self._radios:
                    self._radios[serial]["name"] = str(name).strip()
            self._save()

    def names(self) -> dict[str, str]:
        with self._lock:
            return {s: str(e.get("name") or "") for s, e in self._radios.items()}

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [{"serial": s, "name": str(e.get("name") or ""), "product": str(e.get("product") or ""),
                     "last_seen_ms": e.get("last_seen_ms"), "host": str(e.get("host") or "")}
                    for s, e in self._radios.items()]
        return sorted(rows, key=lambda r: r["last_seen_ms"] or 0, reverse=True)
