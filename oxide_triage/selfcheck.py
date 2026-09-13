"""Self-check: ground-truth validation the system runs on itself before serving results.

The known-answer check from the evaluation suite (the profile's workhorse materials must
surface near the top of an unconstrained run, or be excluded by a stated gate) is run
automatically after every cache warm, fixture load or on-demand material addition. Its outcome
is stored in the cache per profile and read on every triage run; a failed check blocks or warns
according to config.

Missing from the universe is a failure, not a shrug: if HfO2 is not in the candidate set of the
oxide-dielectric profile, the fetch is broken and no shortlist from that cache should be trusted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from oxide_triage.cache import Cache, utcnow_iso
from oxide_triage.config import DEFAULT_SELFCHECK_REQUEST, Config, load_config
from oxide_triage.edges.llm import NullLLM
from oxide_triage.schemas import DataStatus

if TYPE_CHECKING:  # pragma: no cover
    from oxide_triage.schemas import TriageResult

META_KEY = "selfcheck"  # the verdict of the default profile, as every earlier cache stored it
PI = DEFAULT_SELFCHECK_REQUEST  # the default profile's request; kept for callers that import it


def meta_key(profile: str) -> str:
    return f"{META_KEY}:{profile}"


class SelfCheck(BaseModel):
    passed: bool
    checked_at: str
    fixture: bool
    n_candidates: int
    details: list[str] = Field(default_factory=list)
    # A check that could not run for lack of data is not a check that failed. `inconclusive`
    # keeps the two apart so an under-warmed cache does not read as a broken ranker.
    inconclusive: bool = False
    retrieval_completeness: float | None = None
    profile: str = "default"


def _ranked(res: TriageResult) -> list[str]:
    return [s.record.formula for s in res.shortlist + res.ranked_beyond_shortlist]


def _store(cache: Cache, result: SelfCheck) -> None:
    cache.set_meta(meta_key(result.profile), result.model_dump_json())
    if result.profile == "default":
        cache.set_meta(META_KEY, result.model_dump_json())  # the key older caches and bundles read


def run_selfcheck(
    config: Config, cache: Cache, offline: bool | None = None, http: Any | None = None
) -> SelfCheck:
    """Known-answer check under the active profile. Runs from the cache alone, except under
    on-demand formula sources on a live cache, where the ranked pool is filled online first:
    the warm deliberately leaves those sources unfetched, and a check that then reported "too
    sparse" would test nothing."""
    from oxide_triage.pipeline import run_triage  # local import: pipeline imports this module

    sc = config.selfcheck
    profile = config.profile_name
    details: list[str] = []
    ok = True
    if offline is None:
        offline = not (
            config.candidates.formula_sources == "on_demand"
            and not config.cache.offline
            and not cache.has_fixture_data
        )

    # The check must judge the policy actually in force: the site file is applied, the environment
    # is not (the cache path is passed explicitly).
    site = Path(config.site_config_path) if config.site_config_path else None
    cfg = load_config(
        profile, use_env=False, overrides={"cache": {"path": config.cache.path}}, site_config=site
    )
    fom = cfg.figure_of_merit
    res = run_triage(
        sc.request, cfg, cache=cache, offline=offline, skip_selfcheck=True, llm=NullLLM(), http=http
    )
    ranked = _ranked(res)
    universe = {s.record.formula for s in res.shortlist + res.ranked_beyond_shortlist + res.excluded}
    if not universe:
        result = SelfCheck(
            passed=False,
            checked_at=utcnow_iso(),
            fixture=cache.has_fixture_data,
            n_candidates=0,
            details=["no candidates in cache"],
            profile=profile,
        )
        _store(cache, result)
        return result

    # Ground truth cannot be tested on data that was never fetched. Missing criteria lower a
    # score, so on a half-retrieved cache the workhorses sink for reasons that say nothing about
    # the ranker. Report that honestly instead of raising a false alarm.
    completeness = res.retrieval.completeness if res.retrieval else 1.0
    if completeness < sc.min_retrieval_completeness:
        note = (
            f"INCONCLUSIVE: retrieval completeness {completeness:.1%} is below the "
            f"{sc.min_retrieval_completeness:.0%} required to validate ranks. "
            + (res.retrieval.note if res.retrieval else "")
        )
        result = SelfCheck(
            passed=False,
            inconclusive=True,
            retrieval_completeness=completeness,
            checked_at=utcnow_iso(),
            fixture=cache.has_fixture_data,
            n_candidates=res.n_candidates_considered,
            details=[
                note,
                f"Warm the cache to completion and re-run `oxide-triage selfcheck --profile {profile}`.",
            ],
            profile=profile,
        )
        _store(cache, result)
        return result

    for w in sc.workhorses:
        if w not in universe:
            ok = False
            details.append(f"{w}: NOT IN CANDIDATE UNIVERSE (fetch or filter problem)")
        elif w in ranked:
            pos = ranked.index(w) + 1
            upper = pos <= len(ranked) / 2
            if upper:
                details.append(f"{w}: {profile} rank {pos}/{len(ranked)}")
            else:
                # A workhorse below the median is a ranker fault unless a criterion that carries
                # its own evidence explains it. The interface criterion does for Ta2O5 on Si: it
                # reacts (-0.30 eV/atom on the hull), which is why it is a capacitor dielectric on
                # TiN and not a gate oxide, and a Si-substrate triage should say so, not hide it.
                # A figure of merit the source holds no record for is the other honest reason:
                # the material is known, the public data is not, and the check names that.
                sc_w = next(s for s in res.shortlist + res.ranked_beyond_shortlist if s.record.formula == w)
                iface = sc_w.record.interface
                ic = cfg.interface
                reacts = (
                    iface.status is DataStatus.KNOWN
                    and iface.reaction_energy_ev_atom is not None
                    and iface.reaction_energy_ev_atom < -ic.tolerance_ev_atom
                )
                fom_absent = sc_w.record.figure_of_merit.status is DataStatus.ABSENT
                if reacts:
                    details.append(
                        f"{w}: {profile} rank {pos}/{len(ranked)} (below median, explained: reacts with "
                        f"{iface.substrate} at {iface.reaction_energy_ev_atom:+.2f} eV/atom on the hull)"
                    )
                elif fom_absent:
                    details.append(
                        f"{w}: {profile} rank {pos}/{len(ranked)} (below median, explained: no public "
                        f"{fom.label} record at the source, so its {fom.criterion.replace('_', ' ')} merit "
                        "is unverifiable, not low)"
                    )
                else:
                    ok = False
                    details.append(f"{w}: {profile} rank {pos}/{len(ranked)} (below median: FAIL)")
        else:
            ex = next((s for s in res.excluded if s.record.formula == w), None)
            reason = ex.exclusion_reasons[0] if ex and ex.exclusion_reasons else "no stated reason (FAIL)"
            ok &= bool(ex and ex.exclusion_reasons)
            details.append(f"{w}: excluded by gate: {reason}")
    for lead in sc.leaders:
        if lead in ranked and ranked.index(lead) >= sc.leaders_top_n:
            ok = False
            details.append(
                f"{lead}: expected in {profile} top {sc.leaders_top_n}, found at {ranked.index(lead) + 1} (FAIL)"
            )

    if sc.wide_profile:
        wide_cfg = load_config(
            sc.wide_profile, use_env=False, overrides={"cache": {"path": config.cache.path}}, site_config=site
        )
        wide = run_triage(
            sc.request, wide_cfg, cache=cache, offline=offline, skip_selfcheck=True, llm=NullLLM(), http=http
        )
        wide_ranked = _ranked(wide)
        in_top = [w for w in sc.workhorses if w in wide_ranked[: sc.wide_top_n]]
        need = min(sc.min_workhorses_in_wide_top, len(sc.workhorses))
        if len(in_top) < need:
            ok = False
        details.append(
            f"{sc.wide_profile}: {len(in_top)}/{len(sc.workhorses)} workhorses in top {sc.wide_top_n} "
            f"({', '.join(in_top) or 'none'}); need {need}" + ("" if len(in_top) >= need else " (FAIL)")
        )

    result = SelfCheck(
        passed=bool(ok),
        checked_at=utcnow_iso(),
        fixture=cache.has_fixture_data,
        n_candidates=res.n_candidates_considered,
        details=details,
        retrieval_completeness=completeness,
        profile=profile,
    )
    _store(cache, result)
    return result


def read_selfcheck(cache: Cache, profile: str = "default") -> SelfCheck | None:
    """The stored verdict for ``profile``; falls back to the default profile's verdict (a cache
    warmed under default still gates the profiles that share its universe) and then to the key
    every earlier cache wrote."""
    for key in dict.fromkeys((meta_key(profile), meta_key("default"), META_KEY)):
        raw = cache.get_meta(key)
        if not raw:
            continue
        try:
            return SelfCheck.model_validate(json.loads(raw))
        except ValueError:
            continue
    return None
