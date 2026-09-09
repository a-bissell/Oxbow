"""Dialogue primitives shared by the Streamlit app and the MCP server.

A triage result is a complete, self-describing object, so follow-up questions are answered from
it without re-running anything: "why is X ranked where it is", "why was Y excluded", "rerun with
the gap gate at 3.5 eV". Nothing here computes a new number; ``rerun`` goes back through the
deterministic core with changed criteria and the deviations it produces are surfaced as usual.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Any

from oxide_triage.config import CRITERIA, Config
from oxide_triage.schemas import (
    Criteria,
    Deviation,
    GuardDecision,
    RequestBin,
    ScoredCandidate,
    TriageResult,
)

CHANGEABLE = {
    "top_k",
    "min_band_gap_ev",
    "max_energy_above_hull_ev_atom",
    "max_elements",
    "include_elements",
    "exclude_elements",
    "allow_elements",
    "weight_overrides",
    "output_template",
}


class ResultStore:
    """Small in-memory LRU of recent results, keyed by a short id, so a client can follow up."""

    def __init__(self, capacity: int = 50):
        self.capacity = capacity
        self._items: OrderedDict[str, TriageResult] = OrderedDict()

    def put(self, result: TriageResult) -> str:
        blob = result.model_dump_json()
        rid = hashlib.sha256(blob.encode()).hexdigest()[:10]
        self._items[rid] = result
        self._items.move_to_end(rid)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)
        return rid

    def get(self, result_id: str) -> TriageResult | None:
        item = self._items.get(result_id)
        if item is not None:
            self._items.move_to_end(result_id)
        return item

    def ids(self) -> list[str]:
        return list(self._items)


def all_candidates(result: TriageResult) -> list[ScoredCandidate]:
    return result.shortlist + result.ranked_beyond_shortlist + result.excluded


def find_candidate(result: TriageResult, key: str) -> ScoredCandidate | None:
    key_l = key.strip().lower()
    for sc in all_candidates(result):
        if sc.record.material_id.lower() == key_l:
            return sc
    matches = [sc for sc in all_candidates(result) if sc.record.formula.lower() == key_l]
    if not matches:
        return None
    ranked = [m for m in matches if m.rank is not None]
    return min(ranked, key=lambda s: s.rank or 0) if ranked else matches[0]


def explain_candidate(result: TriageResult, key: str) -> str:
    sc = find_candidate(result, key)
    if sc is None:
        known = sorted({s.record.formula for s in all_candidates(result)})
        return f"No candidate '{key}' in this result. Known formulas: {', '.join(known)}"
    r = sc.record
    lines = [
        f"# {r.formula} ({r.material_id}) — {r.crystal_system or '?'} {r.spacegroup_symbol or ''}".rstrip()
    ]
    if result.fixture_data:
        lines.append("> SYNTHETIC FIXTURE DATA: every value below is illustrative.")
    if sc.excluded:
        lines.append("**Excluded by a gate.** Reasons: " + "; ".join(sc.exclusion_reasons))
    else:
        n_pass = len(result.shortlist) + len(result.ranked_beyond_shortlist)
        lines.append(
            f"**Rank {sc.rank} of {n_pass} passing.** Adjusted score {sc.adjusted_score:.4f} "
            f"(raw on available data {sc.raw_score:.4f}, coverage {sc.data_coverage:.0%}, confidence {sc.confidence})."
        )
    if sc.missing_criteria:
        lines.append("**No data for:** " + ", ".join(sc.missing_criteria))
    lines.append("")
    lines.append("| Gate | Threshold | Observed | Result |")
    lines.append("|---|---|---|---|")
    for g in sc.gates:
        res = "pass" if g.passed else ("indeterminate" if g.passed is None else "FAIL")
        lines.append(f"| {g.gate} | {g.threshold_label} | {g.observed_label} | {res} |")
    lines.append("")
    lines.append("| Component | Weight | Observed | Normalised | Contribution | Status |")
    lines.append("|---|---|---|---|---|---|")
    for c in sc.components:
        norm = "—" if c.normalized is None else f"{c.normalized:.3f}"
        contrib = "—" if c.contribution is None else f"{c.contribution:.4f}"
        lines.append(
            f"| {c.criterion} | {c.weight:.3f} | {c.raw_label} | {norm} | {contrib} | {c.status.value} |"
        )
    lines.append("")
    lines.append(f"Band gap: {sc.band_gap_assessment.correction_note}")
    lines.append(f"Cross-source stability check: {sc.cross_source_agreement}.")
    lines.append(f"Scoring rule: `{result.scoring.formula}`")
    if sc.caveats:
        lines.append("")
        lines.append("Caveats:")
        for c in sc.caveats:
            lines.append(f"- [{c.severity}] {c.code} ({c.origin}): {c.text}")
    prov = r.stability.provenance
    if prov:
        lines.append("")
        lines.append(
            f"Source: {prov.source} {prov.source_id or ''} retrieved {prov.retrieved_at}; band gap functional {r.band_gap.functional}."
        )
    return "\n".join(lines)


def apply_changes(criteria: Criteria, changes: dict[str, Any]) -> tuple[Criteria, list[str]]:
    """Return new criteria with ``changes`` applied, plus notes describing each change.
    Unknown keys are rejected so a client cannot reach fields the schema does not expose."""
    unknown = sorted(set(changes) - CHANGEABLE)
    if unknown:
        raise ValueError(f"Cannot change {unknown}; changeable fields: {sorted(CHANGEABLE)}")
    data = criteria.model_dump()
    notes: list[str] = []
    for k, v in changes.items():
        if k == "weight_overrides":
            if not isinstance(v, dict):
                raise ValueError("weight_overrides must be a mapping criterion -> weight")
            bad = sorted(set(v) - set(CRITERIA))
            if bad:
                raise ValueError(f"Unknown criteria {bad}; valid: {list(CRITERIA)}")
            data[k] = {**data.get(k, {}), **{kk: float(vv) for kk, vv in v.items()}}
        else:
            data[k] = v
        notes.append(f"rerun: {k} = {v!r}")
    data["interpretation_notes"] = list(criteria.interpretation_notes) + notes
    return Criteria.model_validate(data), notes


def clarifications(
    criteria: Criteria, deviations: list[Deviation], guard: GuardDecision, config: Config
) -> list[str]:
    """Questions worth asking before running, because the answer changes the run materially."""
    qs: list[str] = []
    request_devs = [d for d in deviations if d.origin == "request"]
    if criteria.allow_elements:
        qs.append(
            f"The request lifts the hazard block for {', '.join(criteria.allow_elements)} under profile "
            f"'{config.profile_name}'. Confirm this is intended; the deviation will be printed on the result and logged."
        )
    for d in request_devs:
        if d.code in {"request_hull_threshold", "request_gap_threshold", "request_max_elements"}:
            qs.append(f"Confirm threshold change: {d.description}")
    zeroed = sorted(k for k, v in criteria.weight_overrides.items() if v == 0)
    if zeroed:
        qs.append(f"The request removes {', '.join(zeroed)} from the scoring entirely (weight 0). Confirm.")
    impossible = [f for f in guard.findings if f.bin == RequestBin.IMPOSSIBLE]
    if impossible and guard.proceed:
        qs.append(
            "Part of the request cannot be done in this deployment ("
            + "; ".join(f'"{f.matched_text}"' for f in impossible)
            + "). Proceed with the triage part only?"
        )
    return qs
