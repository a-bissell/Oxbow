"""Widgets generated from the configuration schema.

Every leaf of ``Config`` becomes one widget chosen by its type and Field bounds, so a knob added
to the schema appears on the Admin page without UI code. Each widget shows the value it would
fall back to and where that value comes from; fields an environment variable overrides are
disabled with the variable named. The converters at the bottom are pure and unit-tested; the
``st.data_editor`` round trip turns ``None`` into ``nan`` and ints into floats, and they undo that.
"""

from __future__ import annotations

import math
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Union, get_args, get_origin

import streamlit as st
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from oxide_triage.config import (
    READ_ONLY_PATHS,
    Config,
    HazardTable,
    env_locked,
    leaf_fields,
)

Kind = Literal[
    "readonly",
    "bool",
    "int",
    "float",
    "literal",
    "str",
    "optional_str",
    "list_str",
    "list_int",
    "int_map",
    "str_float_map",
    "str_map",
    "model_map",
]
# dict-typed leaves whose keys are fixed by a validator: rows may be edited, not added or removed
FIXED_ROWS = frozenset({"missing_data.confidence_thresholds"})
# list fields chosen from the hazard table rather than typed
ELEMENT_LISTS = frozenset({"toxicity.element_blocklist", "toxicity.element_allowlist"})
TIER_LISTS = frozenset({"toxicity.blocklist_tiers"})


@dataclass(frozen=True)
class FieldSpec:
    path: str
    kind: Kind
    options: tuple[Any, ...] = ()
    ge: float | None = None
    le: float | None = None
    gt: float | None = None
    lt: float | None = None
    model: type[BaseModel] | None = None

    @property
    def section(self) -> str:
        return self.path.split(".", 1)[0]

    @property
    def label(self) -> str:
        return self.path.split(".", 1)[1] if "." in self.path else self.path


def _bounds(info: FieldInfo) -> dict[str, float | None]:
    out: dict[str, float | None] = {"ge": None, "le": None, "gt": None, "lt": None}
    for meta in info.metadata:
        for attr in out:
            if hasattr(meta, attr):
                out[attr] = getattr(meta, attr)
    return out


def classify(path: str, info: FieldInfo) -> FieldSpec:
    ann = info.annotation
    origin = get_origin(ann)
    args = get_args(ann)
    bounds = _bounds(info)
    if path in READ_ONLY_PATHS:
        return FieldSpec(path, "readonly")
    if ann is bool:
        return FieldSpec(path, "bool")
    if ann is int:
        return FieldSpec(path, "int", **bounds)
    if ann is float:
        return FieldSpec(path, "float", **bounds)
    if origin is Literal:
        return FieldSpec(path, "literal", options=tuple(args))
    if ann is str:
        return FieldSpec(path, "str")
    if origin in (Union, types.UnionType) and set(args) == {str, type(None)}:
        return FieldSpec(path, "optional_str")
    if origin is list:
        return FieldSpec(path, "list_int" if args and args[0] is int else "list_str")
    if origin is dict:
        key_t, val_t = args
        if isinstance(val_t, type) and issubclass(val_t, BaseModel):
            return FieldSpec(path, "model_map", model=val_t)
        if key_t is int:
            return FieldSpec(path, "int_map")
        if val_t is float:
            return FieldSpec(path, "str_float_map")
        return FieldSpec(path, "str_map")
    raise TypeError(f"{path}: no widget for annotation {ann!r}")


def all_specs() -> list[FieldSpec]:
    return [classify(path, info) for path, info in leaf_fields(Config).items()]


def specs_for(section: str) -> list[FieldSpec]:
    return [s for s in all_specs() if s.section == section]


def sections() -> list[str]:
    """Top-level sections that have at least one widget, in declaration order."""
    present = {s.section for s in all_specs()}
    return [name for name in Config.model_fields if name in present]


# ---- value helpers -----------------------------------------------------------------------------


def get_path(data: dict[str, Any], path: str, default: Any = None) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def fmt_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, list):
        return ", ".join(fmt_value(v) for v in value) or "(none)"
    if isinstance(value, dict):
        return ", ".join(f"{k}: {fmt_value(v)}" for k, v in value.items()) or "(none)"
    return str(value)


def clean_cell(value: Any) -> Any:
    """``nan`` (an emptied data_editor cell) -> None; whole floats -> int."""
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if value.is_integer():
            return int(value)
    return value


def parse_csv_list(text: str, item: type = str) -> list[Any]:
    out = []
    for tok in text.split(","):
        tok = tok.strip()
        if tok:
            out.append(item(tok))
    return out


def records_to_int_map(rows: list[dict[str, Any]]) -> dict[int, float]:
    out: dict[int, float] = {}
    for r in rows:
        k, v = clean_cell(r.get("key")), clean_cell(r.get("value"))
        if k is None or v is None:
            continue
        out[int(k)] = float(v)
    return out


def records_to_str_map(rows: list[dict[str, Any]], cast: Callable[[Any], Any] = str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for r in rows:
        k, v = clean_cell(r.get("key")), clean_cell(r.get("value"))
        if k in (None, "") or v is None or v == "":
            continue
        out[str(k).strip()] = cast(v)
    return out


def records_to_models(
    rows: list[dict[str, Any]], model: type[BaseModel], key: str = "key"
) -> dict[str, dict[str, Any]]:
    """Rows -> {name: validated sub-model dump without None fields}, which equals the YAML shape
    an admin would write. Invalid rows raise the pydantic error, which the page shows next to
    the pending changes."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        name = clean_cell(r.get(key))
        if name in (None, ""):
            continue
        fields = {k: clean_cell(v) for k, v in r.items() if k != key}
        out[str(name).strip()] = model.model_validate(fields).model_dump(exclude_none=True)
    return out


# ---- widgets -----------------------------------------------------------------------------------


def _help(reference: Any, origin: str, env_var: str | None) -> str:
    text = f"shipped: {fmt_value(reference)} (from {origin})"
    if env_var:
        text += f" · set by {env_var} in the environment; the site value is ignored while it is set"
    return text


def _map_editor(spec: FieldSpec, value: Any, key: str, disabled: bool, help_text: str) -> Any:
    if spec.kind == "model_map":
        assert spec.model is not None
        rows = [{"key": k, **(v if isinstance(v, dict) else {})} for k, v in (value or {}).items()]
        if not rows:
            rows = [{"key": "", **{f: None for f in spec.model.model_fields}}]
        st.caption(f"{spec.label} — {help_text}")
        edited = st.data_editor(
            rows, key=key, disabled=disabled, num_rows="dynamic", hide_index=True, width="stretch"
        )
        return records_to_models(edited, spec.model)
    rows = [{"key": k, "value": v} for k, v in (value or {}).items()]
    if not rows:
        rows = [{"key": None, "value": None}]
    st.caption(f"{spec.label} — {help_text}")
    edited = st.data_editor(
        rows,
        key=key,
        disabled=disabled,
        num_rows="fixed" if spec.path in FIXED_ROWS else "dynamic",
        hide_index=True,
        width="stretch",
    )
    if spec.kind == "int_map":
        return records_to_int_map(edited)
    if spec.kind == "str_float_map":
        return records_to_str_map(edited, float)
    return records_to_str_map(edited, str)


def render_field(
    spec: FieldSpec,
    value: Any,
    *,
    reference: Any,
    origin: str,
    key: str,
    disabled: bool,
    table: HazardTable,
) -> Any:
    """One widget; returns the edited value in the shape the config expects."""
    env_var = env_locked(spec.path)
    disabled = disabled or env_var is not None
    help_text = _help(reference, origin, env_var)
    label = spec.label
    k = spec.kind
    if k == "readonly":
        st.text_input(label, value=str(value), key=key, disabled=True, help="read-only")
        return value
    if k == "bool":
        return st.toggle(label, value=bool(value), key=key, disabled=disabled, help=help_text)
    if k == "int":
        lo = int(spec.ge) if spec.ge is not None else (int(spec.gt) + 1 if spec.gt is not None else None)
        hi = int(spec.le) if spec.le is not None else (int(spec.lt) - 1 if spec.lt is not None else None)
        return int(
            st.number_input(
                label,
                value=int(value),
                min_value=lo,
                max_value=hi,
                step=1,
                key=key,
                disabled=disabled,
                help=help_text,
            )
        )
    if k == "float":
        lo = float(spec.ge) if spec.ge is not None else None
        hi = float(spec.le) if spec.le is not None else None
        step = 1.0 if abs(float(value)) >= 20 else 0.01
        return float(
            st.number_input(
                label,
                value=float(value),
                min_value=lo,
                max_value=hi,
                step=step,
                format="%.4g",
                key=key,
                disabled=disabled,
                help=help_text,
            )
        )
    if k == "literal":
        options = list(spec.options)
        index = options.index(value) if value in options else 0
        return st.selectbox(label, options, index=index, key=key, disabled=disabled, help=help_text)
    if k == "str":
        if spec.path == "description":
            return st.text_area(label, value=str(value), key=key, disabled=disabled, help=help_text)
        return st.text_input(label, value=str(value), key=key, disabled=disabled, help=help_text)
    if k == "optional_str":
        text = st.text_input(label, value=value or "", key=key, disabled=disabled, help=help_text)
        return text.strip() or None
    if k == "list_str":
        current = list(value or [])
        if spec.path in ELEMENT_LISTS:
            options = sorted(set(table.tiers) | set(current))
            return st.multiselect(label, options, default=current, key=key, disabled=disabled, help=help_text)
        text = st.text_input(
            label, value=", ".join(current), key=key, disabled=disabled, help=help_text + " · comma-separated"
        )
        return parse_csv_list(text, str)
    if k == "list_int":
        current = list(value or [])
        if spec.path in TIER_LISTS:
            options = sorted(set(table.tiers.values()) | set(current))
            return st.multiselect(label, options, default=current, key=key, disabled=disabled, help=help_text)
        text = st.text_input(
            label, value=", ".join(str(v) for v in current), key=key, disabled=disabled, help=help_text
        )
        return parse_csv_list(text, int)
    return _map_editor(spec, value, key, disabled, help_text)


def render_section(
    section: str,
    current: dict[str, Any],
    reference: dict[str, Any],
    origin_of: Callable[[str], str],
    *,
    keygen: Callable[[str], str],
    disabled: bool,
    table: HazardTable,
) -> dict[str, Any]:
    """Render every leaf of ``section`` and return the section's edited values as a nested dict."""
    edited: dict[str, Any] = {}
    for spec in specs_for(section):
        value = render_field(
            spec,
            get_path(current, spec.path),
            reference=get_path(reference, spec.path),
            origin=origin_of(spec.path),
            key=keygen(spec.path),
            disabled=disabled,
            table=table,
        )
        set_path(edited, spec.path, value)
    return edited
