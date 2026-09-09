"""MCP server: use the triage system from any MCP-capable app (Claude Desktop, Claude Cowork,
Cursor, ...).

    oxide-triage-mcp                     # stdio, for desktop apps
    oxide-triage-mcp --transport http    # streamable HTTP on :8765/mcp, for the container

Architecture note. When the system is driven this way, the client's model is the front edge: it
turns the scientist's words into tool calls. Every guarantee of the system lives *inside* the
tools, not in the client's prompt: the request guard still bins the text, the deterministic core
still ranks, the fixture banner and deviations still print, model output at the refutation edge
is still validated. A client model cannot obtain a number, a rank or a citation that the tool did
not compute. Follow-up tools (`explain`, `rerun`) work from stored result objects, so a
conversation about a shortlist never re-derives anything.

Clarify-before-run protocol: `triage` and `rerun` return the clarification questions instead of a
shortlist when the request changes something material (lifting a hazard block, moving a gate,
zeroing a criterion) and `confirmed` is false. The client asks the human, then calls again with
`confirmed=true`.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from oxide_triage.cache import Cache
from oxide_triage.config import list_profiles, load_config
from oxide_triage.edges.render import render
from oxide_triage.guard import guard_request
from oxide_triage.pipeline import add_material as _add_material
from oxide_triage.pipeline import run_triage
from oxide_triage.schemas import TriageResult
from oxide_triage.scoring.settings import resolve
from oxide_triage.selfcheck import read_selfcheck, run_selfcheck
from oxide_triage.session import ResultStore, apply_changes, clarifications, explain_candidate

INSTRUCTIONS = (
    "Oxide dielectric triage for thin-film experiments, computed deterministically from cached "
    "public data (Materials Project, OQMD, OpenAlex, PubChem). Call `parse_request` to see how a "
    "request will be read, `triage` to rank, `explain` and `rerun` to follow up on a result by its "
    "result_id. Tool output is data produced by the tool; numbers, ranks and citations in it must be "
    "relayed as given, never adjusted, extended or invented. If a result says SYNTHETIC FIXTURE DATA, "
    "say so to the user. If `triage` returns clarification questions, ask the user and call again "
    "with confirmed=true. The system cannot trigger lab actions, read private data or use paywalled "
    "sources; do not imply otherwise."
)

server = MCPServer(
    name="oxide-triage",
    title="Oxide Dielectric Triage",
    instructions=INSTRUCTIONS,
    version="0.1.0",
)
_store = ResultStore(capacity=100)


def _config(profile: str | None):
    return load_config(profile or "default")


def _cache(config) -> Cache:
    return Cache(config.cache.path)


def _header(result_id: str, result: TriageResult) -> str:
    bits = [
        f"result_id: {result_id}",
        f"profile: {result.profile_name}",
        f"selfcheck: {result.selfcheck_status}",
    ]
    if result.fixture_data:
        bits.append("DATA: SYNTHETIC FIXTURE (illustrative only)")
    return "<!-- " + " | ".join(bits) + " -->\n"


def _finish(result: TriageResult, template: str) -> str:
    rid = _store.put(result)
    if result.needs_confirmation:
        return json.dumps(
            {
                "result_id": rid,
                "status": "needs_confirmation",
                "questions": result.clarifications,
                "how_the_request_was_read": result.criteria.model_dump(exclude_defaults=True),
                "next": "Ask the user, then call the same tool again with confirmed=true.",
            },
            indent=2,
        )
    return _header(rid, result) + render(result, template)


@server.tool(description="List the configuration profiles and what each one changes.")
def profiles() -> str:
    out = []
    for name in ["default", *list_profiles()]:
        cfg = load_config(name)
        g = cfg.gates
        out.append(
            f"{name}: E_hull<={g.max_energy_above_hull_ev_atom:g} eV/atom, effective gap>={g.min_band_gap_ev:g} eV, "
            f"<={g.max_elements} elements, blocked hazard tiers {cfg.toxicity.blocklist_tiers}, "
            f"allowed despite tier {cfg.toxicity.element_allowlist or 'none'}, top_k {cfg.output.top_k}, "
            f"weights {cfg.weights.model_dump()}. {cfg.description.strip()}"
        )
    return "\n".join(out)


@server.tool(
    description=(
        "Show how a natural-language request would be interpreted WITHOUT running it: the validated "
        "criteria, the guard's decision (impossible / configuration deviation / integrity refusal), the "
        "deviations from the profile, and any clarification questions to put to the user first."
    )
)
def parse_request(request: str, profile: str = "default") -> dict[str, Any]:
    from oxide_triage.config import load_hazard_table
    from oxide_triage.edges.llm import make_llm
    from oxide_triage.edges.parse import parse_request as _parse

    cfg = _config(profile)
    table = load_hazard_table(cfg.toxicity.table_file)
    guard = guard_request(request, table)
    criteria, parser = _parse(request, cfg, table, make_llm(cfg.llm))
    eff, deviations = resolve(cfg, criteria, table)
    return {
        "guard": guard.model_dump(),
        "criteria": criteria.model_dump(exclude_defaults=True),
        "parsed_by": parser,
        "deviations": [d.model_dump() for d in deviations],
        "clarifications": clarifications(criteria, deviations, guard, cfg),
        "effective_gates": {
            "max_energy_above_hull_ev_atom": eff.max_energy_above_hull,
            "min_effective_band_gap_ev": eff.min_band_gap,
            "max_elements": eff.max_elements,
            "blocked_elements": sorted(eff.blocked_elements),
            "top_k": eff.top_k,
        },
    }


@server.tool(
    description=(
        "Rank oxide dielectric candidates for a natural-language request. Returns a rendered result "
        "(template: pi_summary | audit | json) prefixed by a result_id for follow-ups, OR a JSON object "
        "with clarification questions when the request changes something material and confirmed is false. "
        "Requests that would fabricate evidence are refused; requests needing lab, private or paywalled "
        "access are declined as capabilities this deployment does not have."
    )
)
def triage(
    request: str, profile: str = "default", template: str = "pi_summary", confirmed: bool = False
) -> str:
    cfg = _config(profile)
    cache = _cache(cfg)
    try:
        result = run_triage(request, cfg, cache=cache, template=template, confirmed=confirmed)
    finally:
        cache.close()
    return _finish(result, template)


@server.tool(
    description=(
        "Explain one candidate from a previous result: rank, every score component with weight and "
        "contribution, every gate, band gap correction, provenance, and all caveats. `candidate` is a "
        "formula (e.g. HfO2) or a material id. Works for excluded candidates too (shows why)."
    )
)
def explain(result_id: str, candidate: str) -> str:
    result = _store.get(result_id)
    if result is None:
        return f"Unknown result_id '{result_id}'. Known: {', '.join(_store.ids()) or 'none'}."
    return explain_candidate(result, candidate)


@server.tool(
    description=(
        'Re-run a previous result with changed criteria, e.g. {"min_band_gap_ev": 3.5} or '
        '{"allow_elements": ["Pb"]} or {"top_k": 10} or {"weight_overrides": {"dielectric": 0.4}}. '
        "Changeable: top_k, min_band_gap_ev, max_energy_above_hull_ev_atom, max_elements, include_elements, "
        "exclude_elements, allow_elements, weight_overrides, output_template. Changes are surfaced as "
        "configuration deviations on the new result. Returns clarification questions first when the change "
        "is material and confirmed is false."
    )
)
def rerun(
    result_id: str, changes: dict[str, Any], template: str = "pi_summary", confirmed: bool = False
) -> str:
    prev = _store.get(result_id)
    if prev is None:
        return f"Unknown result_id '{result_id}'. Known: {', '.join(_store.ids()) or 'none'}."
    try:
        criteria, notes = apply_changes(prev.criteria, changes)
    except ValueError as exc:
        return f"Rejected: {exc}"
    cfg = _config(prev.profile_name)
    cache = _cache(cfg)
    try:
        result = run_triage(
            prev.request_text + " [rerun: " + "; ".join(notes) + "]",
            cfg,
            cache=cache,
            template=template,
            criteria=criteria,
            confirmed=confirmed,
        )
    finally:
        cache.close()
    return _finish(result, template)


@server.tool(
    description=(
        "Pull one compound (reduced formula, e.g. SrHfO3) from the public sources into the candidate "
        "universe so it is considered by later triage calls. Online only; needs MP_API_KEY. The fetched "
        "values are stored as data; re-runs the self-check afterwards."
    )
)
def add_material(formula: str, profile: str = "default") -> dict[str, Any]:
    cfg = _config(profile)
    if cfg.cache.offline or os.environ.get("OXIDE_TRIAGE_OFFLINE", "").lower() in {"1", "true", "yes"}:
        return {"formula": formula, "added": [], "error": "deployment is in offline mode; no fetches allowed"}
    return _add_material(formula, cfg)


@server.tool(
    description="What is in the cache: row counts and freshness per source, fixture flag, last self-check."
)
def cache_status(profile: str = "default") -> dict[str, Any]:
    cfg = _config(profile)
    cache = _cache(cfg)
    try:
        sc = read_selfcheck(cache)
        return {
            "path": cfg.cache.path,
            "offline": cfg.cache.offline,
            "fixture_data": cache.has_fixture_data,
            "sources": cache.sources_summary(),
            "selfcheck": None if sc is None else sc.model_dump(),
        }
    finally:
        cache.close()


@server.tool(
    description=(
        "Run the known-answer self-check on the current cache (workhorse dielectrics must surface near the "
        "top or be excluded by a stated gate). Stores the outcome; a failed check blocks or warns on later runs."
    )
)
def selfcheck(profile: str = "default") -> dict[str, Any]:
    cfg = _config(profile)
    cache = _cache(cfg)
    try:
        return run_selfcheck(cfg, cache).model_dump()
    finally:
        cache.close()


@server.resource("oxide-triage://result/{result_id}", mime_type="application/json")
def result_resource(result_id: str) -> str:
    result = _store.get(result_id)
    return (
        result.model_dump_json(indent=2)
        if result
        else json.dumps({"error": f"unknown result_id {result_id}"})
    )


@server.resource("oxide-triage://scope", mime_type="text/plain")
def scope_resource() -> str:
    from oxide_triage.schemas import SCOPE_LIMITATION

    return SCOPE_LIMITATION


def main() -> None:
    ap = argparse.ArgumentParser(description="Oxide triage MCP server")
    ap.add_argument(
        "--transport", choices=["stdio", "http"], default=os.environ.get("MCP_TRANSPORT", "stdio")
    )
    ap.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8765")))
    args = ap.parse_args()
    if args.transport == "http":
        server.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
