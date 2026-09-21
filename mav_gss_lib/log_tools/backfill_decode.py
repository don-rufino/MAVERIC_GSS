"""Re-decode archived rx_packet records against the CURRENT mission decoder
and rewrite the session .jsonl in place.

Why this exists: a mission's decoder only ever runs once, at capture time —
RxPipeline.process() (the exact same code this tool calls) decodes a frame
and the result gets written to the session log and never touched again.
Log files are frozen snapshots of what the decoder produced *at the time*,
so a session captured before a decoder existed (or before a bug in it was
fixed) stays showing raw/undecoded/wrongly-warned facts forever, even
though the raw bytes needed to decode it correctly are sitting right there
in inner_hex. This tool closes that gap for downlinks only — see its
module-level docstring in the platform for why: tx_command records are
built from already-known values at send time, there's nothing in them to
re-decode.

Safety model, since logs/ is gitignored (no VCS safety net):
  - A one-time <session>.jsonl.bak is written before the first modification
    of a given file. A second run never overwrites an existing .bak, so the
    backup always reflects the true original capture, however many times
    this is re-run.
  - The rewrite is atomic (write to a temp file, then os.replace).
  - Every rx_packet record is re-decoded through the identical production
    path (PlatformRuntime.process_rx == RxPipeline.process, the same call
    RxProjectionRunner makes live) — never a reimplementation of decode
    logic — and the recomputed seq/frame_label/inner_hex/inner_len must
    match the stored record exactly, or the whole file is aborted without
    writing anything (a mismatch there means the replay isn't faithful,
    not that the file needs fixing). duplicate/uplink_echo/unknown are
    reported if they differ but don't abort the file, since duplicate
    detection depends on a reconstructed inter-frame clock
    (received_mono_ns synthesized from ts_ms, millisecond-resolution —
    plenty for the 1.0s dup window at any of these missions' framerates,
    but not byte-identical to the original monotonic clock by
    construction).
  - Only mission.facts and warnings are allowed to change on an existing
    rx_packet record (that's the whole point: new housekeeping facts,
    and any now-fixed false-positive warnings). Everything else about a
    record that already decoded correctly is left untouched — this tool
    does not reprocess parameter_cache or verifier state, and does not
    touch anything outside the one session file it's given.
  - A frame whose facts/warnings come back identical to what's already
    stored is left alone, byte-for-byte — re-running this tool on an
    already-backfilled session is a no-op.

Usage:
    python3 -m mav_gss_lib.log_tools.backfill_decode logs/json/session_*.jsonl
    python3 -m mav_gss_lib.log_tools.backfill_decode --mission suchai4
    python3 -m mav_gss_lib.log_tools.backfill_decode --mission suchai4 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mav_gss_lib.platform.log_records import parameter_records, rx_packet_record
from mav_gss_lib.platform.rx.records import RxIngestRecord
from mav_gss_lib.platform.runtime import PlatformRuntime


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Recomputed record must match the stored one exactly on these fields, or
# the file is aborted — a mismatch means the replay isn't faithful to how
# the frame was actually captured, not that the record is wrong.
_HARD_GUARD_FIELDS = ("seq", "frame_label", "inner_hex", "inner_len")
# These may legitimately drift given millisecond-resolution reconstructed
# timing — reported, not fatal.
_SOFT_GUARD_FIELDS = ("duplicate", "uplink_echo", "unknown")


class BackfillGuardError(RuntimeError):
    """Raised when a replayed frame doesn't match its stored record on a
    field that should be timing/decoder-independent — abort, don't guess."""


@dataclass(slots=True)
class SessionBackfillResult:
    path: Path
    mission_id: str | None
    total_rx: int
    changed: int
    soft_mismatches: list[str] = field(default_factory=list)
    skipped_reason: str | None = None
    wrote: bool = False

    def describe(self) -> str:
        if self.skipped_reason:
            return f"{self.path.name}: skipped ({self.skipped_reason})"
        verb = "would rewrite" if not self.wrote and self.changed else "rewrote"
        lines = [
            f"{self.path.name} [{self.mission_id}]: "
            f"{self.changed}/{self.total_rx} rx_packet records changed"
            + (f" — {verb}" if self.changed else " — no changes, left untouched")
        ]
        for m in self.soft_mismatches:
            lines.append(f"  warning: {m}")
        return "\n".join(lines)


def _load_records(path: Path) -> list[dict[str, Any]]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _rebuild_ingest(rec: dict[str, Any]) -> RxIngestRecord:
    ts_ms = int(rec["ts_ms"])
    return RxIngestRecord(
        session_generation=0,
        event_id=rec["event_id"],
        received_at_ms=ts_ms,
        # No real monotonic clock survives into the log; ts_ms is the best
        # available stand-in and is precise enough for the 1.0s dup window.
        received_mono_ns=ts_ms * 1_000_000,
        transport_meta={"transmitter": rec.get("transport_meta", "")},
        raw=bytes.fromhex(rec["inner_hex"]),
    )


def backfill_session(path: Path, *, dry_run: bool = False) -> SessionBackfillResult:
    records = _load_records(path)
    rx_indices = [i for i, r in enumerate(records) if r.get("event_kind") == "rx_packet"]
    if not rx_indices:
        return SessionBackfillResult(
            path=path, mission_id=None, total_rx=0, changed=0,
            skipped_reason="no rx_packet records",
        )

    mission_ids = {records[i].get("mission_id") for i in rx_indices}
    if len(mission_ids) != 1:
        return SessionBackfillResult(
            path=path, mission_id=None, total_rx=len(rx_indices), changed=0,
            skipped_reason=f"mixed/missing mission_id across rx_packet records: {mission_ids}",
        )
    mission_id = mission_ids.pop()
    version = records[rx_indices[0]].get("v", "")

    with tempfile.TemporaryDirectory() as scratch:
        # A scratch log_dir: this must never point at the real logs/ dir —
        # PlatformRuntime.from_split() only *reads* logs/parameters.json
        # (ParameterCache._load, no write), and process_rx() never touches
        # the cache at all (that's an RxProjectionRunner-level side effect
        # this tool deliberately doesn't invoke), but there's no reason to
        # take the risk of a future change making that load path write.
        runtime = PlatformRuntime.from_split({"logs": {"dir": scratch}}, mission_id, {})

        new_records: list[dict[str, Any]] = []
        changed = 0
        soft_mismatches: list[str] = []

        for rec in records:
            if rec.get("event_kind") != "rx_packet":
                new_records.append(rec)
                continue

            ingest = _rebuild_ingest(rec)
            result = runtime.process_rx(ingest)
            pkt = result.packet

            recomputed = rx_packet_record(
                runtime.mission, pkt, version,
                session_id=rec["session_id"],
                event_id=rec["event_id"],
                mission_id=mission_id,
                operator=rec.get("operator", ""),
                station=rec.get("station", ""),
            )

            for f in _HARD_GUARD_FIELDS:
                if recomputed[f] != rec.get(f):
                    raise BackfillGuardError(
                        f"{path.name} event_id={rec['event_id']}: replay produced "
                        f"{f}={recomputed[f]!r}, stored was {rec.get(f)!r} — "
                        "aborting this file without writing anything"
                    )
            for f in _SOFT_GUARD_FIELDS:
                if recomputed[f] != rec.get(f):
                    soft_mismatches.append(
                        f"event_id={rec['event_id']}: {f} was {rec.get(f)!r}, "
                        f"replay got {recomputed[f]!r}"
                    )

            if recomputed["mission"] == rec.get("mission") and recomputed["warnings"] == rec.get("warnings"):
                new_records.append(rec)  # unchanged — leave byte-for-byte as-is
                continue

            changed += 1
            new_records.append(recomputed)
            new_records.extend(parameter_records(
                pkt,
                session_id=rec["session_id"],
                rx_event_id=rec["event_id"],
                version=version,
                mission_id=mission_id,
                operator=rec.get("operator", ""),
                station=rec.get("station", ""),
            ))

    result = SessionBackfillResult(
        path=path, mission_id=mission_id, total_rx=len(rx_indices),
        changed=changed, soft_mismatches=soft_mismatches,
    )
    if changed == 0 or dry_run:
        return result

    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            for r in new_records:
                f.write(json.dumps(r) + "\n")
        os.replace(tmp_name, path)
    except BaseException:
        os.unlink(tmp_name)
        raise
    result.wrote = True
    return result


def _discover(mission: str, log_dir: Path) -> list[Path]:
    return sorted((log_dir / "json").glob(f"session_*_{mission}_*.jsonl"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", type=Path, help="session .jsonl files")
    parser.add_argument("--mission", help="backfill every session file for this mission id")
    parser.add_argument("--log-dir", default=_PROJECT_ROOT / "logs", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = parser.parse_args(argv)

    paths = list(args.paths)
    if args.mission:
        paths += _discover(args.mission, args.log_dir)
    if not paths:
        parser.error("no session files given — pass paths, or --mission <id>")

    exit_code = 0
    for path in paths:
        try:
            result = backfill_session(path, dry_run=args.dry_run)
        except BackfillGuardError as exc:
            print(f"{path.name}: ABORTED — {exc}", file=sys.stderr)
            exit_code = 1
            continue
        print(result.describe())
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
