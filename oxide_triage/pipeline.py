"""Orchestration. Read top to bottom, this is the whole system:

    guard  ->  parse (front edge)  ->  data layer (cache)  ->  deterministic core
           ->  refutation (annotates)  ->  render (back edge, templates)

The language model, if configured, is invoked in exactly two places (parse, refute) and its
output is validated before use. The core never sees it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from oxide_triage.cache import Cache
from oxide_triage.config import Config, load_hazard_table
from oxide_triage.edges.llm import LLMClient, make_llm
from oxide_triage.edges.parse import parse_request
from oxide_triage.edges.render import rationale_line
from oxide_triage.guard import guard_request
from oxide_triage.refute import refute, rule_caveats
from oxide_triage.schemas import Criteria, GuardDecision, TriageResult
from oxide_triage.scoring.core import explanation, rank
from oxide_triage.scoring.settings import resolve
from oxide_triage.sources.assemble import DataLayer
from oxide_triage.sources.fixtures import load_fixture

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


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


def run_triage(
    request_text: str,
    config: Config,
    cache: Cache | None = None,
    offline: bool | None = None,
    llm: LLMClient | None = None,
    template: str | None = None,
) -> TriageResult:
    table = load_hazard_table(config.toxicity.table_file)
    llm = llm or make_llm(config.llm)
    guard: GuardDecision = guard_request(request_text, table)
    criteria, parser_label = parse_request(request_text, config, table, llm)
    if template:
        criteria.output_template = template  # type: ignore[assignment]
    eff, deviations = resolve(config, criteria, table)
    llm_usage = {"parse": parser_label, "refute": "not run", "render": "templates only"}

    own_cache = cache is None
    cache = cache or Cache(config.cache.path)
    try:
        if not guard.proceed:
            return TriageResult(
                request_text=request_text,
                criteria=criteria,
                guard=guard,
                profile_name=config.profile_name,
                config_hash=config.config_hash(),
                cache_fingerprint=cache.fingerprint([]),
                generated_at=_now(),
                fixture_data=cache.has_fixture_data,
                offline=bool(config.cache.offline if offline is None else offline),
                deviations=deviations,
                scoring=explanation(config, eff),
                warnings=[guard.refusal_message or "request declined"],
                llm_usage=llm_usage,
            )

        layer = DataLayer.from_config(config, cache=cache, offline=offline)
        records = layer.build_candidates()
        ranked, excluded = rank(records, config, eff)
        shortlist, beyond = ranked[: eff.top_k], ranked[eff.top_k :]

        llm_usage["refute"] = refute(shortlist, eff, config, llm)
        for sc in beyond:
            sc.caveats = rule_caveats(sc, eff, config)
        for sc in ranked:
            sc.rationale = rationale_line(sc)

        result = TriageResult(
            request_text=request_text,
            criteria=criteria,
            guard=guard,
            profile_name=config.profile_name,
            config_hash=config.config_hash(),
            cache_fingerprint=cache.fingerprint(),
            generated_at=_now(),
            fixture_data=cache.has_fixture_data,
            offline=layer.offline,
            deviations=deviations,
            scoring=explanation(config, eff),
            shortlist=shortlist,
            ranked_beyond_shortlist=beyond,
            excluded=excluded,
            n_candidates_considered=len(records),
            warnings=list(layer.warnings),
            llm_usage=llm_usage,
        )
        _log_deviations(config, result)
        return result
    finally:
        if own_cache:
            cache.close()


def warm_cache(config: Config, cache: Cache | None = None) -> dict[str, object]:
    """Fetch the candidate universe and every per-candidate record into the cache (online)."""
    own = cache is None
    cache = cache or Cache(config.cache.path)
    try:
        layer = DataLayer.from_config(config, cache=cache, offline=False)
        records = layer.build_candidates()
        return {
            "candidates": len(records),
            "warnings": list(layer.warnings),
            "sources": cache.sources_summary(),
            "fixture_data": cache.has_fixture_data,
        }
    finally:
        if own:
            cache.close()


def load_fixtures(config: Config, cache: Cache | None = None) -> int:
    own = cache is None
    cache = cache or Cache(config.cache.path)
    try:
        return load_fixture(cache, config)
    finally:
        if own:
            cache.close()


def criteria_only(request_text: str, config: Config) -> Criteria:
    """Parse without running: handy for the UI to echo the interpretation live."""
    table = load_hazard_table(config.toxicity.table_file)
    return parse_request(request_text, config, table, make_llm(config.llm))[0]
