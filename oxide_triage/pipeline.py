"""Orchestration. Read top to bottom, this is the whole system:

    guard  ->  parse (front edge)  ->  clarify?  ->  self-check gate  ->  data layer (cache)
           ->  deterministic core  ->  refutation (annotates)  ->  render (back edge, templates)

The language model, if configured, is invoked in exactly two places (parse, refute) and its
output is validated before use. The core never sees it. When the system is driven through MCP,
the client's model plays the front-edge role and this module is still the enforcement point.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oxide_triage.acquire import GAP_KINDS, AcquisitionReport, fill_gaps, make_planner, read_report
from oxide_triage.cache import Cache
from oxide_triage.config import (
    Config,
    cations_for_families,
    load_cation_allowlist,
    load_cation_families,
    load_hazard_table,
)
from oxide_triage.edges.llm import LLMClient, make_llm
from oxide_triage.edges.parse import parse_request
from oxide_triage.edges.render import rationale_line
from oxide_triage.grouping import assign_tiers, group_polymorphs, polymorph_caveat
from oxide_triage.guard import guard_request
from oxide_triage.progress import ProgressFn, emit
from oxide_triage.refute import refute, rule_caveats
from oxide_triage.schemas import (
    CandidateRecord,
    Criteria,
    GuardDecision,
    RequestBin,
    ScopeInfo,
    ScoredCandidate,
    TriageResult,
)
from oxide_triage.scoring.core import explanation, rank, retrieval_completeness
from oxide_triage.scoring.settings import blocked_by_policy, never_liftable, resolve
from oxide_triage.selfcheck import read_selfcheck, run_selfcheck
from oxide_triage.session import apply_changes, clarifications
from oxide_triage.sources.assemble import DataLayer
from oxide_triage.sources.base import SourceError
from oxide_triage.sources.fixtures import load_fixture

log = logging.getLogger(__name__)


MAX_SETTLE_ROUNDS = 6  # on-demand fill rounds per query before giving up on a moving pool


def _pool_rows(ranked: list[ScoredCandidate], pool_size: int, by_compound: bool) -> list[ScoredCandidate]:
    """The top of the ranking that the on-demand fill covers. With polymorph grouping the pool
    is the first ``pool_size`` compounds and every passing phase of each, since any phase may
    lead its row once the fill has changed the scores."""
    if not by_compound:
        return ranked[:pool_size]
    formulas: list[str] = []
    for s in ranked:
        if s.record.formula not in formulas:
            if len(formulas) == pool_size:
                break
            formulas.append(s.record.formula)
    keep = set(formulas)
    return [s for s in ranked if s.record.formula in keep]


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def not_acted_on_lines(guard: GuardDecision, criteria: Criteria) -> list[str]:
    """Everything the request asked for that this run does not do, one line each: a mode that
    does not exist, a capability the deployment lacks, a clause no rule read. Printed on every
    output so a partly honoured request never reads as fully honoured."""
    lines: list[str] = []
    for f in guard.findings:
        if f.bin == RequestBin.OVERRIDE:
            lines.append(f'"{f.matched_text}": {f.explanation.split(". ")[0]}.')
        elif f.bin == RequestBin.IMPOSSIBLE:
            lines.append(f'"{f.matched_text}": {f.explanation.split(". ")[0]}.')
    for clause in criteria.unhandled:
        lines.append(clause if ":" in clause else f"{clause}: no rule read this, so it changed nothing.")
    return lines


def _log_deviations(config: Config, result: TriageResult) -> None:
    if not result.deviations:
        return
    path = Path(config.cache.path).with_name("deviations.jsonl")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": result.generated_at,
                        "profile": result.profile_name,
                        "request": result.request_text,
                        "deviations": [d.model_dump() for d in result.deviations],
                    }
                )
                + "\n"
            )
    except OSError as exc:  # logging must never break a run
        log.warning("could not write deviation log: %s", exc)
    for d in result.deviations:
        log.warning("configuration deviation [%s/%s]: %s", d.origin, d.code, d.description)


def _selfcheck_gate(config: Config, cache: Cache) -> tuple[str, str | None]:
    """Return (status, blocking_message).

    status: passed | failed | inconclusive | not_run | skipped. ``inconclusive`` means the
    known-answer test could not run because the cache is too sparsely retrieved to validate a
    ranking. That is a statement about the cache, not a verdict on the ranker, so it warns
    loudly rather than blocking — the retrieval floors in ``retrieval:`` decide whether a
    ranking is served at all.
    """
    if not config.selfcheck.enabled:
        return "skipped", None
    sc = read_selfcheck(cache)
    if sc is None:
        return "not_run", None
    if sc.passed:
        return "passed", None
    if sc.inconclusive:
        return "inconclusive", None
    msg = (
        "SELF-CHECK FAILED on this cache: the known-answer test did not pass ("
        + "; ".join(sc.details)
        + f"). Checked {sc.checked_at}. "
    )
    if config.selfcheck.on_failure == "block":
        return (
            "failed",
            msg + "No shortlist is served from a cache that fails ground-truth validation. "
            "Inspect with `oxide-triage selfcheck`, fix the cache or the profile, or set selfcheck.on_failure to warn.",
        )
    return "failed", None


def scope_records(
    records: list[CandidateRecord], config: Config, families: list[str]
) -> tuple[list[CandidateRecord], ScopeInfo]:
    """Keep the records whose cations all belong to a selected family. Not a gate: a material
    outside the scope was never a candidate for this run, so it is not listed as excluded."""
    fams = load_cation_families(config.candidates.cation_allowlist_file)
    known = {f.id for f in fams}
    selected = [f for f in families if f in known]
    info = ScopeInfo(families=selected, n_universe=len(records), n_in_scope=len(records))
    if not selected or set(selected) == known:
        return records, info
    allowed = cations_for_families(fams, selected)
    kept = [r for r in records if all(el == "O" or el in allowed for el in r.elements)]
    info.n_in_scope = len(kept)
    return kept, info


def run_triage(
    request_text: str,
    config: Config,
    cache: Cache | None = None,
    offline: bool | None = None,
    llm: LLMClient | None = None,
    template: str | None = None,
    criteria: Criteria | None = None,
    confirmed: bool = True,
    skip_selfcheck: bool = False,
    http: Any | None = None,
    progress: ProgressFn | None = None,
    overrides: dict[str, Any] | None = None,
) -> TriageResult:
    """Run one triage request.

    ``criteria`` bypasses the parser (used by reruns with changed settings; the guard still runs
    on the original text). ``confirmed=False`` makes the run stop and return its clarification
    questions instead of a shortlist whenever there are any. ``http`` replaces every client's
    transport (a replay of recorded responses in tests). ``progress`` observes the stages of a
    run (see ``oxide_triage.progress``) and cannot affect the result. ``overrides`` are
    criteria fields set by a front end's controls (shortlist length, gates, families) and are
    applied after parsing through the same validated path as a rerun, so they surface as
    deviations like anything else the request changes.
    """
    table = load_hazard_table(config.toxicity.table_file)
    llm = llm or make_llm(config.llm)
    blocked = blocked_by_policy(config, table)
    guard: GuardDecision = guard_request(request_text, table, blocked, never_liftable(config))
    emit(progress, "parse", "Reading the request")
    if criteria is None:
        criteria, parser_label = parse_request(request_text, config, table, llm, blocked)
    else:
        parser_label = "supplied (rerun)"
    asked = criteria  # what the request itself says: only that needs confirming
    if overrides:
        # Values set on a front end's controls were chosen deliberately, so they are applied
        # without a clarification question; they still surface as deviations on the result.
        criteria, _ = apply_changes(criteria, overrides, note_prefix="scope")
    if not criteria.families and config.candidates.default_families:
        criteria.families = list(config.candidates.default_families)
    if template:
        criteria.output_template = template  # type: ignore[assignment]
    eff, deviations = resolve(config, criteria, table)
    if overrides:
        _, asked_devs = resolve(config, asked, table)
        questions = clarifications(asked, asked_devs, guard, config)
    else:
        questions = clarifications(criteria, deviations, guard, config)
    llm_usage = {"parse": parser_label, "refute": "not run", "render": "templates only"}
    # A declined request is explained by its refusal; the list is for runs that went ahead.
    not_acted_on = not_acted_on_lines(guard, criteria) if guard.proceed else []

    own_cache = cache is None
    cache = cache or Cache(config.cache.path)
    try:
        base: dict[str, Any] = dict(
            request_text=request_text,
            criteria=criteria,
            guard=guard,
            profile_name=config.profile_name,
            config_hash=config.config_hash(),
            generated_at=_now(),
            fixture_data=cache.has_fixture_data,
            offline=bool(config.cache.offline if offline is None else offline),
            deviations=deviations,
            scoring=explanation(config, eff),
            llm_usage=llm_usage,
            not_acted_on=not_acted_on,
            clarifications=questions,
            selfcheck_status="not_evaluated",
        )
        if not guard.proceed:
            return TriageResult(
                **base,
                cache_fingerprint=cache.fingerprint([]),
                warnings=[guard.refusal_message or "request declined"],
            )
        if questions and not confirmed:
            return TriageResult(
                **base,
                cache_fingerprint=cache.fingerprint([]),
                needs_confirmation=True,
                warnings=[
                    "Run not started: confirmation needed for the questions listed under clarifications."
                ],
            )
        status, block_msg = ("skipped", None) if skip_selfcheck else _selfcheck_gate(config, cache)
        base["selfcheck_status"] = status
        if block_msg:
            return TriageResult(**base, cache_fingerprint=cache.fingerprint([]), warnings=[block_msg])

        layer = DataLayer.from_config(config, cache=cache, offline=offline, http=http)
        fill_note: str | None = None
        retrieval_scope: int | None = None
        scope_info: ScopeInfo | None = None
        try:
            emit(progress, "rank", "Loading the candidate universe")
            records = layer.build_candidates()
            records, scope_info = scope_records(records, config, criteria.families)
            ranked, excluded = rank(records, config, eff)
            emit(
                progress,
                "rank",
                f"Ranked {len(records)} candidates in scope, {len(ranked)} passed the gates",
                done=len(ranked),
                total=len(records),
            )
            if config.candidates.formula_sources == "on_demand" and not layer.offline and ranked:
                # The formula-keyed sources (OQMD, OpenAlex, PubChem) are fetched per query for
                # the top of the ranking only. Literature and compound hazards can only add
                # credit or caveats, but an OQMD disagreement lowers a score, so after each fill
                # the ranking is recomputed and whatever newly entered the pool is filled too,
                # until the pool is settled. The shortlist is then drawn from a fully retrieved
                # pool; rows below it say they were not retrieved.
                pool_size = max(config.candidates.on_demand_pool, eff.top_k)
                attempted: set[str] = set()
                failed = 0
                rounds = 0
                while rounds < MAX_SETTLE_ROUNDS:
                    pool = [
                        s.record
                        for s in _pool_rows(ranked, pool_size, config.output.group_polymorphs)
                        if s.record.material_id not in attempted and layer.needs_formula_sources(s.record)
                    ]
                    if not pool:
                        break
                    rounds += 1
                    attempted.update(r.material_id for r in pool)
                    emit(
                        progress,
                        "fill",
                        f"Fetching cross-checks, literature and hazards for {len(pool)} candidates"
                        + (f" (round {rounds})" if rounds > 1 else ""),
                        done=0,
                        total=len(pool),
                    )
                    layer.on_progress = lambda d, t, f: emit(
                        progress, "fill", f"Fetched {f}", done=d, total=t
                    )
                    filled, counts = layer.fill_formula_sources(pool)
                    layer.on_progress = None
                    failed += counts.get("failed", 0)
                    by_id = {r.material_id: r for r in filled}
                    records = [by_id.get(r.material_id, r) for r in records]
                    ranked, excluded = rank(records, config, eff)
                retrieval_scope = pool_size
                settled_pool = _pool_rows(ranked, pool_size, config.output.group_polymorphs)
                unresolved = sum(1 for s in settled_pool if layer.needs_formula_sources(s.record))
                scope = (
                    f"all {len(ranked)} ranked candidates"
                    if pool_size >= len(ranked)
                    else f"the top {pool_size} of {len(ranked)} ranked candidates"
                )
                fill_note = (
                    f"OQMD, OpenAlex and PubChem were queried on demand for {scope}"
                    + (f" over {rounds} rounds" if rounds > 1 else "")
                    + f" ({len(attempted)} candidates fetched, {unresolved} still unretrieved)"
                )
                if pool_size < len(ranked):
                    fill_note += (
                        "; candidates ranked below carry no cross-check, literature or compound-hazard data"
                    )
                if failed:
                    fill_note += f"; {failed} lookups failed (see cache log)"
                if rounds >= MAX_SETTLE_ROUNDS and any(
                    s.record.material_id not in attempted and layer.needs_formula_sources(s.record)
                    for s in settled_pool
                ):
                    fill_note += f"; the pool did not settle within {MAX_SETTLE_ROUNDS} rounds"
                fill_note += "."
        finally:
            layer.close()
        collapsed: list[ScoredCandidate] = []
        if config.output.group_polymorphs:
            ranked, collapsed = group_polymorphs(ranked)
        assign_tiers(ranked, config.output.tie_band)
        shortlist, beyond = ranked[: eff.top_k], ranked[eff.top_k :]

        emit(progress, "refute", f"Arguing against each of the {len(shortlist)} shortlisted candidates")
        llm_usage["refute"] = refute(shortlist, eff, config, llm)
        for sc in beyond + excluded + collapsed:
            sc.caveats = rule_caveats(sc, eff, config)
        for sc in ranked:
            if (pc := polymorph_caveat(sc)) is not None:
                # A material spread between phases leads the caveats; a mere note does not
                # displace a more important one as the row's main caveat.
                sc.caveats.insert(0, pc) if pc.severity != "info" else sc.caveats.append(pc)
        for sc in ranked + collapsed:
            sc.rationale = rationale_line(sc)

        if config.candidates.formula_sources == "on_demand" and ranked and retrieval_scope is None:
            # Offline under on-demand sources the fill does not run, but the semantics are the
            # same: the shortlist is drawn from the pool and rows below it say they were not
            # retrieved. Measuring over the whole ranked set here would make the same cache
            # read 100% online and 79% offline.
            retrieval_scope = max(config.candidates.on_demand_pool, eff.top_k)
        retrieval = retrieval_completeness(ranked, config, scope_n=retrieval_scope)
        warnings = list(layer.warnings)
        if not retrieval.comparable:
            warnings.append(retrieval.note)
        if fill_note:
            warnings.append(fill_note)
        if status == "not_run" and not skip_selfcheck:
            warnings.append(
                "Self-check has not been run on this cache; run `oxide-triage selfcheck` before trusting results."
            )
        elif status == "failed":
            warnings.append(
                "Self-check FAILED on this cache (selfcheck.on_failure=warn). Treat this shortlist with suspicion."
            )
        elif status == "inconclusive":
            sc = read_selfcheck(cache)
            warnings.append(
                "Self-check INCONCLUSIVE: the cache is too sparsely retrieved for the known-answer "
                "test to validate ranks, so this shortlist has not been ground-truth checked. "
                + (sc.details[0] if sc and sc.details else "")
            )

        # A cache too sparse to rank honestly is not served at all, if the site says so.
        serve_floor = config.retrieval.min_completeness_serve
        if serve_floor > 0 and retrieval.completeness < serve_floor:
            return TriageResult(
                **base,
                cache_fingerprint=cache.fingerprint(),
                retrieval=retrieval,
                n_candidates_considered=len(records),
                scope=scope_info,
                warnings=[
                    f"No ranking served: retrieval completeness {retrieval.completeness:.1%} is below "
                    f"the configured floor of {serve_floor:.0%}. {retrieval.note} "
                    "Run `oxide-triage warm-cache` to fill the gaps, or lower "
                    "retrieval.min_completeness_serve if a partial ranking is acceptable here."
                ],
            )

        base.update(fixture_data=cache.has_fixture_data, offline=layer.offline)
        last = read_report(cache)
        base["acquisition_summary"] = last.summary() if last else None
        result = TriageResult(
            **base,
            cache_fingerprint=cache.fingerprint(),
            retrieval=retrieval,
            shortlist=shortlist,
            ranked_beyond_shortlist=beyond,
            excluded=excluded,
            collapsed_polymorphs=collapsed,
            tie_band=config.output.tie_band,
            n_candidates_considered=len(records),
            scope=scope_info,
            warnings=warnings,
        )
        _log_deviations(config, result)
        emit(progress, "done", "Done")
        return result
    finally:
        if own_cache:
            cache.close()


def warm_cache(config: Config, cache: Cache | None = None) -> dict[str, object]:
    """Fetch the candidate universe and every per-candidate record into the cache (online),
    then run the self-check."""
    own = cache is None
    cache = cache or Cache(config.cache.path)
    try:
        layer = DataLayer.from_config(config, cache=cache, offline=False)
        try:
            records = layer.build_candidates()
            report = run_acquisition(config, cache, layer=layer, records=records)
            if report is not None and report.n_filled:
                records = layer.build_candidates()
        finally:
            layer.close()
        check = run_selfcheck(config, cache)
        return {
            "candidates": len(records),
            "warnings": list(layer.warnings),
            "sources": cache.sources_summary(),
            "fixture_data": cache.has_fixture_data,
            "acquisition": None if report is None else report.summary(),
            "selfcheck": check.model_dump(),
        }
    finally:
        if own:
            cache.close()


def load_fixtures(config: Config, cache: Cache | None = None) -> int:
    own = cache is None
    cache = cache or Cache(config.cache.path)
    try:
        n = load_fixture(cache, config)
        run_selfcheck(config, cache)
        return n
    finally:
        if own:
            cache.close()


def add_material(formula: str, config: Config, cache: Cache | None = None) -> dict[str, object]:
    """On-demand acquisition: pull one compound's records from the public sources into the
    universe (online only), then re-run the self-check. The fetched values are data like any
    other row; nothing about them is chosen by whoever asked."""
    own = cache is None
    cache = cache or Cache(config.cache.path)
    layer = None
    try:
        layer = DataLayer.from_config(config, cache=cache, offline=False)
        allowed = set(load_cation_allowlist(config.candidates.cation_allowlist_file)) | {"O"}
        try:
            ids = layer.mp.fetch_by_formula(formula, allowed)
        except SourceError as exc:
            return {"formula": formula, "added": [], "error": str(exc)}
        if not ids:
            return {
                "formula": formula,
                "added": [],
                "error": "no non-deprecated oxide entry with allowed elements found",
            }
        c = config.candidates
        n_new = layer.mp.add_to_universe(
            layer.cations,
            c.max_elements_query,
            c.energy_above_hull_ceiling_ev_atom,
            c.min_reported_gap_ev,
            ids,
            c.observed_only,
        )
        # Build the per-candidate records now so the next query is answered from cache, then
        # try alternative routes for whatever the first pass could not find.
        records = [r for r in layer.build_candidates() if r.material_id in set(ids)]
        records, _ = layer.fill_formula_sources(records)  # one compound: fetch everything now
        report = run_acquisition(config, cache, layer=layer, records=records, kinds=GAP_KINDS)
        if report is not None and report.n_filled:
            records = [r for r in layer.build_candidates() if r.material_id in set(ids)]
        check = run_selfcheck(config, cache)
        return {
            "formula": formula,
            "added": ids,
            "new_to_universe": n_new,
            "records": [
                {
                    "material_id": r.material_id,
                    "formula": r.formula,
                    "e_hull": r.stability.energy_above_hull_ev_atom,
                    "band_gap": r.band_gap.value_ev,
                    "functional": r.band_gap.functional,
                    "dielectric": r.dielectric.status.value,
                }
                for r in records
            ],
            "acquisition": None if report is None else report.summary(),
            "selfcheck_passed": check.passed,
        }
    finally:
        if layer is not None:
            layer.close()
        if own:
            cache.close()


def run_acquisition(
    config: Config,
    cache: Cache | None = None,
    layer: DataLayer | None = None,
    records: list | None = None,
    llm: LLMClient | None = None,
    kinds: set[str] | frozenset[str] | None = None,
) -> AcquisitionReport | None:
    """Gap-filling pass over the cache (online only). Returns None when disabled or offline.
    Unless ``candidates.formula_sources: warm``, literature and cross-check gaps are left to the
    query path, which applies the same fallbacks for the ranked pool (fetching them for the whole
    universe is what on-demand mode exists to avoid); ``add-material`` passes every kind because
    a single compound is cheap."""
    if not config.acquisition.enabled:
        return None
    if kinds is None:
        kinds = (
            GAP_KINDS
            if config.candidates.formula_sources == "warm"
            else GAP_KINDS - {"literature", "cross_check"}  # filled per query with their fallbacks
        )
    own = cache is None and layer is None
    cache = cache or (layer.cache if layer else Cache(config.cache.path))
    own_layer = layer is None
    try:
        layer = layer or DataLayer.from_config(config, cache=cache, offline=False)
        if layer.offline:
            return None
        records = records if records is not None else layer.build_candidates()
        planner = make_planner(config.acquisition.planner, llm or make_llm(config.llm))
        report = fill_gaps(layer, records, planner=planner, budget=config.acquisition.budget, kinds=kinds)
        log.info(
            "acquisition: %d gaps, %d filled, %d unfillable, planner %s",
            report.n_gaps,
            report.n_filled,
            len(report.unfillable),
            report.planner,
        )
        return report
    finally:
        if own_layer and layer is not None:
            layer.close()
        if own:
            cache.close()


def criteria_only(request_text: str, config: Config) -> Criteria:
    """Parse without running: handy for the UI to echo the interpretation live."""
    table = load_hazard_table(config.toxicity.table_file)
    return parse_request(request_text, config, table, make_llm(config.llm), blocked_by_policy(config, table))[
        0
    ]
