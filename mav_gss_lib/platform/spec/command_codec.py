"""build_declarative_command_ops factory + DeclarativeCommandOpsAdapter.

Implements the §3.6 encode pipeline:
  1. start with meta_cmd.packet defaults (copy)
  2. merge operator overrides; check allowed_packet allowlist
  3. packet_codec.complete_header(...) — codec injects defaults and validates
     any mission-required envelope fields
  4. packet_codec.wrap(completed_header, args_bytes) -> raw

`encoded.mission_facts['header']` is the COMPLETED header (post defaulting),
mirroring the RX MissionFacts shape. Header fields remain mission facts; the
platform does not promote them into typed routing fields.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from mav_gss_lib.platform.contract.commands import (
    CommandDraft,
    CommandOps,
    CommandSchemaItem,
    EncodedCommand,
    FramedCommand,
    ValidationIssue,
)
from mav_gss_lib.platform.contract.parameters import ParamUpdate

from .commands import Argument, MetaCommand
from .errors import (
    HeaderFieldNotOverridable,
    HeaderValueNotAllowed,
    NonJsonSafeArg,
)
from .format import format_args_summary
from .mission import Mission
from .packet_codec import CommandHeader, PacketCodec
from .runtime import DeclarativeWalker


def _json_normalize(value: Any, *, cmd_id: str = "?", path: str = "") -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        b = bytes(value)
        return {"hex": b.hex(), "len": len(b)}
    if isinstance(value, dict):
        return {k: _json_normalize(v, cmd_id=cmd_id, path=f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_normalize(v, cmd_id=cmd_id, path=f"{path}[{i}]") for i, v in enumerate(value)]
    raise NonJsonSafeArg(cmd_id=cmd_id, arg_name=path or "?", value_type=type(value))


def _hashable_json(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((str(k), _hashable_json(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_hashable_json(v) for v in value)
    return value


class DeclarativeCommandOpsAdapter:
    def __init__(
        self,
        *,
        mission: Mission,
        walker: DeclarativeWalker,
        packet_codec: PacketCodec,
        framer: Any,
    ) -> None:
        self._mission = mission
        self._walker = walker
        self._packet_codec = packet_codec
        self._framer = framer
        self._meta_by_id: Mapping[str, MetaCommand] = mission.meta_commands

    # CommandOps protocol --------------------------------------------------

    def parse_input(self, value: str | dict[str, Any]) -> CommandDraft:
        if isinstance(value, str):
            from .argument_types import is_to_end_argument
            stripped = value.strip()
            head = stripped.split(maxsplit=1)
            if not head:
                raise ValueError("empty command input")
            cmd_id = head[0]
            meta = self._meta_by_id.get(cmd_id)
            args: dict[str, Any] = {}
            if meta:
                n = len(meta.argument_list)
                # If the LAST arg is a to_end string, use split(maxsplit=n)
                # so the remainder keeps its original inner whitespace
                # (Python guarantees the trailing element of split(maxsplit)
                # is the verbatim tail). Otherwise plain split() is fine —
                # numeric args canonicalize anyway.
                last_to_end = (
                    n > 0
                    and is_to_end_argument(
                        self._mission.argument_types,
                        meta.argument_list[-1].type_ref,
                    )
                )
                if last_to_end:
                    pieces = stripped.split(maxsplit=n)
                else:
                    pieces = stripped.split()
                tokens = pieces[1:]  # drop cmd_id
                last_index = n - 1
                for arg_idx, arg in enumerate(meta.argument_list):
                    if arg_idx >= len(tokens):
                        break
                    if last_to_end and arg_idx == last_index:
                        # Verbatim — do NOT _coerce_token (would str-shape
                        # multi-word remainders) and do NOT join.
                        args[arg.name] = tokens[arg_idx]
                    else:
                        args[arg.name] = _coerce_token(tokens[arg_idx])
            return CommandDraft(payload={"cmd_id": cmd_id, "args": args, "packet": {}})
        return CommandDraft(payload={
            "cmd_id": value["cmd_id"],
            "args": dict(value.get("args", {})),
            "packet": dict(value.get("packet", {})),
        })

    def validate(self, draft: CommandDraft) -> list[ValidationIssue]:
        meta = self._meta_by_id.get(draft.payload["cmd_id"])
        if meta is None:
            return [ValidationIssue(message=f"Unknown command {draft.payload['cmd_id']!r}")]
        issues: list[ValidationIssue] = []
        args = draft.payload["args"]
        known_args = {arg.name for arg in meta.argument_list}
        for name in args:
            if name not in known_args:
                issues.append(ValidationIssue(
                    message=f"{name!r} is not an argument for {meta.id!r}",
                    field=name,
                ))
        for arg in meta.argument_list:
            if arg.name not in args or args[arg.name] in (None, ""):
                issues.append(ValidationIssue(
                    message=f"missing required argument {arg.name!r}",
                    field=arg.name,
                ))
                continue
            issues.extend(self._check_arg_against_type(arg, args[arg.name]))
        return issues

    def _check_arg_against_type(
        self, arg: "Argument", value: Any,
    ) -> list[ValidationIssue]:
        from .argument_types import (
            FloatArgumentType,
            IntegerArgumentType,
            StringArgumentType,
        )

        t = self._mission.argument_types.get(arg.type_ref)
        if t is None:
            return [ValidationIssue(
                message=f"{arg.name}: unknown argument type {arg.type_ref!r}",
                field=arg.name,
            )]

        # String contract enforcement.
        #
        # The wire is ASCII end-to-end: the run-encoder
        # (`runtime.py::CommandEncoder.encode_args`) does `.encode("ascii")`
        # on the joined token string, so any non-ASCII codepoint raises
        # UnicodeEncodeError deep in encode. Catch it here at validate
        # time so the operator sees a clean field error
        # ("log_text contains non-ASCII characters") instead of a stack
        # trace. Apply to BOTH encodings — `to_end` strings get verbatim
        # whitespace but still must be ASCII.
        #
        # `ascii_token` adds the single-token rule on top: the CLI
        # parser enforces it implicitly (split on whitespace, zip arg-
        # by-token), but DICT/API input bypasses that. Without the
        # whitespace check, a caller could pass `{"name": "foo bar"}`
        # for an `ascii_token` arg and emit two wire tokens, breaking
        # the downstream framer's positional decoding.
        #
        # Enum validation is a deferred follow-up.
        if isinstance(t, StringArgumentType):
            s = str(value)
            try:
                s.encode("ascii")
            except UnicodeEncodeError:
                non_ascii = sorted({ch for ch in s if ord(ch) > 127})
                return [ValidationIssue(
                    message=(
                        f"{arg.name}={value!r} contains non-ASCII characters "
                        f"({''.join(non_ascii)!r}); the command wire is ASCII "
                        f"end-to-end. Replace or transliterate before queueing."
                    ),
                    field=arg.name,
                )]
            if t.encoding == "ascii_token":
                if any(ch.isspace() for ch in s):
                    return [ValidationIssue(
                        message=(
                            f"{arg.name}={value!r} contains whitespace; "
                            f"type {arg.type_ref!r} is encoding=ascii_token "
                            f"(single whitespace-delimited token on the wire). "
                            f"Use a to_end-encoded type for free-form text."
                        ),
                        field=arg.name,
                    )]
            return []

        if not isinstance(t, (IntegerArgumentType, FloatArgumentType)):
            # Unknown / future types have no numeric constraints at this
            # layer. (Enum validation is a deferred follow-up.)
            return []

        # Reject bool explicitly (subclass of int in Python).
        if isinstance(value, bool):
            return [ValidationIssue(
                message=f"{arg.name} must be a number (got {value!r})",
                field=arg.name,
            )]

        if isinstance(t, FloatArgumentType):
            if isinstance(value, (int, float)):
                n: float = float(value)
            else:
                try:
                    n = float(value)
                except (TypeError, ValueError):
                    return [ValidationIssue(
                        message=f"{arg.name}={value!r} must be a number",
                        field=arg.name,
                    )]
        else:
            # IntegerArgumentType: must be an exact integer. Reject 26.5
            # to prevent silent truncation; accept 26.0 as integer-valued.
            if isinstance(value, int):
                n = value
            elif isinstance(value, float):
                if not value.is_integer():
                    return [ValidationIssue(
                        message=f"{arg.name} must be an integer (got {value!r})",
                        field=arg.name,
                    )]
                n = int(value)
            else:
                try:
                    n = int(str(value), 10)
                except (TypeError, ValueError):
                    return [ValidationIssue(
                        message=f"{arg.name} must be an integer (got {value!r})",
                        field=arg.name,
                    )]

        # Effective range: explicit `valid_range` wins; otherwise derive
        # from size_bits/signed for IntegerArgumentType so primitives
        # like u8 implicitly reject 999.
        #
        # SAFETY: parse_yaml has already proven (Task 2 Step 5) that any
        # explicit valid_range is ⊆ representable range for size_bits.
        # So when valid_range is set, it can only be tighter than the
        # implicit bounds — never looser.
        out: list[ValidationIssue] = []
        if isinstance(t, IntegerArgumentType):
            if t.valid_range is not None:
                lo, hi = t.valid_range
            else:
                size = t.size_bits
                if t.signed:
                    lo, hi = -(2 ** (size - 1)), (2 ** (size - 1)) - 1
                else:
                    lo, hi = 0, (2 ** size) - 1
            if not (lo <= n <= hi):
                out.append(ValidationIssue(
                    message=f"{arg.name}={n} outside range [{lo}, {hi}]"
                            f"{' (from valid_range)' if t.valid_range else ' (from size_bits)'}",
                    field=arg.name,
                ))
        elif t.valid_range is not None:
            lo, hi = t.valid_range
            if not (lo <= n <= hi):
                out.append(ValidationIssue(
                    message=f"{arg.name}={n} outside valid_range {t.valid_range}",
                    field=arg.name,
                ))
        if t.valid_values is not None and n not in t.valid_values:
            out.append(ValidationIssue(
                message=f"{arg.name}={n} not in valid_values {t.valid_values}",
                field=arg.name,
            ))
        return out

    def encode(self, draft: CommandDraft) -> EncodedCommand:
        cmd_id = draft.payload["cmd_id"]
        args = draft.payload["args"]
        operator_packet = draft.payload.get("packet", {})
        meta = self._meta_by_id[cmd_id]

        working = dict(meta.packet)
        for field, value in operator_packet.items():
            if field not in meta.allowed_packet:
                raise HeaderFieldNotOverridable(cmd_id, field)
            if value not in meta.allowed_packet[field]:
                raise HeaderValueNotAllowed(
                    cmd_id, field, value, allowed=tuple(meta.allowed_packet[field]),
                )
            working[field] = value
        args_bytes = self._walker.encode_args(cmd_id, args)
        completed = self._packet_codec.complete_header(CommandHeader(id=cmd_id, fields=working))
        raw = self._packet_codec.wrap(completed, args_bytes)

        completed_fields = dict(completed.fields)
        normalized_args = {
            arg.name: _json_normalize(args[arg.name], cmd_id=cmd_id, path=arg.name)
            for arg in meta.argument_list
            if arg.name in args
        }
        # cmd_id is the canonical EncodedCommand field; do NOT duplicate it
        # under header.cmd_id — the platform logger surfaces it as
        # mission.cmd_id.
        header = {
            **{k: v for k, v in completed_fields.items() if k != "cmd_id"},
            "args": format_args_summary(normalized_args.items()),
        }
        # Inner-frame length: this is the COMMAND wire (CSP), not the
        # outer ASM+Golay frame. Name accordingly.
        protocol = {
            "args_hex": args_bytes.hex(),
            "args_len": len(args_bytes),
            "inner_len": len(raw),
        }
        mission_facts = {"header": header, "protocol": protocol}
        # Typed args -> parameters (mirrors RX walker emit). The command's
        # argument_list, not operator payload keys, owns names and order.
        params = tuple(
            ParamUpdate(name=name, value=value, ts_ms=0)
            for name, value in normalized_args.items()
        )

        return EncodedCommand(
            raw=raw,
            cmd_id=cmd_id,
            src=str(completed_fields.get("src", "")),
            guard=meta.guard,
            mission_facts=mission_facts,
            parameters=params,
        )

    def frame(self, encoded: EncodedCommand) -> FramedCommand:
        return self._framer.frame(encoded)

    def correlation_key(self, encoded: EncodedCommand) -> tuple:
        facts = encoded.mission_facts if isinstance(encoded.mission_facts, dict) else {}
        header = facts.get("header") if isinstance(facts, dict) else None
        return (encoded.cmd_id, _hashable_json(header or {}))

    def schema(self) -> Mapping[str, CommandSchemaItem]:
        # Final shape: bare {cmd_id: CommandSchemaItem}. Same `tx_args`
        # inlining as MAVERIC's wrapper (via inline_argument_metadata),
        # minus the MAVERIC routing extension (dest/echo/ptype/nodes —
        # those are mission-specific and live in
        # missions/maveric/declarative.py::schema()).
        from mav_gss_lib.platform.spec.schema_helpers import inline_argument_metadata
        out: dict[str, CommandSchemaItem] = {}
        for cmd_id, meta in self._meta_by_id.items():
            out[cmd_id] = {
                "tx_args": inline_argument_metadata(self._mission, meta),
                "guard": meta.guard,
                "rx_only": meta.rx_only,
                "deprecated": meta.deprecated,
            }
        return out


def _coerce_token(token: str) -> Any:
    try:
        return int(token, 0)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def build_declarative_command_ops(
    mission: Mission,
    plugins: Mapping[str, Callable],
    *,
    packet_codec: PacketCodec,
    framer: Any,
) -> CommandOps:
    walker = DeclarativeWalker(mission, plugins)
    return DeclarativeCommandOpsAdapter(
        mission=mission, walker=walker,
        packet_codec=packet_codec, framer=framer,
    )


__all__ = [
    "DeclarativeCommandOpsAdapter",
    "build_declarative_command_ops",
]
