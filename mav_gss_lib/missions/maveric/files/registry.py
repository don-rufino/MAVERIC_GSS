"""Python-declarative file-transport registry.

One row per file kind. Read once at ``mission.py::build(ctx)`` to
construct the concrete adapters; the events watcher and HTTP
router are then wired with the resulting list. The registry is a
maveric-specific binding layer - it composes the kind-agnostic store
and the adapter Protocol.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mav_gss_lib.missions.maveric.files.adapters import (
    AiiKindAdapter,
    EpsKindAdapter,
    FileKindAdapter,
    ImageKindAdapter,
    MagKindAdapter,
)


@dataclass(frozen=True, slots=True)
class FileTransportConfig:
    """Documentation + validation row for one file kind.

    The adapter classes themselves carry their own cmd_id / media_type
    defaults; this dataclass mirrors them so we can validate against
    ``mission.meta_commands`` at build time.
    """

    kind: str
    cnt_cmd: str
    get_cmd: str
    capture_cmd: str | None
    output_subdir: str
    extension: str
    media_type: str


FILE_TRANSPORTS: tuple[FileTransportConfig, ...] = (
    FileTransportConfig(
        kind="image", cnt_cmd="img_cnt_chunks", get_cmd="img_get_chunks",
        capture_cmd="cam_capture", output_subdir="image", extension=".jpg",
        media_type="image/jpeg",
    ),
    FileTransportConfig(
        kind="aii", cnt_cmd="aii_cnt_chunks", get_cmd="aii_get_chunks",
        capture_cmd=None, output_subdir="aii", extension=".json",
        media_type="application/json",
    ),
    FileTransportConfig(
        kind="mag", cnt_cmd="mag_cnt_chunks", get_cmd="mag_get_chunks",
        capture_cmd=None, output_subdir="mag", extension=".npz",
        media_type="application/octet-stream",
    ),
    FileTransportConfig(
        kind="eps", cnt_cmd="eps_cnt_chunks", get_cmd="eps_get_chunks",
        capture_cmd=None, output_subdir="eps", extension=".npz",
        media_type="application/octet-stream",
    ),
)


def build_file_kind_adapters(mission_cfg: dict[str, Any]) -> list[FileKindAdapter]:
    """Construct the concrete adapters with a live mission_cfg ref.

    Order matches ``FILE_TRANSPORTS``. The image adapter holds a
    closure over ``mission_cfg`` so live edits to
    ``imaging.thumb_prefix`` apply without rebuilding the MissionSpec.
    """
    return [
        ImageKindAdapter(mission_cfg=mission_cfg),
        AiiKindAdapter(),
        MagKindAdapter(),
        EpsKindAdapter(),
    ]


def validate_against_mission(mission: Any) -> None:
    """Raise ValueError if any FILE_TRANSPORTS cmd_id is missing from
    ``mission.meta_commands``.

    Called once at server start (mission.py::build) so a typo in the
    registry doesn't silently drop downlinks.
    """
    meta = getattr(mission, "meta_commands", None) or {}
    missing: list[str] = []
    for cfg in FILE_TRANSPORTS:
        for cmd in (cfg.cnt_cmd, cfg.get_cmd, cfg.capture_cmd):
            if cmd is None:
                continue
            if cmd not in meta:
                missing.append(f"{cfg.kind}.{cmd}")
    if missing:
        raise ValueError(
            "FILE_TRANSPORTS references commands not in mission.meta_commands: "
            + ", ".join(sorted(missing))
        )
