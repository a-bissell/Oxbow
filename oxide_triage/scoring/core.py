"""The deterministic core: gates, component scores, aggregation, ranking.

No model, no network, no clock. Given the same records and the same settings this module
returns byte-identical output. Missing data never defaults to a neutral value: a component
with no data contributes ``None``, lowers ``data_coverage`` and is listed by name — together
with *why* it is missing, since a value the source does not hold (ABSENT) and a value this
cache never fetched (NOT_RETRIEVED) are different problems with different remedies.

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
from typing import NamedTuple

from oxide_triage.config import Config
from oxide_triage.schemas import (
    BandGapAssessment,
    CandidateRecord,
    ComponentScore,
    DataStatus,
    FigureOfMeritInfo,
    GateResult,
    RetrievalCompleteness,
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
                reason=None
                if ok
                else f"effective gap {gap.effective_ev:.2f} eV below {eff.min_band_gap:g} eV",
            )
        )
        if not ok:
            reasons.append(gates[-1].reason or "band gap gate failed")

    # Figure of merit: a gate only when the profile says a candidate without it is not a candidate
    fom = record.figure_of_merit
    if eff.on_missing_fom == "exclude" and (fom.status != DataStatus.KNOWN or fom.value is None):
        gates.append(
            GateResult(
                gate=eff.fom_criterion,
                passed=False,
                threshold_label=f"{eff.fom_label} known",
                observed_label=f"{eff.fom_label} {fom.status.value}",
                reason=f"{eff.fom_label} not available (on_missing=exclude)",
            )
        )
        reasons.append(gates[-1].reason or f"{eff.fom_label} unknown")

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
    blocked_hazard = sorted(
        el for el in record.elements if el in eff.blocked_elements and el not in eff.exclude_elements
    )
    ok = not blocked_hazard
    gates.append(
        GateResult(
            gate="hazard_blocklist",
            passed=ok,
            threshold_label="no blocked hazard-tier elements"
            + (
                f" (allowed despite tier: {', '.join(sorted(eff.allowed_despite_tier))})"
                if eff.allowed_despite_tier
                else ""
            ),
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


def _component(
    criterion: str,
    weight: float,
    raw_label: str,
    normalized: float | None,
    notes: list[str] | None = None,
    status: DataStatus = DataStatus.NOT_RETRIEVED,
) -> ComponentScore:
    """One score component. When ``normalized`` is None the caller passes the record's own
    status, so the component carries *why* it has no value and not merely that it has none."""
    known = normalized is not None
    return ComponentScore(
        criterion=criterion,
        weight=round(weight, ROUND),
        raw_label=raw_label,
        normalized=_r(normalized),
        contribution=_r(weight * normalized) if known else None,
        status=DataStatus.KNOWN if known else status,
        notes=notes or [],
    )


def _missing_note(status: DataStatus, absent_note: str) -> str:
    """Say why a component has no value, in the terms the reader needs to act on."""
    if status is DataStatus.NOT_RETRIEVED:
        return "NOT RETRIEVED into this cache — unknown because nothing was fetched, not because the source is empty"
    if status is DataStatus.NOT_APPLICABLE:
        return "not sought: the criterion does not apply, or the source is disabled by config"
    return absent_note


def cross_source_agreement(record: CandidateRecord, tolerance: float) -> str:
    """agree | disagree | unavailable | untested.

    ``unavailable`` means the second source was asked and holds no entry, so this candidate's
    stability genuinely rests on one source. ``untested`` means the cross-check never ran here,
    so agreement is unknown rather than absent — a different caveat, and a fixable one.
    """
    mp = record.stability.energy_above_hull_ev_atom
    oq = record.cross_check.stability_ev_atom
    if record.cross_check.status == DataStatus.NOT_RETRIEVED:
        return "untested"
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
            notes.append(
                "cross-check never ran on this cache; agreement untested"
                if agreement == "untested"
                else "no independent cross-check available; stability rests on one source"
            )
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
        comps.append(
            _component(
                "stability",
                w["stability"],
                "E_hull unknown",
                None,
                [_missing_note(record.stability.status, "no stability value in the source")],
                status=record.stability.status,
            )
        )

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
        comps.append(
            _component(
                "band_gap",
                w["band_gap"],
                "band gap unknown",
                None,
                [gap.correction_note, _missing_note(record.band_gap.status, "no band gap in the source")],
                status=record.band_gap.status,
            )
        )

    # Figure of merit (the application property; config.figure_of_merit says which) ----
    fom = config.figure_of_merit
    d = record.figure_of_merit
    if d.status == DataStatus.KNOWN and d.value is not None:
        lo, hi = fom.low, fom.high
        if fom.prefer == "high":
            norm = clamp01((d.value - lo) / (hi - lo))
            rule = f"score = clamp(({fom.property} - {lo:g}) / ({hi:g} - {lo:g}))"
        else:
            norm = clamp01((hi - d.value) / (hi - lo))
            rule = f"score = clamp(({hi:g} - {fom.property}) / ({hi:g} - {lo:g}))"
        comps.append(
            _component(
                fom.criterion,
                w[fom.criterion],
                d.display or f"{fom.property} = {d.value:.1f} ({fom.method})",
                norm,
                [rule],
            )
        )
    else:
        comps.append(
            _component(
                fom.criterion,
                w[fom.criterion],
                f"{fom.label} UNKNOWN",
                None,
                [
                    _missing_note(d.status, d.absent_note or f"no {fom.label} value in the source"),
                    "not scored; coverage reduced",
                ],
                status=d.status,
            )
        )

    # Interface: stability in contact with the substrate ---------------------------------
    iface = record.interface
    ic = config.interface
    if iface.status == DataStatus.KNOWN and iface.reaction_energy_ev_atom is not None:
        e_rxn = iface.reaction_energy_ev_atom
        norm = clamp01(1.0 + (e_rxn + ic.tolerance_ev_atom) / ic.zero_score_at_ev_atom)
        if e_rxn >= -1e-9:
            label = f"stable against {iface.substrate} (no hull reaction)"
        else:
            prods = " + ".join(iface.products[:4]) or "hull phases"
            label = f"reacts with {iface.substrate}: {e_rxn:+.3f} eV/atom -> {prods}"
        comps.append(
            _component(
                "interface",
                w["interface"],
                label,
                norm,
                [
                    f"score = clamp(1 + (E_rxn + {ic.tolerance_ev_atom:g}) / {ic.zero_score_at_ev_atom:g}); "
                    f"reactions inside {ic.tolerance_ev_atom:g} eV/atom count as none; most exothermic reaction of the "
                    f"oxide with {iface.substrate} against the {iface.thermo_type} hull "
                    f"({iface.n_phases} stable phases)"
                    + (
                        f", at {iface.x_substrate:.0%} {iface.substrate}"
                        if iface.x_substrate is not None
                        else ""
                    ),
                    "bulk thermodynamics only: kinetics, interlayers and epitaxy are not modelled",
                ],
            )
        )
    else:
        comps.append(
            _component(
                "interface",
                w["interface"],
                f"stability against {ic.substrate} unavailable",
                None,
                [_missing_note(iface.status, "no hull data for this element system")],
                status=iface.status,
            )
        )

    # Toxicity ------------------------------------------------------------------------
    h = record.hazard
    if h.status == DataStatus.KNOWN and h.worst_tier is not None:
        tier_score = config.toxicity.tier_scores.get(h.worst_tier, 0.0)
        notes = [
            f"{el}: tier {t} ({h.element_basis.get(el, '')})" for el, t in sorted(h.element_tiers.items())
        ]
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
        comps.append(
            _component(
                "toxicity",
                w["toxicity"],
                "hazard screen unavailable",
                None,
                [_missing_note(h.status, "no hazard entry for these elements")],
                status=h.status,
            )
        )

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
        comps.append(
            _component(
                "literature",
                w["literature"],
                "literature evidence unavailable",
                None,
                [_missing_note(lit.status, "OpenAlex returned no works")],
                status=lit.status,
            )
        )

    return comps, agreement


# --------------------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------------------


class Aggregate(NamedTuple):
    raw: float | None
    coverage: float
    adjusted: float | None
    missing: list[str]
    absent: list[str]
    not_retrieved: list[str]
    retrieval_gap: float
    confidence: str


def aggregate(components: list[ComponentScore], config: Config) -> Aggregate:
    """Combine components into a score, and report what was missing and why.

    The arithmetic deliberately treats ABSENT and NOT_RETRIEVED alike: in both cases we do not
    know the value, and pretending otherwise would let an unfetched candidate score as though
    its data were good. What changes is that ``retrieval_gap`` is reported separately, so a
    reader (and the self-check) can tell a materially data-poor candidate from one this cache
    simply failed to fetch — the second is fixable by warming the cache, the first is not.
    """
    total_w = sum(c.weight for c in components)
    known = [c for c in components if c.status == DataStatus.KNOWN and c.normalized is not None]
    known_w = sum(c.weight for c in known)
    missing = [c.criterion for c in components if c.status != DataStatus.KNOWN]
    absent = [c.criterion for c in components if c.status == DataStatus.ABSENT]
    not_retrieved = [c.criterion for c in components if c.status == DataStatus.NOT_RETRIEVED]
    nr_w = sum(c.weight for c in components if c.status == DataStatus.NOT_RETRIEVED)
    coverage = known_w / total_w if total_w > 0 else 0.0
    retrieval_gap = round(nr_w / total_w, ROUND) if total_w > 0 else 0.0
    if known_w <= 0:
        return Aggregate(
            None, round(coverage, ROUND), None, missing, absent, not_retrieved, retrieval_gap, "low"
        )
    raw = sum(c.weight * (c.normalized or 0.0) for c in known) / known_w
    credit = raw if config.missing_data.policy == "renormalize" else raw * coverage
    adjusted = max(0.0, credit - config.missing_data.penalty * (1.0 - coverage))
    th = config.missing_data.confidence_thresholds
    confidence = "high" if coverage >= th["high"] else "medium" if coverage >= th["medium"] else "low"
    if missing and confidence == "high":
        confidence = "medium"  # any criterion without data caps confidence, whatever its weight
    if not_retrieved and confidence != "low":
        confidence = "low"  # a score built on unfetched data is not a confident score
    return Aggregate(
        round(raw, ROUND),
        round(coverage, ROUND),
        round(adjusted, ROUND),
        missing,
        absent,
        not_retrieved,
        retrieval_gap,
        confidence,
    )


def score_candidate(record: CandidateRecord, config: Config, eff: Effective) -> ScoredCandidate:
    gap = assess_band_gap(record.band_gap, config.band_gap)
    gates, reasons = evaluate_gates(record, gap, eff)
    components, agreement = score_components(record, gap, eff, config)
    agg = aggregate(components, config)
    return ScoredCandidate(
        record=record,
        band_gap_assessment=gap,
        gates=gates,
        excluded=bool(reasons),
        exclusion_reasons=reasons,
        components=components,
        raw_score=agg.raw,
        data_coverage=agg.coverage,
        missing_criteria=agg.missing,
        absent_criteria=agg.absent,
        not_retrieved_criteria=agg.not_retrieved,
        retrieval_gap=agg.retrieval_gap,
        comparable=not agg.not_retrieved,
        adjusted_score=agg.adjusted,
        confidence=agg.confidence,  # type: ignore[arg-type]
        cross_source_agreement=agreement,  # type: ignore[arg-type]
    )


def rank(
    records: list[CandidateRecord], config: Config, eff: Effective
) -> tuple[list[ScoredCandidate], list[ScoredCandidate]]:
    scored = [score_candidate(r, config, eff) for r in records]
    passing = [s for s in scored if not s.excluded and s.adjusted_score is not None]
    excluded = [s for s in scored if s.excluded or s.adjusted_score is None]
    passing.sort(key=lambda s: (-(s.adjusted_score or 0.0), s.record.material_id))
    for i, s in enumerate(passing, 1):
        s.rank = i
    excluded.sort(key=lambda s: s.record.material_id)
    return passing, excluded


def retrieval_completeness(
    ranked: list[ScoredCandidate], config: Config, scope_n: int | None = None
) -> RetrievalCompleteness:
    """Measure how much of the data the ranking wanted was actually fetched into this cache.

    Reported per result rather than per candidate because the damage is comparative: one
    unfetched candidate is a caveat on that row, but a cache that is broadly unretrieved makes
    the *ordering* a partial artefact of which fetches happened to finish.

    ``scope_n`` restricts the measure to the top ``scope_n`` ranked candidates: under on-demand
    formula sources that is the settled pool the shortlist is drawn from, and everything below
    it is unretrieved by design and labelled as such on each row.
    """
    floor = config.retrieval.min_completeness_warn
    scoped = scope_n is not None and scope_n < len(ranked)
    if scoped:
        ranked = ranked[:scope_n]
    if not ranked:
        return RetrievalCompleteness(
            completeness=1.0,
            n_ranked=0,
            n_fully_retrieved=0,
            comparable=True,
            note="No candidates ranked; nothing to retrieve.",
        )
    # The cross-check is not a weighted criterion of its own: it moves the score as a bonus or
    # penalty inside the stability component. It is nonetheless retrieved data that can go
    # missing, and on a partly-warmed cache it is usually the *largest* hole, so it is accounted
    # here at the weight of the component it modifies. Leaving it out was how a cache with 84%
    # of its cross-checks unfetched still measured 90% complete.
    stability_w = next((c.weight for c in ranked[0].components if c.criterion == "stability"), 0.0)
    per_candidate_w = sum(c.weight for c in ranked[0].components) + stability_w
    total_w = per_candidate_w * len(ranked)
    nr_w = sum(c.weight for s in ranked for c in s.components if c.status == DataStatus.NOT_RETRIEVED)
    nr_by: dict[str, int] = {}
    ab_by: dict[str, int] = {}
    for s in ranked:
        for name in s.not_retrieved_criteria:
            nr_by[name] = nr_by.get(name, 0) + 1
        for name in s.absent_criteria:
            ab_by[name] = ab_by.get(name, 0) + 1
        if s.record.cross_check.status == DataStatus.NOT_RETRIEVED:
            nr_w += stability_w
            nr_by["cross_check"] = nr_by.get("cross_check", 0) + 1
        elif s.record.cross_check.status == DataStatus.ABSENT:
            ab_by["cross_check"] = ab_by.get("cross_check", 0) + 1
    completeness = round(1.0 - (nr_w / total_w if total_w else 0.0), ROUND)
    n_full = sum(
        1
        for s in ranked
        if not s.not_retrieved_criteria and s.record.cross_check.status != DataStatus.NOT_RETRIEVED
    )
    comparable = completeness >= floor
    where = (
        f"the top {len(ranked)} ranked candidates (the on-demand pool)"
        if scoped
        else f"{len(ranked)} ranked candidates"
    )
    if comparable:
        note = (
            f"{completeness:.1%} of the scoring weight across {where} was "
            f"retrieved into this cache; ranks are comparable."
        )
    else:
        worst = ", ".join(f"{k} ({v} candidates)" for k, v in sorted(nr_by.items(), key=lambda kv: -kv[1]))
        note = (
            f"INCOMPLETE RETRIEVAL: only {completeness:.1%} of the scoring weight across "
            f"{where} was retrieved (floor {floor:.0%}); "
            f"{n_full} candidates have complete data. Never retrieved: {worst}. "
            "Because unretrieved criteria lower a score, this ordering partly reflects which "
            "fetches finished rather than which materials are better. Warm the cache to completion "
            "before comparing ranks across candidates."
        )
    return RetrievalCompleteness(
        completeness=completeness,
        n_ranked=len(ranked),
        n_fully_retrieved=n_full,
        not_retrieved_by_criterion=nr_by,
        absent_by_criterion=ab_by,
        comparable=comparable,
        note=note,
    )


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
            f"on_missing_{config.figure_of_merit.criterion}": eff.on_missing_fom,
            "substrate": eff.substrate,
        },
        figure_of_merit=FigureOfMeritInfo(
            criterion=config.figure_of_merit.criterion,
            label=config.figure_of_merit.label,
            units=config.figure_of_merit.units,
            method=config.figure_of_merit.method,
            property=config.figure_of_merit.property,
            provider=config.figure_of_merit.provider,
            prefer=config.figure_of_merit.prefer,
        ),
    )
