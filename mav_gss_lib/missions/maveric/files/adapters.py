"""Per-kind file adapters.

Each adapter encapsulates everything format-specific about a file kind:
which packets seed totals, whether to repair partial files, MIME type,
status-view shape, optional on-complete validation, optional thumb
pairing.

Adapters are constructed once at ``mission.py::build(ctx)`` from
``FILE_TRANSPORTS`` rows (see ``registry.py``) and registered with the
events watcher and the HTTP router.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from mav_gss_lib.missions.maveric.files.store import ChunkFileStore, FileRef


# ── Shared helpers ─────────────────────────────────────────────────


def slice_chunk_data(args_raw: bytes, chunk_len: int) -> bytes:
    """Extract the trailing chunk_data bytes from a ``*_get_chunks_file``
    args_raw blob.

    Layout: ``<filename> <chunk_idx> <chunk_len> <bytes…>`` — the first
    three tokens are ASCII separated by single spaces. We walk past
    three spaces to land at the first byte of chunk_data, then take
    ``chunk_len`` bytes.

    Identical wire layout for ``img_get_chunks_file``,
    ``aii_get_chunks_file``, and ``mag_get_chunks_file`` in
    ``mission.yml`` — a single helper covers all three kinds.
    """
    if not args_raw or chunk_len <= 0:
        return b""
    pos = 0
    for _ in range(3):
        sp = args_raw.find(b" ", pos)
        if sp < 0:
            return b""
        pos = sp + 1
    return bytes(args_raw[pos:pos + chunk_len])


def args_by_key(packet: Any) -> dict[str, Any]:
    """Build a flat ``{key: value}`` map from a packet's parameter updates."""
    out: dict[str, Any] = {}
    for u in packet.parameters:
        if u.display_only:
            continue
        key = u.name.split(".", 1)[1] if "." in u.name else u.name
        out[key] = u.value
    return out


def packet_source(header: dict[str, Any]) -> str | None:
    src = header.get("src")
    if src is None:
        return None
    text = str(src).strip()
    return text or None


# ── Adapter Protocol ───────────────────────────────────────────────


class FileKindAdapter(Protocol):
    """Per-kind hooks the events watcher and router call into."""

    kind: str
    cnt_cmd: str
    get_cmd: str
    capture_cmd: str | None
    media_type: str

    def seed_from_cnt(self, args: dict[str, Any]) -> Iterable[tuple[str, int]]: ...

    def seed_from_capture(self, args: dict[str, Any]) -> Iterable[tuple[str, int]]: ...

    def partial_repair(self, path: str) -> None: ...

    def on_complete(self, path: str) -> dict[str, Any]: ...

    def status_view(self, store: ChunkFileStore) -> dict[str, Any]: ...


# ── ImageKindAdapter ───────────────────────────────────────────────


@dataclass(slots=True)
class ImageKindAdapter:
    """JPEG image kind: thumb pairing, EOI repair, cam_capture twin-seed.

    ``mission_cfg`` is a live reference into ``runtime.mission_cfg`` so
    ``/api/config`` edits to ``imaging.thumb_prefix`` apply without a
    MissionSpec rebuild — same closure-over-live-cfg pattern the legacy
    imaging router used.
    """

    mission_cfg: dict[str, Any]
    kind: str = "image"
    cnt_cmd: str = "img_cnt_chunks"
    get_cmd: str = "img_get_chunks"
    capture_cmd: str | None = "cam_capture"
    media_type: str = "image/jpeg"

    def seed_from_cnt(self, args: dict[str, Any]) -> Iterable[tuple[str, int]]:
        return self._seed_full_and_thumb(args)

    def seed_from_capture(self, args: dict[str, Any]) -> Iterable[tuple[str, int]]:
        return self._seed_full_and_thumb(args)

    def _seed_full_and_thumb(self, args: dict[str, Any]) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for filename_key, total_key in (
            ("filename", "num_chunks"),
            ("thumb_filename", "thumb_num_chunks"),
        ):
            filename = str(args.get(filename_key, ""))
            if not filename:
                continue
            try:
                total = int(args.get(total_key, ""))
            except (ValueError, TypeError):
                continue
            out.append((filename, total))
        return out

    def partial_repair(self, path: str) -> None:
        from mav_gss_lib.missions.maveric.files.repair import jpeg_eoi_repair
        jpeg_eoi_repair(path)

    def on_complete(self, path: str) -> dict[str, Any]:
        return {}

    @property
    def thumb_prefix(self) -> str:
        return (self.mission_cfg.get("imaging") or {}).get("thumb_prefix", "") or ""

    def status_view(self, store: ChunkFileStore) -> dict[str, Any]:
        prefix = self.thumb_prefix
        all_refs = store.known_files(kind=self.kind)
        ref_set = set(all_refs)

        def real_leaf(ref: FileRef) -> dict[str, Any]:
            received, total = store.progress(ref)
            return {
                "id": ref.id,
                "kind": ref.kind,
                "source": ref.source,
                "filename": ref.filename,
                "received": received,
                "total": total,
                "complete": store.is_complete(ref),
                "chunk_size": store.chunk_size(ref),
            }

        def placeholder_leaf(ref: FileRef) -> dict[str, Any]:
            return {
                "id": ref.id,
                "kind": ref.kind,
                "source": ref.source,
                "filename": ref.filename,
                "received": 0,
                "total": None,
                "complete": False,
                "chunk_size": None,
            }

        def leaf(ref: FileRef) -> dict[str, Any]:
            return real_leaf(ref) if ref in ref_set else placeholder_leaf(ref)

        if not prefix:
            entries = [
                {
                    "id": r.id, "kind": r.kind, "source": r.source,
                    "stem": r.filename, "full": real_leaf(r), "thumb": None,
                    "last_activity_ms": store.meta_mtime_ms(r),
                }
                for r in all_refs
            ]
            entries.sort(key=lambda p: (-(p["last_activity_ms"] or 0), p["source"] or "", p["stem"]))
            return {"files": entries}

        stems_by_source: dict[str | None, set[str]] = {}
        for r in all_refs:
            stem = r.filename[len(prefix):] if r.filename.startswith(prefix) else r.filename
            stems_by_source.setdefault(r.source, set()).add(stem)

        pairs: list[dict[str, Any]] = []
        for source, stems in stems_by_source.items():
            for stem in stems:
                full_ref = FileRef(kind=self.kind, source=source, filename=stem)
                thumb_ref = FileRef(kind=self.kind, source=source, filename=f"{prefix}{stem}")
                full = leaf(full_ref)
                thumb = leaf(thumb_ref)
                mtime = max(
                    store.meta_mtime_ms(full_ref) or 0 if full_ref in ref_set else 0,
                    store.meta_mtime_ms(thumb_ref) or 0 if thumb_ref in ref_set else 0,
                )
                pairs.append({
                    "id": full_ref.id,  # matches FileRef.id — no double-slash for source-less refs
                    "kind": self.kind, "source": source, "stem": stem,
                    "full": full, "thumb": thumb,
                    "last_activity_ms": mtime or None,
                })

        pairs.sort(key=lambda p: (-(p["last_activity_ms"] or 0), p["source"] or "", p["stem"]))
        return {"files": pairs}


# ── AiiKindAdapter ─────────────────────────────────────────────────


@dataclass(slots=True)
class AiiKindAdapter:
    """JSON inventory kind: single-seed, validate on complete, no pairing.

    The events watcher writes the ``valid`` flag returned by
    ``on_complete`` into ``store.set_extras(...)`` once per completion.
    ``status_view`` reads it back from extras — no file I/O per call.
    """

    kind: str = "aii"
    cnt_cmd: str = "aii_cnt_chunks"
    get_cmd: str = "aii_get_chunks"
    capture_cmd: str | None = None
    media_type: str = "application/json"

    def seed_from_cnt(self, args: dict[str, Any]) -> Iterable[tuple[str, int]]:
        return _single_seed(args)

    def seed_from_capture(self, args: dict[str, Any]) -> Iterable[tuple[str, int]]:
        return []

    def partial_repair(self, path: str) -> None:
        return None

    def on_complete(self, path: str) -> dict[str, Any]:
        from mav_gss_lib.missions.maveric.files.repair import json_validate
        return json_validate(path)

    def status_view(self, store: ChunkFileStore) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        for ref in store.known_files(kind=self.kind):
            received, total = store.progress(ref)
            extras = store.get_extras(ref)
            files.append({
                "id": ref.id,
                "kind": ref.kind,
                "source": ref.source,
                "filename": ref.filename,
                "received": received,
                "total": total,
                "complete": store.is_complete(ref),
                "chunk_size": store.chunk_size(ref),
                "last_activity_ms": store.meta_mtime_ms(ref),
                "valid": extras.get("valid"),
            })
        files.sort(key=lambda p: (-(p["last_activity_ms"] or 0), p["source"] or "", p["filename"]))
        return {"files": files}


def _single_seed(args: dict[str, Any]) -> list[tuple[str, int]]:
    filename = str(args.get("filename", ""))
    if not filename:
        return []
    try:
        total = int(args.get("num_chunks", ""))
    except (ValueError, TypeError):
        return []
    return [(filename, total)]


# ── MagKindAdapter ─────────────────────────────────────────────────


@dataclass(slots=True)
class MagKindAdapter:
    """Magnetometer NVG kind: raw bytes, single-seed, no validation."""

    kind: str = "mag"
    cnt_cmd: str = "mag_cnt_chunks"
    get_cmd: str = "mag_get_chunks"
    capture_cmd: str | None = None
    media_type: str = "application/octet-stream"

    def seed_from_cnt(self, args: dict[str, Any]) -> Iterable[tuple[str, int]]:
        return _single_seed(args)

    def seed_from_capture(self, args: dict[str, Any]) -> Iterable[tuple[str, int]]:
        return []

    def partial_repair(self, path: str) -> None:
        return None

    def on_complete(self, path: str) -> dict[str, Any]:
        return {}

    def status_view(self, store: ChunkFileStore) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        for ref in store.known_files(kind=self.kind):
            received, total = store.progress(ref)
            files.append({
                "id": ref.id,
                "kind": ref.kind,
                "source": ref.source,
                "filename": ref.filename,
                "received": received,
                "total": total,
                "complete": store.is_complete(ref),
                "chunk_size": store.chunk_size(ref),
                "last_activity_ms": store.meta_mtime_ms(ref),
            })
        files.sort(key=lambda p: (-(p["last_activity_ms"] or 0), p["source"] or "", p["filename"]))
        return {"files": files}


# ── EpsKindAdapter ─────────────────────────────────────────────────


@dataclass(slots=True)
class EpsKindAdapter:
    """EPS-payload npz kind: raw bytes, single-seed, no validation.

    Structural twin of ``MagKindAdapter`` — the payload RPi ``eps.py``
    captures EPS housekeeping into an npz (``eps_capture``) and downlinks
    it chunked (``eps_cnt_chunks`` seeds the total, ``eps_get_chunks``
    feeds chunks). Distinct from the EPS-*node* ``eps_*`` commands.
    """

    kind: str = "eps"
    cnt_cmd: str = "eps_cnt_chunks"
    get_cmd: str = "eps_get_chunks"
    capture_cmd: str | None = None
    media_type: str = "application/octet-stream"

    def seed_from_cnt(self, args: dict[str, Any]) -> Iterable[tuple[str, int]]:
        return _single_seed(args)

    def seed_from_capture(self, args: dict[str, Any]) -> Iterable[tuple[str, int]]:
        return []

    def partial_repair(self, path: str) -> None:
        return None

    def on_complete(self, path: str) -> dict[str, Any]:
        return {}

    def status_view(self, store: ChunkFileStore) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        for ref in store.known_files(kind=self.kind):
            received, total = store.progress(ref)
            files.append({
                "id": ref.id,
                "kind": ref.kind,
                "source": ref.source,
                "filename": ref.filename,
                "received": received,
                "total": total,
                "complete": store.is_complete(ref),
                "chunk_size": store.chunk_size(ref),
                "last_activity_ms": store.meta_mtime_ms(ref),
            })
        files.sort(key=lambda p: (-(p["last_activity_ms"] or 0), p["source"] or "", p["filename"]))
        return {"files": files}
