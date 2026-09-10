"""Refutation pass: argue *against* every shortlisted candidate.

A shortlist entry is a conjecture. This stage attaches the known counterexamples as caveats.
It annotates; it never alters a rank or a score, and it never fetches data.

Two layers:
  * Rule-derived caveats (always). Deterministic, from structured facts already retrieved.
  * Optional model elaboration over the same structured facts, delimited as data. Model output
    is validated: it may add at most three observations per candidate, each must cite a fact
    field that exists, and any number it mentions must already appear in the facts
    (``numeric_guard``). Anything else is discarded. The model cannot introduce a value.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import yaml

from oxide_triage.config import DATA_DIR, Config
from oxide_triage.edges.llm import LLMClient, wrap_retrieved
from oxide_triage.schemas import Caveat, DataStatus, ScoredCandidate
from oxide_triage.scoring.settings import Effective

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}
THIN_LITERATURE_THRESHOLD = 10  # thin-film works below which the evidence is called thin
GAP_MARGIN_EV = 0.5
GHS_SERIOUS = re.compile(r"^H(30[0-2]|31[0-2]|33[0-2]|34[01]|35[01]|36[0-2]|37[0-3])")
SHORT_FORMULA_NOISE = re.compile(r"^[A-Z][a-z]?O$")  # CaO, BaO, MgO ... noisy literature search


def _load_hygroscopic() -> dict[str, Any]:
    with (DATA_DIR / "hygroscopic_oxides.yaml").open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


HYGROSCOPIC = _load_hygroscopic()


# --------------------------------------------------------------------------------------
# Rule-derived caveats
# --------------------------------------------------------------------------------------


def rule_caveats(sc: ScoredCandidate, eff: Effective, config: Config) -> list[Caveat]:
    r = sc.record
    out: list[Caveat] = []

    def add(code: str, severity: str, text: str, **evidence: Any) -> None:
        out.append(Caveat(code=code, severity=severity, text=text, origin="rule", evidence=evidence))  # type: ignore[arg-type]

    # Hygroscopicity ---------------------------------------------------------------------
    entry = HYGROSCOPIC.get("formulas", {}).get(r.formula)
    if entry:
        add(
            "hygroscopic_risk",
            entry["severity"],
            f"{r.formula} is known to take up moisture: {entry['basis']}. Handling and capping "
            f"strategy needed; not modeled by this ranking.",
            table_version=HYGROSCOPIC.get("version"),
        )
    elif r.n_elements == 2:
        cation = next((e for e in r.elements if e != "O"), None)
        sev = HYGROSCOPIC.get("binary_cation_rule", {}).get(cation)
        if sev:
            add(
                "hygroscopic_risk",
                sev,
                f"Binary {cation} oxide: hygroscopic class; handling risk not modeled.",
            )

    # Retrieval completeness --------------------------------------------------------------
    # The strongest thing this pass can say about a candidate is that its rank should not be
    # compared with the others at all. Missing data lowers the score, so a candidate the cache
    # failed to fetch is pushed down for a reason that is about the cache, not the material.
    if sc.not_retrieved_criteria:
        add(
            "incomplete_retrieval",
            "critical",
            f"Ranked on incomplete retrieval: {', '.join(sc.not_retrieved_criteria)} "
            f"{'was' if len(sc.not_retrieved_criteria) == 1 else 'were'} never successfully "
            f"fetched into this cache, leaving {sc.retrieval_gap:.0%} of the scoring weight "
            "unretrieved. The score is lowered by data the system failed to collect, not by "
            "anything known about the material, so this rank is not comparable with candidates "
            "whose retrieval completed. Warm the cache and re-run before drawing a comparison.",
            not_retrieved=sc.not_retrieved_criteria,
            retrieval_gap=sc.retrieval_gap,
            data_coverage=sc.data_coverage,
        )

    # Dielectric data ---------------------------------------------------------------------
    if r.dielectric.status == DataStatus.ABSENT:
        add(
            "dielectric_unknown",
            "warning",
            "No DFPT dielectric constant in Materials Project for this entry. The candidate is "
            "ranked on partial data; its dielectric merit is unverified, not low.",
            data_coverage=sc.data_coverage,
        )

    # Stability evidence ------------------------------------------------------------------
    if sc.cross_source_agreement == "unavailable":
        add(
            "single_source_stability",
            "warning",
            "Stability rests on Materials Project alone; no OQMD entry matched the formula for an "
            "independent check.",
        )
    elif sc.cross_source_agreement == "untested":
        add(
            "cross_check_untested",
            "warning",
            "The independent stability cross-check never ran for this candidate on this cache, so "
            "agreement is untested rather than absent. OQMD may well hold an entry; nothing here "
            "says it does not.",
        )
    elif sc.cross_source_agreement == "disagree":
        add(
            "cross_source_disagreement",
            "critical",
            f"Materials Project and OQMD disagree on hull distance "
            f"(MP {r.stability.energy_above_hull_ev_atom:.3f} vs OQMD "
            f"{r.cross_check.stability_ev_atom:.3f} eV/atom; tolerance "
            f"{config.stability.cross_check_tolerance_ev_atom:g}). Polymorph or reference-state "
            "differences are likely; treat stability as contested.",
            mp_e_hull=r.stability.energy_above_hull_ev_atom,
            oqmd_stability=r.cross_check.stability_ev_atom,
        )
    e_hull = r.stability.energy_above_hull_ev_atom
    if e_hull is not None and e_hull > 0:
        add(
            "metastable",
            "info" if e_hull <= 0.025 else "warning",
            f"{e_hull * 1000:.0f} meV/atom above the convex hull: not the computed ground state; "
            "may transform or phase-separate depending on processing.",
            e_hull_ev_atom=e_hull,
        )
    if r.theoretical is True:
        add(
            "theoretical_structure",
            "warning",
            "Materials Project marks this structure as theoretical (no matching experimentally "
            "observed structure). It may never have been synthesised in this form.",
        )

    # Band gap ----------------------------------------------------------------------------
    bg = sc.band_gap_assessment
    if bg.corrected:
        add(
            "band_gap_corrected",
            "info",
            f"Effective gap {bg.effective_ev:.2f} eV is a {config.band_gap.correction.scalar_factor:g}x "
            f"scalar correction of a {bg.reported_functional} value ({bg.reported_ev:.2f} eV), not a "
            "measurement or a hybrid-functional result.",
            reported_ev=bg.reported_ev,
            effective_ev=bg.effective_ev,
            functional=bg.reported_functional,
        )
    if bg.reported_functional in (None, "unknown"):
        add(
            "functional_unknown",
            "warning",
            "The DFT functional behind the band gap could not be resolved; the correction assumed a "
            "semi-local functional.",
        )
    if bg.effective_ev is not None and 0 <= bg.effective_ev - eff.min_band_gap < GAP_MARGIN_EV:
        add(
            "band_gap_near_threshold",
            "warning",
            f"Effective gap {bg.effective_ev:.2f} eV clears the {eff.min_band_gap:g} eV threshold by "
            f"less than {GAP_MARGIN_EV:g} eV; a different correction factor would change the verdict.",
            effective_ev=bg.effective_ev,
            threshold_ev=eff.min_band_gap,
        )

    # Hazards -----------------------------------------------------------------------------
    for el, tier in sorted(r.hazard.element_tiers.items()):
        if tier >= 2:
            add(
                "hazard_allowed_by_config",
                "critical",
                f"{el} is hazard tier 2 ({r.hazard.element_basis.get(el, '')}) and appears only because "
                "the active configuration permits it. Handling, disposal and RoHS implications apply.",
                element=el,
                tier=tier,
            )
        elif tier == 1:
            add(
                "hazard_caution",
                "warning",
                f"{el} is hazard tier 1: {r.hazard.element_basis.get(el, '')}.",
                element=el,
                tier=tier,
            )
        if r.hazard.element_basis.get(el, "").startswith("not in hazard table"):
            add(
                "hazard_table_gap",
                "warning",
                f"The hazard table has no entry for {el}; default tier applied.",
                element=el,
            )
    serious = [c for c in r.hazard.ghs_hazard_codes if GHS_SERIOUS.match(c)]
    if serious:
        add(
            "ghs_hazard_statements",
            "warning",
            f"PubChem GHS record (CID {r.hazard.pubchem_cid}) carries {', '.join(serious)} for the compound.",
            cid=r.hazard.pubchem_cid,
            codes=serious,
        )

    # Literature --------------------------------------------------------------------------
    lit = r.literature
    if lit.status == DataStatus.NOT_RETRIEVED:
        add(
            "literature_not_retrieved",
            "warning",
            "Literature counts were never fetched for this candidate on this cache; evidence "
            "strength is unmeasured, which is not the same as weak.",
        )
    elif lit.status != DataStatus.KNOWN:
        add(
            "literature_unavailable",
            "warning",
            "Literature evidence could not be retrieved; evidence strength unknown.",
        )
    elif (lit.thin_film_works or 0) == 0:
        add(
            "no_thin_film_literature",
            "warning",
            "No OpenAlex works matched both the compound and a thin-film deposition term. Any film "
            "route would be unexplored territory.",
            total_works=lit.total_works,
        )
    elif (lit.thin_film_works or 0) < THIN_LITERATURE_THRESHOLD:
        add(
            "thin_literature",
            "warning",
            f"Only {lit.thin_film_works} thin-film works found (of {lit.total_works} total). Evidence "
            "for a deposition route is thin.",
            thin_film_works=lit.thin_film_works,
            total_works=lit.total_works,
        )
    if SHORT_FORMULA_NOISE.match(r.formula) and lit.status == DataStatus.KNOWN:
        add(
            "literature_count_noisy",
            "info",
            f"'{r.formula}' is a short formula string; OpenAlex counts may include unrelated matches.",
        )

    # Composition & coverage --------------------------------------------------------------
    if r.n_elements >= 4:
        add(
            "complex_composition",
            "info",
            f"{r.n_elements} distinct elements: stoichiometry control in a film is harder.",
        )
    if sc.confidence != "high":
        pct = round(sc.data_coverage * 100)
        add(
            "partial_data",
            "warning" if sc.confidence == "low" else "info",
            f"Ranked on {pct}% of criterion weight; missing: {', '.join(sc.missing_criteria)}. "
            f"Confidence {sc.confidence}.",
            data_coverage=sc.data_coverage,
            missing=list(sc.missing_criteria),
        )
    if r.is_fixture:
        add(
            "fixture_data",
            "critical",
            "Synthetic fixture record: every value above is illustrative, not real.",
        )

    out.sort(key=lambda c: (SEVERITY_RANK[c.severity], c.code))
    return out


def primary_caveat(sc: ScoredCandidate) -> Caveat | None:
    if not sc.caveats:
        return None
    # Fixture banner is shown globally; prefer a substantive caveat as the headline.
    substantive = [c for c in sc.caveats if c.code != "fixture_data"]
    return (substantive or sc.caveats)[0]


# --------------------------------------------------------------------------------------
# Optional model elaboration (over structured facts only)
# --------------------------------------------------------------------------------------

REFUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "observations": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string", "maxLength": 300},
                    "evidence_fields": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["text", "evidence_fields"],
            },
        }
    },
    "required": ["observations"],
}

REFUTE_SYSTEM = (
    "Your job is to argue AGAINST a candidate material for thin-film dielectric experiments, "
    "using only the structured facts provided. Point out weaknesses a bench scientist should "
    "check before committing time. Do not restate caveats already listed. Do not introduce any "
    "number, citation or property that is not present in the facts. Each observation must name "
    "the fact field(s) it is based on."
)

NUMBER_RE = re.compile(r"(?<![A-Za-z\d])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def _decimals(tok: str) -> int:
    return len(tok.split(".", 1)[1]) if "." in tok else 0


LIST_MARKER_RE = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)  # "1. HfO2" is a list, not a claim


def allowed_numbers(values: Iterable[Any]) -> set[float]:
    """Every number that appears in ``values``: numeric values directly, and numbers written
    inside strings (tool output, rendered templates). Booleans are not numbers."""
    allowed: set[float] = set()
    for v in values:
        if isinstance(v, bool):
            continue
        if isinstance(v, int | float):
            allowed.add(float(v))
        elif isinstance(v, str):
            allowed.update(float(n.replace(",", "")) for n in NUMBER_RE.findall(v))
    return allowed


def unverified_numbers(text: str, allowed: set[float]) -> list[str]:
    """Numbers in ``text`` that equal no allowed value once that value is rounded to the
    precision the text used. "5.6" matches 5.63; "2019" does not match 2010. Markdown list
    markers are ignored. Returned in order of first appearance, without duplicates."""
    out: list[str] = []
    for tok in NUMBER_RE.findall(LIST_MARKER_RE.sub("", text)):
        clean = tok.replace(",", "")
        n = float(clean)
        d = _decimals(clean)
        if not any(round(a, d) == n for a in allowed) and tok not in out:
            out.append(tok)
    return out


def numeric_guard(text: str, facts: dict[str, Any]) -> bool:
    """True if every number in ``text`` equals some fact value once that value is rounded to
    the precision the text used. "5.6" matches 5.63; "2019" does not match 2010."""
    return not unverified_numbers(text, allowed_numbers(facts.values()))


def candidate_facts(sc: ScoredCandidate) -> dict[str, Any]:
    r = sc.record
    return {
        "formula": r.formula,
        "material_id": r.material_id,
        "elements": r.elements,
        "crystal_system": r.crystal_system,
        "theoretical_structure": r.theoretical,
        "stability": r.stability.model_dump(exclude={"provenance"}),
        "cross_check_oqmd": r.cross_check.model_dump(exclude={"provenance"}),
        "cross_source_agreement": sc.cross_source_agreement,
        "band_gap": sc.band_gap_assessment.model_dump(),
        "dielectric": r.dielectric.model_dump(exclude={"provenance"}),
        "hazard": r.hazard.model_dump(exclude={"provenance"}),
        "literature": r.literature.model_dump(exclude={"provenance"}),
        "score": {
            "adjusted": sc.adjusted_score,
            "raw": sc.raw_score,
            "data_coverage": sc.data_coverage,
            "confidence": sc.confidence,
        },
        "missing_criteria": sc.missing_criteria,
        "rule_caveats": [c.code for c in sc.caveats],
    }


def llm_caveats(sc: ScoredCandidate, llm: LLMClient) -> list[Caveat]:
    facts = candidate_facts(sc)
    flat = flatten(facts)
    user = (
        "Structured facts for one candidate follow as retrieved data. Argue against it.\n"
        + wrap_retrieved(facts, "triage_facts")
    )
    data = llm.complete_json(REFUTE_SYSTEM, user, REFUTE_SCHEMA)
    if not data:
        return []
    out: list[Caveat] = []
    for obs in (data.get("observations") or [])[:3]:
        if not isinstance(obs, dict):
            continue
        text = str(obs.get("text", "")).strip()[:300]
        fields = [str(f) for f in obs.get("evidence_fields") or []]
        valid_fields = [
            f
            for f in fields
            if f in flat or any(k.startswith(f + ".") or k.startswith(f + "[") for k in flat)
        ]
        if not text or not valid_fields:
            continue
        if not numeric_guard(text, flat):
            continue  # the model introduced a number that is not in the facts
        out.append(
            Caveat(
                code="model_observation",
                severity="info",
                text=text,
                origin="llm",
                evidence={"fields": valid_fields, "model": llm.name},
            )
        )
    return out


def refute(
    shortlist: list[ScoredCandidate], eff: Effective, config: Config, llm: LLMClient | None = None
) -> str:
    """Attach caveats to every shortlisted candidate. Returns a label of what produced them."""
    for sc in shortlist:
        sc.caveats = rule_caveats(sc, eff, config)
    if llm is not None and llm.name != "none" and config.llm.use_for.refute:
        for sc in shortlist:
            sc.caveats.extend(llm_caveats(sc, llm))
        return f"rules+{llm.name}"
    return "rules"
