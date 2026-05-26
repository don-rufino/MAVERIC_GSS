"""
mav_gss_lib.server.tx.queue -- Pure TX Queue Operations

Stateless queue helper functions: item construction, validation,
serialization, persistence, import/export parsing, and summary.

TxService remains the runtime state owner (queue, history, sending).
This module owns only the pure logic that operates on queue data
without holding any mutable state of its own.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, Union

from mav_gss_lib.platform import EncodedCommand

if TYPE_CHECKING:
    from ..state import WebRuntime
    from mav_gss_lib.platform.tx.commands import PreparedCommand


class MissionCmdItem(TypedDict):
    type: Literal["mission_cmd"]
    raw_cmd: NotRequired[bytes]
    cmd_id: str
    mission: dict[str, Any]                  # {id, facts}
    parameters: list[dict[str, Any]]         # typed args from EncodedCommand.parameters
    correlation_key: list[Any]
    payload: str | dict[str, Any]            # original input — opaque to platform
    guard: bool
    num: NotRequired[int]
    event_id: NotRequired[str]


class DelayItem(TypedDict):
    type: Literal["delay"]
    delay_ms: int


class NoteItem(TypedDict):
    type: Literal["note"]
    text: str


class CheckpointItem(TypedDict):
    type: Literal["checkpoint"]
    text: str


QueueItem = Union[MissionCmdItem, DelayItem, NoteItem, CheckpointItem]


def make_delay(delay_ms: int) -> DelayItem:
    """Build one delay queue item."""
    return {"type": "delay", "delay_ms": delay_ms}


def make_note(text: Any) -> NoteItem:
    """Build one note queue item (from ``//`` comment lines in JSONL files)."""
    return {"type": "note", "text": " ".join(str(text).split())}


def make_checkpoint(text: Any) -> CheckpointItem:
    """Build one manual checkpoint queue item."""
    clean = " ".join(str(text).split())
    return {"type": "checkpoint", "text": clean or "Confirm before continuing"}


def _params_as_json(params: tuple) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in params:
        if is_dataclass(p):
            out.append(asdict(p))
        elif isinstance(p, dict):
            out.append(dict(p))
    return out


def _mission_block(mission_id: str, encoded: EncodedCommand) -> dict[str, Any]:
    return {
        "id": mission_id,
        "cmd_id": encoded.cmd_id,
        "facts": dict(encoded.mission_facts or {}),
    }


def _correlation_key_json(key: tuple[Any, ...]) -> list[Any]:
    return list(key)


def make_mission_cmd(
    payload: str | dict[str, Any],
    runtime: "WebRuntime | None" = None,
) -> MissionCmdItem:
    """Build one mission-command queue item from a CLI string or builder dict.

    Calls the active mission command ops to validate and encode. Does NOT
    check MTU — use ``validate_mission_cmd`` for full admission.

    The persisted ``payload`` field is the user's original input dict —
    needed by ``load_queue`` (and import/export) so the mission can rebuild
    the EncodedCommand on the next session. Free-form (string) inputs are
    stored as ``{"line": ...}`` since the mission's parse_input handles both.
    """
    if runtime is None:
        raise ValueError("mission command validation requires a runtime")

    prepared = runtime.platform.prepare_tx(payload)
    encoded = prepared.encoded
    # Persist the user's original input verbatim — string or dict — so
    # disk-saved queues and import/export files survive a restart.
    # `load_queue` hands it back to `mission.parse_input` which knows how
    # to interpret its own grammar; the platform never inspects `payload`.
    return {
        "type": "mission_cmd",
        "raw_cmd": encoded.raw,
        "cmd_id": encoded.cmd_id,
        "mission": _mission_block(runtime.mission_id, encoded),
        "parameters": _params_as_json(encoded.parameters),
        "correlation_key": _correlation_key_json(runtime.mission.commands.correlation_key(encoded)),
        "guard": bool(payload.get("guard", encoded.guard)) if isinstance(payload, dict) else bool(encoded.guard),
        "payload": payload,
    }


def validate_mission_cmd(
    payload: str | dict[str, Any],
    runtime: "WebRuntime | None" = None,
) -> MissionCmdItem:
    """Validate and build a mission-command queue item.

    Checks: mission-owned build succeeds, mission-owned framing admits the
    encoded bytes (MTU / FEC-cap / etc).
    """
    from ..state import ensure_runtime

    runtime = ensure_runtime(runtime)
    item = make_mission_cmd(payload, runtime=runtime)

    mission = runtime.mission
    if mission.commands is not None:
        encoded = mission.commands.encode(mission.commands.parse_input(payload))
        if encoded.raw != item["raw_cmd"]:
            encoded = EncodedCommand(
                raw=item["raw_cmd"],
                cmd_id=encoded.cmd_id,
                src=encoded.src,
                guard=encoded.guard,
                mission_facts=encoded.mission_facts,
                parameters=encoded.parameters,
            )
        mission.commands.frame(encoded)
    return item


def sanitize_queue_items(
    items: list[dict[str, Any]],
    runtime: "WebRuntime | None" = None,
) -> tuple[list[QueueItem], int]:
    """Filter a queue restore/import set down to valid command/delay items."""
    from ..state import ensure_runtime

    runtime = ensure_runtime(runtime)
    accepted: list[QueueItem] = []
    skipped = 0
    for item in items:
        if item["type"] in ("delay", "note", "checkpoint"):
            accepted.append(item)  # type: ignore[arg-type]
            continue
        if item["type"] == "mission_cmd":
            try:
                rebuilt = validate_mission_cmd(
                    item.get("payload", {}),
                    runtime=runtime,
                )
                if item.get("guard"):
                    rebuilt["guard"] = True
                accepted.append(rebuilt)
            except ValueError:
                skipped += 1
            continue
        skipped += 1
    return accepted, skipped


def item_to_json(item: dict[str, Any]) -> dict[str, Any]:
    """Serialize a queue item for persistence (strips raw_cmd bytes)."""
    return {key: value for key, value in item.items() if key != "raw_cmd"}


def json_to_item(
    payload: dict[str, Any],
    runtime: "WebRuntime | None" = None,
) -> QueueItem:
    """Convert one persisted JSON payload back into a runtime queue item."""
    if payload["type"] == "delay":
        return make_delay(payload.get("delay_ms", 0))
    if payload["type"] == "note":
        return make_note(payload.get("text", ""))
    if payload["type"] == "checkpoint":
        return make_checkpoint(payload.get("text", ""))
    if payload["type"] == "mission_cmd":
        item = validate_mission_cmd(
            payload.get("payload", {}),
            runtime=runtime,
        )
        if "guard" in payload:
            item["guard"] = payload["guard"]
        return item
    raise ValueError(f"unsupported queue item type: {payload['type']}")


def save_queue(queue: list[QueueItem], queue_file: Path) -> None:
    """Persist the current queue to disk as JSONL."""
    if not queue:
        try:
            os.remove(queue_file)
        except FileNotFoundError:
            pass
        return
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        prev_mode = queue_file.stat().st_mode & 0o777
    except FileNotFoundError:
        prev_mode = 0o664
    fd, tmp = tempfile.mkstemp(suffix=".tmp", dir=str(queue_file.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            for item in queue:
                handle.write(json.dumps(item_to_json(item)) + "\n")  # type: ignore[arg-type]
        os.chmod(tmp, prev_mode)
        os.replace(tmp, str(queue_file))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_queue(queue_file: Path, runtime: "WebRuntime | None" = None) -> list[QueueItem]:
    """Load any persisted queue items from disk."""
    if not queue_file.is_file():
        return []
    items: list[QueueItem] = []
    with open(queue_file) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                items.append(json_to_item(payload, runtime=runtime))
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                logging.warning("Skipped corrupted queue entry: %s", exc)
    return items


def renumber_queue(queue: list[QueueItem]) -> None:
    """Assign sequential display numbers to queued command items."""
    count = 0
    for item in queue:
        if item["type"] == "mission_cmd":
            count += 1
            item["num"] = count  # type: ignore[typeddict-item]


def queue_summary(queue: list[QueueItem], default_delay_ms: int = 500) -> dict[str, Any]:
    """Summarize queue size, guard count, and rough execution time."""
    cmds = sum(1 for item in queue if item["type"] == "mission_cmd")
    guards = sum(1 for item in queue if item.get("guard"))
    checkpoints = sum(1 for item in queue if item["type"] == "checkpoint")
    delay_total = sum(item.get("delay_ms", 0) for item in queue if item["type"] == "delay")
    inter_cmd_ms = default_delay_ms * max(cmds - 1, 0)
    est_time_s = (delay_total + inter_cmd_ms) / 1000.0
    return {"cmds": cmds, "guards": guards, "checkpoints": checkpoints, "est_time_s": round(est_time_s, 1)}


def queue_items_json(queue: list[QueueItem]) -> list[dict[str, Any]]:
    """Project the current queue into the websocket/API JSON shape."""
    result: list[dict[str, Any]] = []
    for item in queue:
        if item["type"] == "delay":
            result.append({"type": "delay", "delay_ms": item["delay_ms"]})
            continue
        if item["type"] == "note":
            result.append({"type": "note", "text": item["text"]})
            continue
        if item["type"] == "checkpoint":
            result.append({"type": "checkpoint", "text": item["text"]})
            continue
        raw = item.get("raw_cmd", b"")
        projected: dict[str, Any] = {
            "type": "mission_cmd",
            "num": item.get("num", 0),
            "cmd_id": item.get("cmd_id", ""),
            "mission": item.get("mission", {}),
            "parameters": item.get("parameters", []),
            "guard": item.get("guard", False),
            "size": len(raw),
            "raw_hex": raw.hex() if raw else "",
            "payload": item.get("payload", {}),
        }
        if item.get("event_id"):
            projected["event_id"] = item["event_id"]
        result.append(projected)
    return result


def parse_import_file(
    filepath: Path,
    runtime: "WebRuntime | None" = None,
) -> tuple[list[QueueItem], int]:
    """Parse a queue import JSONL file into runtime queue items."""
    items: list[QueueItem] = []
    skipped = 0
    for raw_line in filepath.read_text().strip().split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("//"):
            text = line[2:].strip()
            if text:
                items.append(make_note(text))
            continue
        in_str, escaped, out = False, False, []
        for index, ch in enumerate(line):
            if escaped:
                escaped = False
                out.append(ch)
                continue
            if ch == "\\" and in_str:
                escaped = True
                out.append(ch)
                continue
            if ch == '"':
                in_str = not in_str
                out.append(ch)
                continue
            if not in_str and ch == "/" and index + 1 < len(line) and line[index + 1] == "/":
                break
            out.append(ch)
        line = "".join(out).rstrip().rstrip(",")
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                if obj.get("type") == "delay":
                    items.append(make_delay(max(0, min(300_000, int(obj.get("delay_ms", 0))))))
                elif obj.get("type") == "note":
                    text = str(obj.get("text", "")).strip()
                    if text:
                        items.append(make_note(text))
                elif obj.get("type") == "checkpoint":
                    items.append(make_checkpoint(obj.get("text", "")))
                elif obj.get("type") == "mission_cmd" and "payload" in obj:
                    item = validate_mission_cmd(obj["payload"], runtime=runtime)
                    if obj.get("guard"):
                        item["guard"] = True
                    items.append(item)
                else:
                    skipped += 1
            else:
                skipped += 1
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            skipped += 1
    return items, skipped
