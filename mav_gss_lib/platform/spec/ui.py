"""Declarative UI-spec types for mission.yml.

A mission may declare its packet-list / TX queue columns as YAML:

    ui:
      rx_columns:
        - { id: source, label: src,   width: 52, path: header.source }
        - { id: kind,   label: type,  width: 52, path: header.kind, badge: true,
            value_icons: {CMD: command, TLM: telemetry}, default_icon: unknown }
      tx_columns:
        - { id: target, label: tgt,   width: 52, path: header.target }
        - { id: cmd,    label: cmd,   flex: true,  kind: cmd_id }
        - { id: verify, label: verify, width: 78, align: right, kind: verifiers }

`width:` is the fixed column width in CSS pixels (positive integer).
The platform applies it as an inline style at render time, so the value
is CSS-framework-agnostic and works for any pixel value without needing
a build-time class allowlist.

`path:` is a dotted lookup against the row's mission-facts dict on the
client. Platform shell columns (RX: num/time/frame/flags/size) are
hardcoded in the frontend and not authored here. Cells are pure
declarative — there is no formatter registry; missing values render as
"--". Badge icons may be selected with semantic ``value_icons`` tokens;
the frontend owns the concrete icon library.

`kind:` selects the renderer. `value` (default) walks `path:` against
the facts dict. `verifiers` ignores `path:` and renders the
client-tracked tick strip from the verification WS stream. `cmd_id`
ignores `path:` and reads the canonical envelope field
(``mission.cmd_id``) — same field the platform log-search uses.

Author:  Irfan Annuar - USC ISI SERC
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


_VALID_ALIGN = ("left", "right")
_VALID_KINDS = ("value", "verifiers", "cmd_id")
_VALID_ICON_TOKENS = frozenset((
    "command",
    "request",
    "response",
    "ack",
    "telemetry",
    "file",
    "error",
    "unknown",
))


@dataclass(frozen=True, slots=True)
class UiColumn:
    id: str
    label: str
    path: str = ""
    kind: str = "value"
    width: int | None = None
    align: str | None = None
    flex: bool = False
    toggle: str | None = None
    badge: bool = False
    truncate: bool = False
    value_icons: tuple[tuple[str, str], ...] = ()
    default_icon: str | None = None
    hide_if_all: tuple[Any, ...] = ()

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "label": self.label}
        if self.path:
            out["path"] = self.path
        if self.kind != "value":
            out["kind"] = self.kind
        if self.width is not None:
            out["width"] = self.width
        if self.align is not None:
            out["align"] = self.align
        if self.flex:
            out["flex"] = True
        if self.toggle is not None:
            out["toggle"] = self.toggle
        if self.badge:
            out["badge"] = True
        if self.truncate:
            out["truncate"] = True
        if self.value_icons:
            out["value_icons"] = dict(self.value_icons)
        if self.default_icon is not None:
            out["default_icon"] = self.default_icon
        if self.hide_if_all:
            out["hide_if_all"] = list(self.hide_if_all)
        return out


@dataclass(frozen=True, slots=True)
class UiSpec:
    rx_columns: tuple[UiColumn, ...] = ()
    tx_columns: tuple[UiColumn, ...] = ()


def parse_ui_section(raw: Mapping[str, Any] | None) -> UiSpec | None:
    """Parse the ``ui:`` block from mission.yml.

    Returns None when the block is absent. Empty mapping is treated as
    "no UI customization" and yields an empty UiSpec.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("ui must be a mapping")

    return UiSpec(
        rx_columns=_parse_columns(raw.get("rx_columns") or [], "ui.rx_columns"),
        tx_columns=_parse_columns(raw.get("tx_columns") or [], "ui.tx_columns"),
    )


def _parse_columns(raw_list: Any, path_label: str) -> tuple[UiColumn, ...]:
    if not isinstance(raw_list, list):
        raise ValueError(f"{path_label} must be a list")
    seen_ids: set[str] = set()
    columns: list[UiColumn] = []
    for entry in raw_list:
        columns.append(_parse_column(entry, seen_ids, path_label))
    return tuple(columns)


def _parse_column(entry: Any, seen_ids: set[str], path_label: str) -> UiColumn:
    if not isinstance(entry, Mapping):
        raise ValueError(f"{path_label} entry must be a mapping, got {type(entry).__name__}")
    col_id = entry.get("id")
    if not isinstance(col_id, str) or not col_id:
        raise ValueError(f"{path_label} entry missing 'id': {entry!r}")
    if col_id in seen_ids:
        raise ValueError(f"{path_label} has duplicate id {col_id!r}")
    seen_ids.add(col_id)
    label = entry.get("label", "")
    if not isinstance(label, str):
        raise ValueError(f"{path_label}[{col_id}].label must be a string")
    kind = entry.get("kind", "value")
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"{path_label}[{col_id}].kind must be one of {_VALID_KINDS}, got {kind!r}"
        )
    path = entry.get("path", "")
    if path is not None and not isinstance(path, str):
        raise ValueError(f"{path_label}[{col_id}].path must be a string")
    if kind == "value" and not path:
        raise ValueError(f"{path_label}[{col_id}].path is required when kind='value'")
    width = entry.get("width")
    if width is not None:
        # bool is a subclass of int — reject explicitly to catch `width: true`.
        if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
            raise ValueError(
                f"{path_label}[{col_id}].width must be a positive integer (pixels)"
            )
    align = entry.get("align")
    if align is not None and align not in _VALID_ALIGN:
        raise ValueError(
            f"{path_label}[{col_id}].align must be one of {_VALID_ALIGN}, got {align!r}"
        )
    toggle = entry.get("toggle")
    if toggle is not None and not isinstance(toggle, str):
        raise ValueError(f"{path_label}[{col_id}].toggle must be a string")
    value_icons = _parse_value_icons(
        entry.get("value_icons", ()),
        f"{path_label}[{col_id}].value_icons",
    )
    default_icon = entry.get("default_icon")
    if default_icon is not None:
        if not isinstance(default_icon, str):
            raise ValueError(f"{path_label}[{col_id}].default_icon must be a string")
        _validate_icon_token(default_icon, f"{path_label}[{col_id}].default_icon")
    hide_if_all = entry.get("hide_if_all", ())
    if not isinstance(hide_if_all, (list, tuple)):
        raise ValueError(f"{path_label}[{col_id}].hide_if_all must be a list")
    return UiColumn(
        id=col_id,
        label=label,
        path=path or "",
        kind=kind,
        width=width,
        align=align,
        flex=bool(entry.get("flex", False)),
        toggle=toggle,
        badge=bool(entry.get("badge", False)),
        truncate=bool(entry.get("truncate", False)),
        value_icons=value_icons,
        default_icon=default_icon,
        hide_if_all=tuple(hide_if_all),
    )


def _parse_value_icons(raw: Any, path_label: str) -> tuple[tuple[str, str], ...]:
    if raw in (None, ()):
        return ()
    if not isinstance(raw, Mapping):
        raise ValueError(f"{path_label} must be a mapping")
    out: list[tuple[str, str]] = []
    for value, token in raw.items():
        if not isinstance(value, str) or not value:
            raise ValueError(f"{path_label} keys must be non-empty strings")
        if not isinstance(token, str):
            raise ValueError(f"{path_label}[{value!r}] must be a string")
        _validate_icon_token(token, f"{path_label}[{value!r}]")
        out.append((value, token))
    return tuple(out)


def _validate_icon_token(token: str, path_label: str) -> None:
    if token not in _VALID_ICON_TOKENS:
        allowed = ", ".join(sorted(_VALID_ICON_TOKENS))
        raise ValueError(
            f"{path_label} has unknown icon token {token!r}; expected one of: {allowed}"
        )
