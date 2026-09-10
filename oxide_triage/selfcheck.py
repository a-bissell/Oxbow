"""Self-check: ground-truth validation the system runs on itself before serving results.

The known-answer check from the evaluation suite (the workhorse dielectrics must surface near
the top of an unconstrained run, or be excluded by a stated gate) is run automatically after
every cache warm, fixture load or on-demand material addition. Its outcome is stored in the cache
and read on every triage run; a failed check blocks or warns according to config.

Missing from the universe is a failure, not a shrug: if HfO2 is not in the candidate set, the
fetch is broken and no shortlist from that cache should be trusted.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from oxide_triage.cache import Cache, utcnow_iso
from oxide_triage.config import Config, load_config
from oxide_triage.edges.llm import NullLLM

if TYPE_CHECKING:  # pragma: no cover
    from oxide_triage.schemas import TriageResult

META_KEY = "selfcheck"
PI = (
    "Find promising oxide dielectric candidates for thin-film experiments. Prefer "
    "thermodynamically stable materials, wide band gaps, non-toxic elements, simple "
    "compositions, and public evidence. Return a ranked shortlist with caveats."
)


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


def _ranked(res: TriageResult) -> list[str]:
    return [s.record.formula for s in res.shortlist + res.ranked_beyond_shortlist]


def run_selfcheck(config: Config, cache: Cache) -> SelfCheck:
    from oxide_triage.pipeline import run_triage  # local import: pipeline imports this module

    sc = config.selfcheck
    details: list[str] = []
    ok = True

    default_cfg = load_config("default", use_env=False, overrides={"cache": {"path": config.cache.path}})
    res = run_triage(PI, default_cfg, cache=cache, offline=True, skip_selfcheck=True, llm=NullLLM())
    ranked = _ranked(res)
    universe = {s.record.formula for s in res.shortlist + res.ranked_beyond_shortlist + res.excluded}
    if not universe:
        return SelfCheck(
            passed=False,
            checked_at=utcnow_iso(),
            fixture=cache.has_fixture_data,
            n_candidates=0,
            details=["no candidates in cache"],
        )

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
            details=[note, "Warm the cache to completion and re-run `oxide-triage selfcheck`."],
        )
        cache.set_meta(META_KEY, result.model_dump_json())
        return result

    for w in sc.workhorses:
        if w not in universe:
            ok = False
            details.append(f"{w}: NOT IN CANDIDATE UNIVERSE (fetch or filter problem)")
        elif w in ranked:
            pos = ranked.index(w) + 1
            upper = pos <= len(ranked) / 2
            ok &= upper
            details.append(
                f"{w}: default rank {pos}/{len(ranked)}" + ("" if upper else " (below median: FAIL)")
            )
        else:
            ex = next((s for s in res.excluded if s.record.formula == w), None)
            reason = ex.exclusion_reasons[0] if ex and ex.exclusion_reasons else "no stated reason (FAIL)"
            ok &= bool(ex and ex.exclusion_reasons)
            details.append(f"{w}: excluded by gate: {reason}")
    for lead in sc.leaders:
        if lead in ranked and ranked.index(lead) >= 5:
            ok = False
            details.append(f"{lead}: expected in default top 5, found at {ranked.index(lead) + 1} (FAIL)")

    wide_cfg = load_config("exploratory", use_env=False, overrides={"cache": {"path": config.cache.path}})
    wide = run_triage(PI, wide_cfg, cache=cache, offline=True, skip_selfcheck=True, llm=NullLLM())
    wide_ranked = _ranked(wide)
    in_top10 = [w for w in sc.workhorses if w in wide_ranked[:10]]
    need = min(sc.min_workhorses_in_wide_top10, len(sc.workhorses))
    if len(in_top10) < need:
        ok = False
    details.append(
        f"exploratory: {len(in_top10)}/{len(sc.workhorses)} workhorses in top 10 ({', '.join(in_top10) or 'none'}); "
        f"need {need}" + ("" if len(in_top10) >= need else " (FAIL)")
    )
    if wide_ranked and wide_ranked[0] not in universe:
        ok = False  # unreachable in practice; kept for symmetry

    result = SelfCheck(
        passed=bool(ok),
        checked_at=utcnow_iso(),
        fixture=cache.has_fixture_data,
        n_candidates=res.n_candidates_considered,
        details=details,
        retrieval_completeness=completeness,
    )
    cache.set_meta(META_KEY, result.model_dump_json())
    return result


def read_selfcheck(cache: Cache) -> SelfCheck | None:
    raw = cache.get_meta(META_KEY)
    if not raw:
        return None
    try:
        return SelfCheck.model_validate(json.loads(raw))
    except ValueError:
        return None
