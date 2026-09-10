"""Adaptive acquisition: attack gaps in the retrieved data with alternative read-only routes.

After a cache warm the data layer knows exactly what it could not find: no OQMD match for a
formula, no literature counts, an unresolved band-gap functional, no DFPT dielectric record.
This module turns each such gap into a small plan drawn from an **allowlist of routes**, each a
read-only query against a public source that stores its result in the cache under the same key
the normal path reads. Downstream code is unchanged; provenance says which route produced a
value.

Where the agency is, and where it is not:
  * A planner orders the allowed routes for a gap. The default planner is a deterministic ladder.
    An optional model planner may reorder or skip routes; its output is validated to be a subset of
    the allowlist, otherwise the ladder is used. The model chooses *what to try*, never a value.
  * A gap with no public route (per-material dielectric constants beyond MP's DFPT set) is
    reported as unfillable with the reason. It is never estimated.
  * Every attempt is recorded: gap, route, outcome. The report is stored in the cache and
    summarised on every result rendered from that cache.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from oxide_triage.cache import Cache, utcnow_iso
from oxide_triage.edges.llm import LLMClient, wrap_retrieved
from oxide_triage.schemas import CandidateRecord, DataStatus
from oxide_triage.sources.assemble import DataLayer
from oxide_triage.sources.base import SourceError

log = logging.getLogger(__name__)

META_KEY = "acquisition"
GapKind = Literal["cross_check", "literature", "functional", "dielectric"]
Outcome = Literal["filled", "no_match", "error", "not_applicable", "skipped_offline"]


class Gap(BaseModel):
    material_id: str
    formula: str
    kind: GapKind
    detail: str


class Attempt(BaseModel):
    material_id: str
    formula: str
    kind: GapKind
    route: str
    outcome: Outcome
    note: str = ""


class AcquisitionReport(BaseModel):
    started_at: str
    finished_at: str
    planner: str
    n_gaps: int
    n_filled: int
    budget: int
    budget_exhausted: bool
    attempts: list[Attempt] = Field(default_factory=list)
    unfillable: list[Gap] = Field(default_factory=list)  # no public route exists

    def summary(self) -> dict[str, Any]:
        by_kind: dict[str, dict[str, int]] = {}
        for a in self.attempts:
            d = by_kind.setdefault(a.kind, {"attempts": 0, "filled": 0})
            d["attempts"] += 1
            d["filled"] += a.outcome == "filled"
        return {
            "finished_at": self.finished_at,
            "planner": self.planner,
            "gaps": self.n_gaps,
            "filled": self.n_filled,
            "unfillable": len(self.unfillable),
            "budget_exhausted": self.budget_exhausted,
            "by_kind": by_kind,
        }


# --------------------------------------------------------------------------------------
# Gap detection
# --------------------------------------------------------------------------------------


def detect_gaps(records: list[CandidateRecord]) -> list[Gap]:
    gaps: list[Gap] = []
    for r in records:
        if r.cross_check.status != DataStatus.KNOWN:
            gaps.append(
                Gap(
                    material_id=r.material_id,
                    formula=r.formula,
                    kind="cross_check",
                    detail="no OQMD match by composition",
                )
            )
        if r.literature.status != DataStatus.KNOWN or (r.literature.total_works == 0):
            gaps.append(
                Gap(
                    material_id=r.material_id,
                    formula=r.formula,
                    kind="literature",
                    detail="literature lookup failed"
                    if r.literature.status != DataStatus.KNOWN
                    else "formula search returned no works",
                )
            )
        if r.band_gap.status == DataStatus.KNOWN and r.band_gap.functional in (None, "unknown"):
            gaps.append(
                Gap(
                    material_id=r.material_id,
                    formula=r.formula,
                    kind="functional",
                    detail="band-gap functional unresolved",
                )
            )
        if r.dielectric.status != DataStatus.KNOWN:
            gaps.append(
                Gap(
                    material_id=r.material_id,
                    formula=r.formula,
                    kind="dielectric",
                    detail="no DFPT dielectric record",
                )
            )
    return gaps


# --------------------------------------------------------------------------------------
# Routes (the allowlist). Each is read-only and writes only to the cache.
# --------------------------------------------------------------------------------------

RouteFn = Callable[[DataLayer, Gap], tuple[Outcome, str]]


def _route_oqmd_chemsys(layer: DataLayer, gap: Gap) -> tuple[Outcome, str]:
    payload, _, status = layer.oqmd.lookup_by_chemsys(gap.formula)
    if status == "missing_offline":
        return "skipped_offline", "offline"
    if payload is None:
        return "error", status
    return (
        ("filled", f"OQMD entry {payload.get('entry_id')} via {payload.get('filter')}")
        if payload.get("found")
        else (
            "no_match",
            f"{payload.get('n_entries', 0)} entries in chemical system, none with this stoichiometry",
        )
    )


def _route_openalex_retry(layer: DataLayer, gap: Gap) -> tuple[Outcome, str]:
    if layer.offline:
        return "skipped_offline", "offline"
    names = layer.aliases.get(gap.formula, [])
    payload, _, status = layer.openalex.evidence(gap.formula, names)
    if payload is None:
        return "error", status
    if status == "cached" and payload.get("total_works", 0) == 0:
        return "not_applicable", "cached result already zero; retry would return the same"
    return (
        ("filled", f"{payload.get('total_works')} works")
        if payload.get("total_works", 0) > 0
        else ("no_match", "still zero works")
    )


def _route_openalex_names_only(layer: DataLayer, gap: Gap) -> tuple[Outcome, str]:
    names = layer.aliases.get(gap.formula, [])
    payload, _, status = layer.openalex.evidence_names_only(gap.formula, names)
    if status == "not_applicable":
        return "not_applicable", "no common name known for this formula"
    if status == "missing_offline":
        return "skipped_offline", "offline"
    if payload is None:
        return "error", status
    return (
        ("filled", f"{payload.get('total_works')} works via names {names}")
        if payload.get("total_works", 0) > 0
        else (
            "no_match",
            "zero works under common names too",
        )
    )


def _route_mp_refresh_functional(layer: DataLayer, gap: Gap) -> tuple[Outcome, str]:
    if layer.offline:
        return "skipped_offline", "offline"
    try:
        functional = layer.mp.refresh_functional(gap.material_id)
    except SourceError as exc:
        return "error", str(exc)
    return ("filled", functional) if functional != "unknown" else ("no_match", "run_type still unresolved")


ROUTES: dict[str, RouteFn] = {
    "oqmd_chemsys": _route_oqmd_chemsys,
    "openalex_retry": _route_openalex_retry,
    "openalex_names_only": _route_openalex_names_only,
    "mp_refresh_functional": _route_mp_refresh_functional,
}

LADDER: dict[str, list[str]] = {
    "cross_check": ["oqmd_chemsys"],
    "literature": ["openalex_retry", "openalex_names_only"],
    "functional": ["mp_refresh_functional"],
    "dielectric": [],  # no public per-material route beyond MP DFPT
}

NO_ROUTE_REASON = {
    "dielectric": (
        "No public per-material dielectric source beyond Materials Project's DFPT set is wired in. "
        "The JARVIS-DFT bulk dataset (OptB88vdW dielectric tensors) is a candidate future route; "
        "until then the value stays unknown."
    ),
}


# --------------------------------------------------------------------------------------
# Planners
# --------------------------------------------------------------------------------------


class Planner(Protocol):
    name: str

    def order(self, gap: Gap, allowed: list[str], tried: list[str]) -> list[str]: ...


class LadderPlanner:
    name = "ladder"

    def order(self, gap: Gap, allowed: list[str], tried: list[str]) -> list[str]:
        return [r for r in allowed if r not in tried]


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "routes": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string", "maxLength": 200},
    },
    "required": ["routes", "reason"],
}
PLAN_SYSTEM = (
    "You choose the ORDER in which to try data-acquisition routes for one gap in a materials "
    "record. You may only return route names from the allowed list, in the order to try them; "
    "you may omit routes you judge pointless. You cannot add routes and you never supply values."
)


class LLMPlanner:
    """Model orders the allowed routes. Output validated to a subset of the allowlist."""

    def __init__(self, llm: LLMClient, fallback: Planner | None = None):
        self.llm = llm
        self.fallback = fallback or LadderPlanner()
        self.name = f"llm:{llm.name}"

    def order(self, gap: Gap, allowed: list[str], tried: list[str]) -> list[str]:
        candidates = [r for r in allowed if r not in tried]
        if len(candidates) <= 1:
            return candidates
        user = "Gap and allowed routes follow as data.\n" + wrap_retrieved(
            {"gap": gap.model_dump(), "allowed_routes": candidates, "already_tried": tried}, "acquisition_gap"
        )
        data = self.llm.complete_json(PLAN_SYSTEM, user, PLAN_SCHEMA)
        if not data or not isinstance(data.get("routes"), list):
            return self.fallback.order(gap, allowed, tried)
        chosen = [r for r in data["routes"] if isinstance(r, str) and r in candidates]
        chosen = list(dict.fromkeys(chosen))  # dedupe, keep order
        return chosen or self.fallback.order(gap, allowed, tried)


def make_planner(name: str, llm: LLMClient | None) -> Planner:
    if name == "llm" and llm is not None and llm.name != "none":
        return LLMPlanner(llm)
    return LadderPlanner()


# --------------------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------------------


def fill_gaps(
    layer: DataLayer,
    records: list[CandidateRecord],
    planner: Planner | None = None,
    budget: int = 200,
    routes: dict[str, RouteFn] | None = None,
    ladder: dict[str, list[str]] | None = None,
) -> AcquisitionReport:
    """Try alternative routes for every detected gap, within ``budget`` attempts. Records are
    not rebuilt here; the caller re-reads the cache (``layer.build_candidates()``)."""
    planner = planner or LadderPlanner()
    routes = routes or ROUTES
    ladder = ladder or LADDER
    started = utcnow_iso()
    gaps = detect_gaps(records)
    attempts: list[Attempt] = []
    unfillable: list[Gap] = []
    filled = 0
    spent = 0
    exhausted = False

    for gap in gaps:
        allowed = [r for r in ladder.get(gap.kind, []) if r in routes]
        if not allowed:
            unfillable.append(gap)
            continue
        tried: list[str] = []
        done = False
        while not done:
            plan = planner.order(gap, allowed, tried)
            if not plan:
                break
            route = plan[0]
            if spent >= budget:
                exhausted = True
                break
            spent += 1
            tried.append(route)
            try:
                outcome, note = routes[route](layer, gap)
            except Exception as exc:  # a broken route must not kill the pass
                outcome, note = "error", f"{type(exc).__name__}: {exc}"
                log.warning("acquisition route %s failed for %s: %s", route, gap.formula, exc)
            attempts.append(
                Attempt(
                    material_id=gap.material_id,
                    formula=gap.formula,
                    kind=gap.kind,
                    route=route,
                    outcome=outcome,
                    note=note,
                )
            )
            if outcome == "filled":
                filled += 1
                done = True
            elif outcome == "skipped_offline":
                done = True
        if exhausted:
            break

    report = AcquisitionReport(
        started_at=started,
        finished_at=utcnow_iso(),
        planner=planner.name,
        n_gaps=len(gaps),
        n_filled=filled,
        budget=budget,
        budget_exhausted=exhausted,
        attempts=attempts,
        unfillable=unfillable,
    )
    layer.cache.set_meta(META_KEY, report.model_dump_json())
    return report


def read_report(cache: Cache) -> AcquisitionReport | None:
    raw = cache.get_meta(META_KEY)
    if not raw:
        return None
    try:
        return AcquisitionReport.model_validate(json.loads(raw))
    except ValueError:
        return None
