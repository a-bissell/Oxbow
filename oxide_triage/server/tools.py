"""The assistant's tools: one registry used by both agent drivers.

Every tool is one of the operations the MCP server already exposes (triage, explain, rerun,
add material, cache status, profiles) plus two conveniences (compare, list a section). A tool
returns text for a model to read and, where a result was produced, the id of the stored result
object for the front end to render. No tool computes a number: they call the pipeline and the
session layer and pass through what those return.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from oxide_triage.cache import Cache
from oxide_triage.config import Config, list_profiles, load_config, load_hazard_table
from oxide_triage.edges.llm import make_llm
from oxide_triage.edges.parse import parse_request as _parse
from oxide_triage.edges.render import render
from oxide_triage.guard import guard_request
from oxide_triage.pipeline import add_material as _add_material
from oxide_triage.pipeline import run_triage
from oxide_triage.progress import ProgressFn
from oxide_triage.schemas import TriageResult
from oxide_triage.scoring.settings import resolve
from oxide_triage.selfcheck import read_selfcheck
from oxide_triage.server.store import Conversation, Scope, SessionStore
from oxide_triage.session import (
    CHANGEABLE,
    apply_changes,
    clarifications,
    compare_candidates,
    explain_candidate,
    find_candidate,
    list_candidates,
)


@dataclass
class ToolContext:
    store: SessionStore
    conv: Conversation
    profile: str
    scope: Scope | None = None
    progress: ProgressFn | None = None
    offline: bool | None = None
    config_loader: Callable[[str], Config] = load_config


@dataclass
class ToolOutput:
    text: str  # what a model reads; also the deterministic driver's raw material
    label: str = ""  # one line for the visible step, e.g. "Ranked 1,640 candidates"
    result_id: str | None = None
    explain: str | None = None  # markdown attached to the turn (explain / compare)
    focus: str | None = None  # candidate the canvas should focus
    questions: list[str] = field(default_factory=list)  # clarify-before-run
    error: bool = False
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[ToolContext, dict[str, Any]], ToolOutput]
    visible: bool = True  # shown as a step in the conversation


# ---- helpers ---------------------------------------------------------------------------------


def _config(ctx: ToolContext) -> Config:
    profile = (ctx.scope.profile if ctx.scope and ctx.scope.profile else None) or ctx.profile
    return ctx.config_loader(profile)


def _scope_overrides(scope: Scope | None) -> dict[str, Any]:
    if scope is None:
        return {}
    out: dict[str, Any] = {}
    for k in ("families", "top_k", "min_band_gap_ev", "max_energy_above_hull_ev_atom", "max_elements"):
        v = getattr(scope, k)
        if v is not None:
            out[k] = v
    return out


def _header(rid: str, result: TriageResult) -> str:
    bits = [f"result_id: {rid}", f"profile: {result.profile_name}", f"selfcheck: {result.selfcheck_status}"]
    if result.fixture_data:
        bits.append("DATA: SYNTHETIC FIXTURE (illustrative only)")
    if result.scope and result.scope.families:
        bits.append(
            f"scope: {', '.join(result.scope.families)} ({result.scope.n_in_scope} of {result.scope.n_universe})"
        )
    return "<!-- " + " | ".join(bits) + " -->\n"


def _finish(ctx: ToolContext, result: TriageResult, label: str) -> ToolOutput:
    rid = ctx.store.put_result(result)
    if not result.needs_confirmation:
        ctx.conv.result_ids.append(rid)  # a held run is stored but is not "the latest result"
    if result.needs_confirmation:
        text = (
            f"NEEDS CONFIRMATION before running (result_id {rid}). Ask the user these questions, then call "
            "the same tool again with confirmed=true if they agree:\n- " + "\n- ".join(result.clarifications)
        )
        return ToolOutput(
            text=text,
            label="Needs confirmation before running",
            result_id=rid,
            questions=list(result.clarifications),
        )
    if not result.guard.proceed:
        return ToolOutput(
            text=_header(rid, result) + result.warnings[0],
            label="Request declined",
            result_id=rid,
            error=True,
        )
    if not result.shortlist and result.warnings:
        return ToolOutput(
            text=_header(rid, result) + result.warnings[0],
            label="No shortlist served",
            result_id=rid,
            error=True,
        )
    return ToolOutput(text=_header(rid, result) + render(result, "pi_summary"), label=label, result_id=rid)


def _latest_result(ctx: ToolContext, result_id: str | None) -> tuple[str | None, TriageResult | None]:
    rid = result_id or ctx.conv.latest_result_id
    if rid is None:
        return None, None
    return rid, ctx.store.get_result(rid)


# ---- tools -----------------------------------------------------------------------------------


def t_triage(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    request = str(args.get("request") or "").strip()
    if not request:
        return ToolOutput(text="A request is required.", error=True, label="No request")
    cfg = _config(ctx)
    cache = Cache(cfg.cache.path)
    started = time.monotonic()
    try:
        result = run_triage(
            request,
            cfg,
            cache=cache,
            offline=ctx.offline,
            confirmed=bool(args.get("confirmed", False)),
            progress=ctx.progress,
            overrides=_scope_overrides(ctx.scope),
        )
    finally:
        cache.close()
    n_pass = len(result.shortlist) + len(result.ranked_beyond_shortlist)
    label = (
        f"Ranked {result.n_candidates_considered} candidates, {n_pass} passed the gates"
        f" · {time.monotonic() - started:.0f} s"
    )
    return _finish(ctx, result, label)


def t_rerun(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    rid, prev = _latest_result(ctx, args.get("result_id"))
    if prev is None:
        return ToolOutput(
            text="No previous result to rerun. Run a triage first.", error=True, label="Nothing to rerun"
        )
    changes = dict(args.get("changes") or {})
    try:
        criteria, notes = apply_changes(prev.criteria, changes)
    except ValueError as exc:
        return ToolOutput(text=f"Rejected: {exc}", error=True, label="Rerun rejected")
    cfg = ctx.config_loader(prev.profile_name)
    cache = Cache(cfg.cache.path)
    started = time.monotonic()
    try:
        result = run_triage(
            prev.request_text.split(" [rerun:")[0] + " [rerun: " + "; ".join(notes) + "]",
            cfg,
            cache=cache,
            offline=ctx.offline,
            criteria=criteria,
            confirmed=bool(args.get("confirmed", False)),
            progress=ctx.progress,
        )
    finally:
        cache.close()
    n_pass = len(result.shortlist) + len(result.ranked_beyond_shortlist)
    out = _finish(
        ctx,
        result,
        f"Reran with {', '.join(f'{k}={v!r}' for k, v in changes.items())} · {n_pass} passed · {time.monotonic() - started:.0f} s",
    )
    out.data["previous_result_id"] = rid
    return out


def t_explain(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    rid, result = _latest_result(ctx, args.get("result_id"))
    if result is None:
        return ToolOutput(
            text="No result to explain from. Run a triage first.", error=True, label="Nothing to explain"
        )
    key = str(args.get("candidate") or "")
    sc = find_candidate(result, key)
    text = explain_candidate(result, key)
    if sc is None:
        return ToolOutput(text=text, error=True, label=f"No candidate {key!r} in result {rid}")
    return ToolOutput(
        text=text,
        label=f"Explained {sc.record.formula} from result {rid} · nothing re-computed",
        result_id=rid,
        explain=text,
        focus=sc.record.material_id,
    )


def t_compare(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    rid, result = _latest_result(ctx, args.get("result_id"))
    if result is None:
        return ToolOutput(
            text="No result to compare from. Run a triage first.", error=True, label="Nothing to compare"
        )
    keys = [str(k) for k in (args.get("candidates") or [])]
    text = compare_candidates(result, keys)
    ok = text.startswith("# Comparison")
    return ToolOutput(
        text=text,
        label=("Compared " + " and ".join(keys)) if ok else "Comparison failed",
        result_id=rid,
        explain=text if ok else None,
        error=not ok,
    )


def t_list(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    rid, result = _latest_result(ctx, args.get("result_id"))
    if result is None:
        return ToolOutput(
            text="No result to list from. Run a triage first.", error=True, label="Nothing to list"
        )
    section = str(args.get("section") or "shortlist")
    limit = int(args.get("limit") or 25)
    return ToolOutput(
        text=list_candidates(result, section, limit), label=f"Listed {section} of result {rid}", result_id=rid
    )


def t_parse(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    cfg = _config(ctx)
    table = load_hazard_table(cfg.toxicity.table_file)
    request = str(args.get("request") or "")
    guard = guard_request(request, table)
    criteria, parser = _parse(request, cfg, table, make_llm(cfg.llm))
    eff, deviations = resolve(cfg, criteria, table)
    lines = [
        f"parsed_by: {parser}",
        f"guard: proceed={guard.proceed}"
        + (f"; findings: {[f.bin.value for f in guard.findings]}" if guard.findings else ""),
        f"criteria: {criteria.model_dump(exclude_defaults=True)}",
        f"effective gates: hull<={eff.max_energy_above_hull:g} eV/atom, gap>={eff.min_band_gap:g} eV, "
        f"<={eff.max_elements} elements, top_k {eff.top_k}, blocked {sorted(eff.blocked_elements)}",
    ]
    if deviations:
        lines.append("deviations: " + "; ".join(d.description for d in deviations))
    qs = clarifications(criteria, deviations, guard, cfg)
    if qs:
        lines.append("clarifications: " + " | ".join(qs))
    return ToolOutput(text="\n".join(lines), label="Read the request without running it")


def t_profiles(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    out = []
    for name in ["default", *list_profiles()]:
        cfg = ctx.config_loader(name)
        g = cfg.gates
        out.append(
            f"{name}: E_hull<={g.max_energy_above_hull_ev_atom:g} eV/atom, effective gap>={g.min_band_gap_ev:g} eV, "
            f"<={g.max_elements} elements, blocked tiers {cfg.toxicity.blocklist_tiers}, allowed despite tier "
            f"{cfg.toxicity.element_allowlist or 'none'}, top_k {cfg.output.top_k}, weights {cfg.weights.model_dump()}. "
            f"{cfg.description.strip()}"
        )
    return ToolOutput(text="\n".join(out), label="Listed the profiles")


def t_cache_status(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    cfg = _config(ctx)
    cache = Cache(cfg.cache.path)
    try:
        summary = cache.sources_summary()
        fixture = cache.has_fixture_data
        sc = read_selfcheck(cache)
    finally:
        cache.close()
    lines = [f"cache: {cfg.cache.path}", f"fixture data: {fixture}"]
    for src, info in summary.items():
        lines.append(f"{src}: {info['n']} rows, newest {info['newest']}")
    if sc is None:
        lines.append("self-check: not run")
    else:
        lines.append(
            f"self-check: {'passed' if sc.passed else 'FAILED'} {sc.checked_at}; " + "; ".join(sc.details)
        )
    return ToolOutput(text="\n".join(lines), label="Checked the cache")


def t_add_material(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    cfg = _config(ctx)
    formula = str(args.get("formula") or "").strip()
    if not formula:
        return ToolOutput(text="A formula is required.", error=True, label="No formula")
    if (
        cfg.cache.offline
        or os.environ.get("OXIDE_TRIAGE_OFFLINE", "").lower() in {"1", "true", "yes"}
        or ctx.offline
    ):
        return ToolOutput(
            text="Deployment is offline; no fetches allowed.",
            error=True,
            label="Offline: cannot add a material",
        )
    out = _add_material(formula, cfg)
    return ToolOutput(
        text=str(out),
        label=f"Added {formula} to the universe" if out.get("added") else f"Could not add {formula}",
        data=dict(out),
    )


def t_suggest(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    items = [str(s) for s in (args.get("suggestions") or [])][:4]
    return ToolOutput(text="ok", label="", data={"suggestions": items})


_CHANGE_SCHEMA = {
    "type": "object",
    "description": "Fields to change. Changeable: " + ", ".join(sorted(CHANGEABLE)) + ".",
    "properties": {
        "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
        "min_band_gap_ev": {"type": "number"},
        "max_energy_above_hull_ev_atom": {"type": "number"},
        "max_elements": {"type": "integer", "minimum": 2, "maximum": 6},
        "include_elements": {"type": "array", "items": {"type": "string"}},
        "exclude_elements": {"type": "array", "items": {"type": "string"}},
        "allow_elements": {"type": "array", "items": {"type": "string"}},
        "weight_overrides": {"type": "object", "additionalProperties": {"type": "number"}},
        "families": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "triage",
            "Rank oxide dielectric candidates for a natural-language request against the active profile and "
            "scope. Returns a rendered summary prefixed by a result_id for follow-ups, or NEEDS CONFIRMATION with "
            "questions when the request changes something material (lifts a hazard block, moves a gate, zeroes a "
            "criterion); then ask the user and call again with confirmed=true. Requests that would fabricate "
            "evidence are refused; lab, private-data and paywalled requests are declined as capabilities this "
            "deployment does not have.",
            {
                "type": "object",
                "properties": {
                    "request": {"type": "string", "description": "The user's request in their own words."},
                    "confirmed": {
                        "type": "boolean",
                        "description": "true only after the user agreed to the clarification questions.",
                    },
                },
                "required": ["request"],
            },
            t_triage,
        ),
        Tool(
            "rerun",
            'Re-run the latest result (or result_id) with changed criteria, e.g. {"min_band_gap_ev": 3.5}, '
            '{"allow_elements": ["Pb"]}, {"top_k": 10}, {"weight_overrides": {"dielectric": 0}}, '
            '{"families": ["early_transition"]}. Changes surface as configuration deviations on the new result. '
            "Returns NEEDS CONFIRMATION first when the change is material and confirmed is false.",
            {
                "type": "object",
                "properties": {
                    "result_id": {
                        "type": "string",
                        "description": "Result to rerun; defaults to the latest.",
                    },
                    "changes": _CHANGE_SCHEMA,
                    "confirmed": {"type": "boolean"},
                },
                "required": ["changes"],
            },
            t_rerun,
        ),
        Tool(
            "explain",
            "Explain one candidate of a result from the stored result object, without recomputing anything: rank, "
            "every score component with weight and contribution, every gate, band-gap correction, provenance, "
            "caveats. Works for excluded candidates too (shows why). Use it whenever the user asks why a material "
            "ranks where it does.",
            {
                "type": "object",
                "properties": {
                    "candidate": {"type": "string", "description": "Formula (HfO2) or material id (mp-352)."},
                    "result_id": {"type": "string", "description": "Defaults to the latest result."},
                },
                "required": ["candidate"],
            },
            t_explain,
        ),
        Tool(
            "compare",
            "Side-by-side gates and score components for two or more candidates of one result, with the largest "
            "difference named. Use it when the user asks how two materials differ or why one is above another.",
            {
                "type": "object",
                "properties": {
                    "candidates": {"type": "array", "items": {"type": "string"}, "minItems": 2},
                    "result_id": {"type": "string"},
                },
                "required": ["candidates"],
            },
            t_compare,
        ),
        Tool(
            "list_candidates",
            "List one section of a result compactly: shortlist, beyond (ranked below the shortlist) or excluded "
            "(with the gate that excluded each). Use it before answering questions about what was left out.",
            {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": ["shortlist", "beyond", "excluded"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "result_id": {"type": "string"},
                },
                "required": ["section"],
            },
            t_list,
        ),
        Tool(
            "parse_request",
            "Show how a request would be read WITHOUT running it: validated criteria, guard decision, deviations "
            "from the profile, clarification questions. Use it when the user asks how something would be interpreted.",
            {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]},
            t_parse,
        ),
        Tool(
            "profiles",
            "List the configuration profiles and what each changes.",
            {"type": "object", "properties": {}},
            t_profiles,
        ),
        Tool(
            "cache_status",
            "What is in the cache: rows per source, freshness, fixture flag, last self-check.",
            {"type": "object", "properties": {}},
            t_cache_status,
        ),
        Tool(
            "add_material",
            "Pull one compound (reduced formula, e.g. SrHfO3) from the public sources into the candidate universe "
            "so later triage calls consider it. Online only; re-runs the self-check.",
            {"type": "object", "properties": {"formula": {"type": "string"}}, "required": ["formula"]},
            t_add_material,
        ),
        Tool(
            "suggest_followups",
            "Attach two to four short follow-up questions the user might click next (each under 60 characters, "
            "phrased as the user would say them, about this result). Call it once at the end of your answer.",
            {
                "type": "object",
                "properties": {"suggestions": {"type": "array", "items": {"type": "string"}, "maxItems": 4}},
                "required": ["suggestions"],
            },
            t_suggest,
            visible=False,
        ),
    ]
}


def model_tool_definitions() -> list[dict[str, Any]]:
    """Tool definitions in the shape the Messages API takes."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in TOOLS.values()
    ]
