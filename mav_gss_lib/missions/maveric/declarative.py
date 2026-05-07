"""Build MAVERIC's declarative telemetry + command capabilities.

Reads ``mission.yml`` via ``platform.spec.parse_yaml`` (with CALIBRATORS
bound), constructs a ``MaverPacketCodec`` from the parsed extensions,
and a platform ``DeclarativeFramer`` from ``mission.framing``.

Wraps the declarative command_ops with MAVERIC's operator grammar:
shortcut CLI commands, full routing-form CLI commands, and canonical
declarative dict payloads (``{cmd_id, args:dict, packet:dict}``).

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from mav_gss_lib.platform.contract import CommandOps
from mav_gss_lib.platform.contract.commands import CommandDraft
from mav_gss_lib.platform.framing import DeclarativeFramer
from mav_gss_lib.platform.spec import (
    Mission,
    ParseWarning,
    build_declarative_command_ops,
    parse_yaml,
)

from mav_gss_lib.missions.maveric.codec import MaverPacketCodec
from mav_gss_lib.missions.maveric.calibrators import CALIBRATORS
from mav_gss_lib.missions.maveric.schema_types import MavericCommandSchemaItem


_HEADER_FIELDS = ("dest", "echo", "ptype", "src")

# Per-command argument-name deprecation aliases. When an inbound payload
# carries an old key, _canonicalize remaps it to the new key (warn-once
# via logger) so external operator scripts that haven't migrated yet
# don't silently drop the value. Entries stay for one release cycle.
_DEPRECATED_ARG_ALIASES: dict[str, dict[str, str]] = {
    "ppm_sched_cmd": {"start_delay": "start_delay_ms"},
}

_LOGGER = logging.getLogger("mav_gss_lib.missions.maveric")


def _coerce_token(token: str) -> Any:
    """Best-effort numeric coercion for CLI tokens. Mirrors the inner
    declarative parse_input so positional CLI tokens reach the walker
    typed (int/float) when they look numeric."""
    try:
        return int(token, 0)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


@dataclass(frozen=True, slots=True)
class DeclarativeCapabilities:
    mission: Mission
    plugins: Mapping[str, Any]
    packet_codec: MaverPacketCodec
    command_ops: CommandOps
    parse_warnings: tuple[ParseWarning, ...]


class _MaverCommandOpsWrapper:
    """Operator-surface adapter over the declarative CommandOps.

    Two shape translations:

    * **CLI grammars** (parse_input on a string):
        - shortcut: ``CMD [arg1 arg2 ...]`` — declarative pass-through.
        - full:     ``[SRC] DEST ECHO TYPE CMD [arg1 arg2 ...]`` — routed
          MAVERIC operator form. SRC/DEST/ECHO accept node names or
          numeric ids; TYPE accepts ptype names or ids. SRC defaults to
          the GS node when 4-token form is detected.

    * **Frontend dict shape** (parse_input on a dict): canonical declarative
      ``{cmd_id, args:dict, packet:dict}``; packet header fields must live
      under ``packet``.

    Plus an `rx_only` admission gate and an operator-friendly `schema()`
    shape (``{cmd_id: {tx_args, dest, echo, ptype, nodes, ...}}``). All
    other CommandOps methods delegate to the
    inner declarative adapter via ``__getattr__``. TX columns are read
    from ``mission.yml::ui.tx_columns`` by the platform.
    """

    __slots__ = ("inner", "mission", "_codec")

    def __init__(self, *, inner: CommandOps, mission: Mission, codec: MaverPacketCodec) -> None:
        self.inner = inner
        self.mission = mission
        self._codec = codec

    def parse_input(self, value: str | dict[str, Any]) -> CommandDraft:
        if isinstance(value, dict):
            cmd_id = str(value.get("cmd_id", "")).lower()
            meta = self.mission.meta_commands.get(cmd_id)
            if meta is not None and meta.rx_only:
                raise ValueError(f"'{cmd_id}' is receive-only")
            return self.inner.parse_input(self._canonicalize(value))
        return self._parse_cli(value)

    def schema(self) -> dict[str, MavericCommandSchemaItem]:
        from mav_gss_lib.platform.spec.schema_helpers import inline_argument_metadata
        out: dict[str, MavericCommandSchemaItem] = {}
        for cmd_id, meta in self.mission.meta_commands.items():
            allowed_dest = meta.allowed_packet.get("dest")
            fixed_dest = meta.packet.get("dest")
            if allowed_dest:
                nodes = [self._resolve_node_value(v) for v in allowed_dest]
            elif fixed_dest is not None:
                nodes = [self._resolve_node_value(fixed_dest)]
            else:
                nodes = []
            out[cmd_id] = {
                "tx_args": inline_argument_metadata(self.mission, meta),
                "dest":  self._resolve_node_value(fixed_dest) if fixed_dest is not None else None,
                "echo":  self._resolve_node_value(meta.packet.get("echo")) if meta.packet.get("echo") is not None else None,
                "ptype": self._resolve_ptype_value(meta.packet.get("ptype")) if meta.packet.get("ptype") is not None else None,
                "nodes": nodes,
                "guard": meta.guard,
                "rx_only": meta.rx_only,
                "deprecated": meta.deprecated,
            }
        return out

    def _resolve_node_value(self, value: Any) -> str:
        # Schema invariant: routing fields exposed on /api/schema are
        # always symbolic node NAMES, never wire bytes. YAML may declare
        # a header as `dest: LPPM` (string) or `dest: 1` (numeric byte);
        # both produce the same wire byte at encode time but would
        # otherwise leak through to the schema as different shapes,
        # breaking the TxBuilder filter (`def.nodes.some(n =>
        # String(n) === selectedDestNode)` expects names like "LPPM").
        # Normalize at the producer so the schema is one shape.
        if isinstance(value, str):
            return value
        try:
            return self._codec.node_name_for(int(value))
        except Exception:
            return str(value)

    def _resolve_ptype_value(self, value: Any) -> str:
        # Same invariant as `_resolve_node_value` but for ptype (CMD/RES/
        # ACK/NACK/TLM/FILE — the inner-packet-type byte).
        if isinstance(value, str):
            return value
        try:
            return self._codec.ptype_name_for(int(value))
        except Exception:
            return str(value)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def correlation_key(self, encoded):
        facts = encoded.mission_facts if isinstance(encoded.mission_facts, dict) else {}
        header = facts.get("header") or {}
        dest_raw = header.get("dest", "")
        # Normalize through the same routine the schema producer uses so
        # numeric and symbolic dest declarations collapse to one key shape.
        # Returns "" for None/missing — matches the empty-string sentinel
        # used elsewhere when routing isn't declared.
        dest_name = self._resolve_node_value(dest_raw) if dest_raw not in (None, "") else ""
        return (encoded.cmd_id, dest_name)

    # -- CLI grammar -----------------------------------------------------

    def _parse_cli(self, line: str) -> CommandDraft:
        stripped = line.strip()
        # Cheap first-token grab so we can look up the meta and decide
        # whether the LAST arg is a `to_end` string. If it is, switch to
        # split(maxsplit=N) so the trailing remainder keeps its original
        # whitespace. This matters for to_end string args (Tle / LogText /
        # EmbeddedCmdArgs / MtqRegData / RpiCamArgs) where operator-typed
        # double-spaces are load-bearing.
        head = stripped.split(maxsplit=1)
        if not head:
            raise ValueError("empty command input")
        first_lower = head[0].lower()
        if first_lower in self.mission.meta_commands:
            meta = self.mission.meta_commands[first_lower]
            if meta.rx_only:
                raise ValueError(f"'{first_lower}' is receive-only")
            parts = self._split_for_meta(stripped, meta.argument_list)
            payload: dict[str, Any] = {
                "cmd_id": first_lower,
                "args": self._parse_arg_tokens(meta.argument_list, parts[1:]),
                "packet": {},
            }
            return self.inner.parse_input(self._canonicalize(payload))
        # Fall through to full-form parser. Pass `stripped` as `original`
        # so _parse_full_form can re-tokenize with maxsplit when the last
        # arg of the resolved command is a to_end string.
        return self._parse_full_form(stripped.split(), original=stripped)

    def _split_for_meta(self, stripped: str, argument_list: tuple) -> list[str]:
        """split() that preserves original whitespace in the trailing
        remainder when the last argument is a to_end string. Returns
        [cmd_id, arg1, arg2, ..., last_arg_remainder] where the last
        element keeps its inner whitespace verbatim if the last
        argument has type encoding=to_end; otherwise a normal split().
        """
        n = len(argument_list)
        if n > 0 and self._arg_consumes_to_end(argument_list[-1].type_ref):
            # cmd_id + (n-1) tokenized args + 1 remainder = n+1 items.
            # split(maxsplit=n) yields up to n+1 items.
            return stripped.split(maxsplit=n)
        return stripped.split()

    def _parse_full_form(self, parts: list[str], original: str | None = None) -> CommandDraft:
        if len(parts) < 4:
            raise ValueError(
                "need at least: <dest> <echo> <type> <cmd>  "
                "(or <src> <dest> <echo> <type> <cmd>)"
            )
        # 5+ tokens AND parts[3] resolves as a ptype → form WITH src.
        # Otherwise (4 tokens, OR 5+ where parts[2] is the ptype) → SRC=GS.
        src_name: str | None = None
        if len(parts) >= 5 and self._token_is_ptype(parts[3]):
            src_name = self._resolve_node_name(parts[0], "src")
            dest = self._resolve_node_name(parts[1], "dest")
            echo = self._resolve_node_name(parts[2], "echo")
            ptype = self._resolve_ptype_name(parts[3])
            cmd_idx = 4
        else:
            dest = self._resolve_node_name(parts[0], "dest")
            echo = self._resolve_node_name(parts[1], "echo")
            ptype = self._resolve_ptype_name(parts[2])
            cmd_idx = 3
        if cmd_idx >= len(parts):
            raise ValueError("missing command id after routing tokens")
        cmd_id = parts[cmd_idx].lower()
        meta = self.mission.meta_commands.get(cmd_id)
        if meta is None:
            raise ValueError(f"Unknown command {cmd_id!r} — verify command name in schema")
        if meta.rx_only:
            raise ValueError(f"'{cmd_id}' is receive-only")
        # When the last arg is a to_end string AND we have the original
        # line, re-tokenize the ORIGINAL with maxsplit at exactly the
        # position where the to_end arg starts. The boundary is:
        #   <prefix tokens (cmd_idx + 1)> + <fixed args (n - 1)> + <to_end remainder>
        # so split with maxsplit = cmd_idx + n. The last element of the
        # resulting list IS the verbatim remainder.
        if (original is not None and meta.argument_list
                and self._arg_consumes_to_end(meta.argument_list[-1].type_ref)):
            n = len(meta.argument_list)
            split_pieces = original.split(maxsplit=cmd_idx + n)
            # split_pieces[: cmd_idx + 1] = prefix (incl. cmd token)
            # split_pieces[cmd_idx + 1 : cmd_idx + n] = (n-1) fixed args
            # split_pieces[cmd_idx + n] = the to_end remainder (verbatim)
            arg_tokens = split_pieces[cmd_idx + 1:]
        else:
            arg_tokens = parts[cmd_idx + 1:]
        args_dict = self._parse_arg_tokens(meta.argument_list, arg_tokens)
        meta_packet = meta.packet
        allowed = meta.allowed_packet
        parsed_packet = {
            "dest": dest,
            "echo": echo,
            "ptype": ptype,
            **({"src": src_name} if src_name is not None else {}),
        }
        packet = {
            field: value
            for field, value in parsed_packet.items()
            if field in allowed or (field in meta_packet and meta_packet[field] != value)
        }
        payload: dict[str, Any] = {"cmd_id": cmd_id, "args": args_dict, "packet": packet}
        return self.inner.parse_input(self._canonicalize(payload))

    def _parse_arg_tokens(self, argument_list: tuple[Any, ...], tokens: list[str]) -> dict[str, Any]:
        args: dict[str, Any] = {}
        for arg_idx, arg in enumerate(argument_list):
            if arg_idx >= len(tokens):
                break
            if arg_idx == len(argument_list) - 1 and self._arg_consumes_to_end(arg.type_ref):
                # When _split_for_meta / _parse_full_form used
                # maxsplit=N, tokens[arg_idx] IS the already-preserved
                # remainder. Keep it verbatim — do NOT rejoin (that
                # would collapse the whitespace we just preserved).
                args[arg.name] = tokens[arg_idx]
                break
            args[arg.name] = _coerce_token(tokens[arg_idx])
        return args

    def _arg_consumes_to_end(self, type_ref: str) -> bool:
        # Delegate to the public helper so parser layers stay in sync
        # on `to_end` resolution. The helper takes the type registry
        # directly (not the Mission aggregate) — keeps the type module
        # at the bottom of the dep stack. We hold `self.mission` and
        # pass through its `argument_types` mapping; no reach into
        # `self.inner` (which is typed as the platform `CommandOps`
        # Protocol — adapter internals are intentionally not part of
        # that contract).
        from mav_gss_lib.platform.spec.argument_types import is_to_end_argument
        return is_to_end_argument(self.mission.argument_types, type_ref)

    def _resolve_node_name(self, token: str, field: str) -> str:
        s = token.strip()
        if s.lstrip("-").isdigit():
            try:
                return self._codec.node_name_for(int(s))
            except Exception as exc:
                raise ValueError(f"unknown {field} node id {token!r}") from exc
        try:
            self._codec.node_id_for(s)
        except Exception as exc:
            raise ValueError(f"unknown {field} node {token!r}") from exc
        return s

    def _resolve_ptype_name(self, token: str) -> str:
        s = token.strip()
        if s.lstrip("-").isdigit():
            try:
                return self._codec.ptype_name_for(int(s))
            except Exception as exc:
                raise ValueError(f"unknown ptype id {token!r}") from exc
        try:
            self._codec.ptype_id_for(s)
        except Exception as exc:
            raise ValueError(f"unknown ptype {token!r}") from exc
        return s

    def _token_is_ptype(self, token: str) -> bool:
        try:
            self._resolve_ptype_name(token)
            return True
        except ValueError:
            return False

    def _canonicalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalize MAVERIC's canonical declarative dict shape."""
        leaked = [field for field in _HEADER_FIELDS if field in payload]
        if leaked:
            fields = ", ".join(leaked)
            raise ValueError(f"header field(s) must be under 'packet': {fields}")
        canonical: dict[str, Any] = {"cmd_id": payload.get("cmd_id", "")}
        args = payload.get("args", {})
        if not isinstance(args, dict):
            raise ValueError("'args' must be an object")
        # Take a fresh copy so alias remapping (and any future canonical
        # rewrites) don't mutate the caller's payload dict — retry queues
        # and history viewers may hold the original reference.
        canonical["args"] = dict(args)
        cmd_id = canonical["cmd_id"]
        aliases = _DEPRECATED_ARG_ALIASES.get(cmd_id, {})
        for old, new in aliases.items():
            if old in canonical["args"] and new not in canonical["args"]:
                _LOGGER.warning(
                    "deprecated arg name %r in %s; remap to %r "
                    "(will be removed in next release)",
                    old, cmd_id, new,
                )
                canonical["args"][new] = canonical["args"].pop(old)
        packet = payload.get("packet")
        canonical["packet"] = dict(packet) if isinstance(packet, dict) else {}
        return canonical


def build_declarative_capabilities(
    *,
    mission_yml_path: str | Path,
    mission_cfg: Mapping[str, Any],
) -> DeclarativeCapabilities:
    """Construct MAVERIC's declarative capabilities from mission.yml.

    `mission_cfg` is captured by reference; the DeclarativeFramer reads
    its current state per send so /api/config edits to bound sections
    (csp.*) propagate without a MissionSpec rebuild.
    """
    mission = parse_yaml(Path(mission_yml_path), plugins=CALIBRATORS)
    if mission.framing is None:
        raise ValueError(
            f"mission.yml at {mission_yml_path} is missing the 'framing:' "
            "block — required for the declarative pipeline. See "
            "mav_gss_lib/missions/maveric/mission.example.yml for the schema."
        )
    codec = MaverPacketCodec(extensions=mission.extensions)
    framer = DeclarativeFramer(mission.framing, mission_cfg)
    declarative_ops = build_declarative_command_ops(
        mission, CALIBRATORS, packet_codec=codec, framer=framer,
    )
    command_ops = _MaverCommandOpsWrapper(
        inner=declarative_ops, mission=mission, codec=codec,
    )
    return DeclarativeCapabilities(
        mission=mission,
        plugins=CALIBRATORS,
        packet_codec=codec,
        command_ops=command_ops,
        parse_warnings=tuple(mission.parse_warnings),
    )
