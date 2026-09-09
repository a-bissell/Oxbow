"""The deterministic core: gates, component scores, aggregation, ranking.

No model, no network, no clock. Given the same records and the same settings this module
returns byte-identical output. Missing data never defaults to a neutral value: a component
with no data contributes ``None``, lowers ``data_coverage`` and is listed by name.

    raw_score      = sum(w_i * s_i, known i) / sum(w_i, known i)      # "how good on the data we have"
    data_coverage  = sum(w_i, known i) / sum(w_i, all i)
    policy no_credit (default):
        adjusted   = max(0, raw_score * data_coverage - penalty * (1 - data_coverage))
        i.e. an unknown criterion earns nothing; less data can never mean a higher rank.
    policy renormalize:
        adjusted   = max(0, raw_score - penalty * (1 - data_coverage))

Ties are broken by material_id so ordering is total and reproducible.
"""

from __future__ import annotations

import math

from oxide_triage.config import Config
from oxide_triage.schemas import (
    BandGapAssessment,
    CandidateRecord,
    ComponentScore,
    DataStatus,
    GateResult,
    ScoredCandidate,
    ScoringExplanation,
)
from oxide_triage.scoring.bandgap import assess_band_gap
from oxide_triage.scoring.settings import Effective

ROUND = 6


def clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, ROUND)


# --------------------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------------------


def evaluate_gates(
    record: CandidateRecord, gap: BandGapAssessment, eff: Effective
) -> tuple[list[GateResult], list[str]]:
    gates: list[GateResult] = []
    reasons: list[str] = []

    # Stability
    e_hull = record.stability.energy_above_hull_ev_atom
    if record.stability.status != DataStatus.KNOWN or e_hull is None:
        passed = None if eff.on_missing_stability == "flag" else False
        gates.append(
            GateResult(
                gate="stability",
                passed=passed,
                threshold_label=f"E_hull <= {eff.max_energy_above_hull:g} eV/atom",
                observed_label="E_hull unknown",
                reason="energy above hull not available; gate indeterminate",
            )
        )
        if passed is False:
            reasons.append("stability unknown (on_missing_stability=exclude)")
    else:
        ok = e_hull <= eff.max_energy_above_hull
        gates.append(
            GateResult(
                gate="stability",
                passed=ok,
                threshold_label=f"E_hull <= {eff.max_energy_above_hull:g} eV/atom",
                observed_label=f"E_hull = {e_hull:.3f} eV/atom",
                reason=None if ok else f"E_hull {e_hull:.3f} exceeds {eff.max_energy_above_hull:g} eV/atom",
            )
        )
        if not ok:
            reasons.append(gates[-1].reason or "stability gate failed")

    # Band gap (effective, functional-aware)
    if gap.effective_ev is None:
        passed = None if eff.on_missing_band_gap == "flag" else False
        gates.append(
            GateResult(
                gate="band_gap",
                passed=passed,
                threshold_label=f"effective gap >= {eff.min_band_gap:g} eV",
                observed_label="band gap unknown",
                reason="band gap not available; gate indeterminate",
            )
        )
        if passed is False:
            reasons.append("band gap unknown (on_missing_band_gap=exclude)")
    else:
        ok = gap.effective_ev >= eff.min_band_gap
        corrected = " (corrected)" if gap.corrected else ""
        observed = (
            f"effective gap = {gap.effective_ev:.2f} eV{corrected} from "
            f"{gap.reported_functional or 'unknown'} {gap.reported_ev:.2f} eV"
        )
        gates.append(
            GateResult(
                gate="band_gap",
                passed=ok,
                threshold_label=f"effective gap >= {eff.min_band_gap:g} eV",
                observed_label=observed,
                reason=None if ok else f"effective gap {gap.effective_ev:.2f} eV below {eff.min_band_gap:g} eV",
            )
        )
        if not ok:
            reasons.append(gates[-1].reason or "band gap gate failed")

    # Composition size
    ok = record.n_elements <= eff.max_elements
    gates.append(
        GateResult(
            gate="max_elements",
            passed=ok,
            threshold_label=f"distinct elements <= {eff.max_elements}",
            observed_label=f"{record.n_elements} elements ({'-'.join(record.elements)})",
            reason=None if ok else f"{record.n_elements} elements exceeds {eff.max_elements}",
        )
    )
    if not ok:
        reasons.append(gates[-1].reason or "too many elements")

    # Hazard blocklist (tiered) and request exclusions
    blocked_hazard = sorted(el for el in record.elements if el in eff.blocked_elements and el not in eff.exclude_elements)
    ok = not blocked_hazard
    gates.append(
        GateResult(
            gate="hazard_blocklist",
            passed=ok,
            threshold_label="no blocked hazard-tier elements"
            + (f" (allowed despite tier: {', '.join(sorted(eff.allowed_despite_tier))})" if eff.allowed_despite_tier else ""),
            observed_label="blocked: " + (", ".join(blocked_hazard) if blocked_hazard else "none"),
            reason=None if ok else f"contains blocked element(s): {', '.join(blocked_hazard)}",
        )
    )
    if not ok:
        reasons.append(gates[-1].reason or "hazard blocklist")

    if eff.exclude_elements:
        hit = sorted(el for el in record.elements if el in eff.exclude_elements)
        ok = not hit
        gates.append(
            GateResult(
                gate="excluded_elements",
                passed=ok,
                threshold_label="none of: " + ", ".join(sorted(eff.exclude_elements)),
                observed_label="present: " + (", ".join(hit) if hit else "none"),
                reason=None if ok else f"contains excluded element(s): {', '.join(hit)}",
            )
        )
        if not ok:
            reasons.append(gates[-1].reason or "excluded element")

    if eff.include_elements:
        missing = sorted(el for el in eff.include_elements if el not in record.elements)
        ok = not missing
        gates.append(
            GateResult(
                gate="required_elements",
                passed=ok,
                threshold_label="must contain: " + ", ".join(sorted(eff.include_elements)),
                observed_label="missing: " + (", ".join(missing) if missing else "none"),
                reason=None if ok else f"lacks required element(s): {', '.join(missing)}",
            )
        )
        if not ok:
            reasons.append(gates[-1].reason or "required element missing")

    return gates, reasons


# --------------------------------------------------------------------------------------
# Component scores
# --------------------------------------------------------------------------------------


def _component(criterion: str, weight: float, raw_label: str, normalized: float | None, notes: list[str] | None = None) -> ComponentScore:
    known = normalized is not None
    return ComponentScore(
        criterion=criterion,
        weight=round(weight, ROUND),
        raw_label=raw_label,
        normalized=_r(normalized),
        contribution=_r(weight * normalized) if known else None,
        status=DataStatus.KNOWN if known else DataStatus.UNKNOWN,
        notes=notes or [],
    )


def cross_source_agreement(record: CandidateRecord, tolerance: float) -> str:
    mp = record.stability.energy_above_hull_ev_atom
    oq = record.cross_check.stability_ev_atom
    if record.stability.status != DataStatus.KNOWN or record.cross_check.status != DataStatus.KNOWN:
        return "unavailable"
    if mp is None or oq is None:
        return "unavailable"
    return "agree" if abs(mp - oq) <= tolerance else "disagree"


def score_components(
    record: CandidateRecord, gap: BandGapAssessment, eff: Effective, config: Config
) -> tuple[list[ComponentScore], str]:
    w = eff.weights
    comps: list[ComponentScore] = []

    # Stability -----------------------------------------------------------------------
    agreement = cross_source_agreement(record, config.stability.cross_check_tolerance_ev_atom)
    e_hull = record.stability.energy_above_hull_ev_atom
    if record.stability.status == DataStatus.KNOWN and e_hull is not None:
        base = 1.0 - clamp01(e_hull / config.stability.zero_score_at_ev_atom)
        notes = [f"base = 1 - E_hull/{config.stability.zero_score_at_ev_atom:g} = {base:.3f}"]
        if agreement == "agree":
            base = clamp01(base + config.stability.agreement_bonus)
            notes.append(
                f"OQMD agrees (hull distance {record.cross_check.stability_ev_atom:.3f} eV/atom): "
                f"+{config.stability.agreement_bonus:g} bonus"
            )
        elif agreement == "disagree":
            base = clamp01(base - config.stability.disagreement_penalty)
            notes.append(
                f"OQMD disagrees (hull distance {record.cross_check.stability_ev_atom:.3f} eV/atom): "
                f"-{config.stability.disagreement_penalty:g} penalty"
            )
        else:
            notes.append("no independent cross-check available; stability rests on one source")
        comps.append(
            _component(
                "stability",
                w["stability"],
                f"E_hull = {e_hull:.3f} eV/atom [{record.stability.functional}]",
                base,
                notes,
            )
        )
    else:
        comps.append(_component("stability", w["stability"], "E_hull unknown", None, ["no stability data"]))

    # Band gap ------------------------------------------------------------------------
    if gap.effective_ev is not None:
        ideal = config.band_gap.preference.ideal_ev
        lo = eff.min_band_gap
        norm = 1.0 if ideal <= lo else clamp01((gap.effective_ev - lo) / (ideal - lo))
        comps.append(
            _component(
                "band_gap",
                w["band_gap"],
                f"effective gap = {gap.effective_ev:.2f} eV" + (" (corrected)" if gap.corrected else ""),
                norm,
                [gap.correction_note, f"score = clamp((gap - {lo:g}) / ({ideal:g} - {lo:g}))"],
            )
        )
    else:
        comps.append(_component("band_gap", w["band_gap"], "band gap unknown", None, [gap.correction_note]))

    # Dielectric ----------------------------------------------------------------------
    d = record.dielectric
    if d.status == DataStatus.KNOWN and d.e_total is not None:
        lo, hi = config.dielectric.low, config.dielectric.high
        norm = clamp01((d.e_total - lo) / (hi - lo))
        comps.append(
            _component(
                "dielectric",
                w["dielectric"],
                f"e_total = {d.e_total:.1f} (DFPT; electronic {d.e_electronic if d.e_electronic is not None else '?'})",
                norm,
                [f"score = clamp((e_total - {lo:g}) / ({hi:g} - {lo:g}))"],
            )
        )
    else:
        comps.append(
            _component(
                "dielectric",
                w["dielectric"],
                "dielectric constant UNKNOWN",
                None,
                ["no DFPT dielectric record; not scored, coverage reduced"],
            )
        )

    # Toxicity ------------------------------------------------------------------------
    h = record.hazard
    if h.status == DataStatus.KNOWN and h.worst_tier is not None:
        tier_score = config.toxicity.tier_scores.get(h.worst_tier, 0.0)
        notes = [f"{el}: tier {t} ({h.element_basis.get(el, '')})" for el, t in sorted(h.element_tiers.items())]
        if h.ghs_hazard_codes:
            notes.append(f"PubChem GHS for compound (CID {h.pubchem_cid}): {', '.join(h.ghs_hazard_codes)}")
        comps.append(
            _component(
                "toxicity",
                w["toxicity"],
                f"worst element tier = {h.worst_tier} ({', '.join(h.worst_elements)}) [table {h.table_version}]",
                tier_score,
                notes,
            )
        )
    else:
        comps.append(_component("toxicity", w["toxicity"], "hazard screen unavailable", None))

    # Simplicity ----------------------------------------------------------------------
    simp = config.simplicity.scores.get(record.n_elements, 0.0)
    comps.append(
        _component(
            "simplicity",
            w["simplicity"],
            f"{record.n_elements} distinct elements",
            simp,
            [f"lookup table {dict(sorted(config.simplicity.scores.items()))}"],
        )
    )

    # Literature ----------------------------------------------------------------------
    lit = record.literature
    if lit.status == DataStatus.KNOWN and lit.total_works is not None and lit.thin_film_works is not None:
        lc = config.literature
        tf = min(1.0, math.log1p(lit.thin_film_works) / math.log1p(lc.thin_film_saturation))
        tot = min(1.0, math.log1p(lit.total_works) / math.log1p(lc.total_saturation))
        norm = lc.thin_film_weight * tf + (1 - lc.thin_film_weight) * tot
        comps.append(
            _component(
                "literature",
                w["literature"],
                f"{lit.thin_film_works} thin-film works / {lit.total_works} total (OpenAlex)",
                norm,
                [
                    f"thin-film sub-score {tf:.3f} (log-saturating at {lc.thin_film_saturation}), "
                    f"total sub-score {tot:.3f} (saturating at {lc.total_saturation}), "
                    f"weights {lc.thin_film_weight:g}/{1 - lc.thin_film_weight:g}",
                    "search terms: " + ", ".join(lit.query_terms),
                ],
            )
        )
    else:
        comps.append(_component("literature", w["literature"], "literature evidence unavailable", None))

    return comps, agreement


# --------------------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------------------


def aggregate(components: list[ComponentScore], config: Config) -> tuple[float | None, float, float | None, list[str], str]:
    total_w = sum(c.weight for c in components)
    known = [c for c in components if c.status == DataStatus.KNOWN and c.normalized is not None]
    known_w = sum(c.weight for c in known)
    missing = [c.criterion for c in components if c.status != DataStatus.KNOWN]
    coverage = known_w / total_w if total_w > 0 else 0.0
    if known_w <= 0:
        return None, round(coverage, ROUND), None, missing, "low"
    raw = sum(c.weight * (c.normalized or 0.0) for c in known) / known_w
    credit = raw if config.missing_data.policy == "renormalize" else raw * coverage
    adjusted = max(0.0, credit - config.missing_data.penalty * (1.0 - coverage))
    th = config.missing_data.confidence_thresholds
    confidence = "high" if coverage >= th["high"] else "medium" if coverage >= th["medium"] else "low"
    return round(raw, ROUND), round(coverage, ROUND), round(adjusted, ROUND), missing, confidence


def score_candidate(record: CandidateRecord, config: Config, eff: Effective) -> ScoredCandidate:
    gap = assess_band_gap(record.band_gap, config.band_gap)
    gates, reasons = evaluate_gates(record, gap, eff)
    components, agreement = score_components(record, gap, eff, config)
    raw, coverage, adjusted, missing, confidence = aggregate(components, config)
    return ScoredCandidate(
        record=record,
        band_gap_assessment=gap,
        gates=gates,
        excluded=bool(reasons),
        exclusion_reasons=reasons,
        components=components,
        raw_score=raw,
        data_coverage=coverage,
        missing_criteria=missing,
        adjusted_score=adjusted,
        confidence=confidence,  # type: ignore[arg-type]
        cross_source_agreement=agreement,  # type: ignore[arg-type]
    )


def rank(records: list[CandidateRecord], config: Config, eff: Effective) -> tuple[list[ScoredCandidate], list[ScoredCandidate]]:
    scored = [score_candidate(r, config, eff) for r in records]
    passing = [s for s in scored if not s.excluded and s.adjusted_score is not None]
    excluded = [s for s in scored if s.excluded or s.adjusted_score is None]
    passing.sort(key=lambda s: (-(s.adjusted_score or 0.0), s.record.material_id))
    for i, s in enumerate(passing, 1):
        s.rank = i
    excluded.sort(key=lambda s: s.record.material_id)
    return passing, excluded


def explanation(config: Config, eff: Effective) -> ScoringExplanation:
    return ScoringExplanation(
        formula=(
            "raw = sum(w_i * s_i over criteria with data) / sum(w_i over criteria with data); "
            "coverage = sum(w_i with data) / sum(w_i); "
            + (
                f"adjusted = max(0, raw - {config.missing_data.penalty:g} * (1 - coverage)) [policy: renormalize]; "
                if config.missing_data.policy == "renormalize"
                else f"adjusted = max(0, raw * coverage - {config.missing_data.penalty:g} * (1 - coverage)) "
                "[policy: no_credit — an unknown criterion earns no credit]; "
            )
            + "ties broken by material id"
        ),
        missing_data_penalty=config.missing_data.penalty,
        missing_data_policy=config.missing_data.policy,
        confidence_thresholds=dict(config.missing_data.confidence_thresholds),
        weights={k: round(v, ROUND) for k, v in eff.weights.items()},
        gates={
            "max_energy_above_hull_ev_atom": eff.max_energy_above_hull,
            "min_effective_band_gap_ev": eff.min_band_gap,
            "max_elements": eff.max_elements,
            "blocked_elements": sorted(eff.blocked_elements),
            "allowed_despite_tier": sorted(eff.allowed_despite_tier),
            "required_elements": sorted(eff.include_elements),
            "excluded_elements": sorted(eff.exclude_elements),
            "band_gap_correction": config.band_gap.correction.model_dump(),
            "on_missing_stability": eff.on_missing_stability,
            "on_missing_band_gap": eff.on_missing_band_gap,
        },
    )
