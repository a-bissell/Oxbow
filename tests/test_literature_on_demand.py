"""Literature (OpenAlex) is fetched at query time for the top-ranked pool, not during the warm.

OpenAlex meters a small daily budget, so the warm must not touch it and a query must fetch only
what it can use. Literature credit is never negative, so filling the pool can only move pool
members up relative to the rest; the shortlist is drawn from the pool."""

from __future__ import annotations

import pytest

from oxide_triage.acquire import GAP_KINDS, fill_gaps
from oxide_triage.config import load_config, load_hazard_table
from oxide_triage.edges.llm import NullLLM
from oxide_triage.pipeline import run_acquisition, run_triage
from oxide_triage.schemas import Criteria, DataStatus
from oxide_triage.scoring.core import rank
from oxide_triage.scoring.settings import resolve
from oxide_triage.sources.assemble import DataLayer
from tests.test_acquire import FakeHttp, make_layer
from tests.test_pipeline import PI

LIT = {
    "meta": {"count": 7},
    "results": [{"id": "https://openalex.org/W9", "title": "t", "publication_year": 2020}],
}


def _drop_literature(cache) -> None:
    cache._conn.execute("DELETE FROM records WHERE source='openalex'")
    cache._conn.commit()


def _openalex_formulas(http: FakeHttp) -> set[str]:
    out = set()
    for url, params in http.calls:
        if "openalex" in url:
            f = (params or {}).get("filter", "")
            out.add(f.split("(", 1)[1].split(" ", 1)[0].rstrip(")") if "(" in f else f)
    return out


def test_warm_skips_openalex_by_default():
    http = FakeHttp()
    http.when("openalex.org", None, LIT)
    layer, cache = make_layer(http)
    _drop_literature(cache)
    assert layer.config.literature.fetch == "on_demand"
    records = layer.build_candidates()
    assert records and not any("openalex" in url for url, _ in http.calls)
    lit = records[0].literature
    assert lit.status == DataStatus.UNKNOWN and "fetched at query time" in (lit.provenance.note or "")


def test_warm_mode_fetches_openalex_for_every_formula():
    http = FakeHttp()
    http.when("openalex.org", None, LIT)
    layer, cache = make_layer(http, overrides={"literature": {"fetch": "warm"}})
    _drop_literature(cache)
    records = layer.build_candidates()
    assert _openalex_formulas(http) == {r.formula for r in records}
    assert all(r.literature.status == DataStatus.KNOWN for r in records)


def test_fill_literature_touches_only_the_given_records():
    http = FakeHttp()
    http.when("openalex.org", None, LIT)
    layer, cache = make_layer(http)
    _drop_literature(cache)
    records = layer.build_candidates()
    pool = records[:3]
    filled, counts = layer.fill_literature(pool)
    assert _openalex_formulas(http) == {r.formula for r in pool}
    assert counts["resolved"] == 3 and all(r.literature.total_works == 7 for r in filled)
    # a second call is free: everything is cached now
    n = len(http.calls)
    layer.fill_literature(pool)
    assert len(http.calls) == n


def test_fill_literature_offline_is_a_no_op():
    http = FakeHttp()
    layer, cache = make_layer(http, offline=True)
    _drop_literature(cache)
    records = layer.build_candidates()
    filled, counts = layer.fill_literature(records[:2])
    assert counts == {"skipped_offline": 2} and not http.calls
    assert all(r.literature.status == DataStatus.UNKNOWN for r in filled)


def test_run_triage_fetches_pool_only_and_reranks(monkeypatch):
    http = FakeHttp()
    http.when("openalex.org", None, LIT)
    cfg = load_config("default", use_env=False, overrides={"literature": {"on_demand_pool": 3}})
    layer, cache = make_layer(http, overrides={"literature": {"on_demand_pool": 3}})
    _drop_literature(cache)
    monkeypatch.setattr(DataLayer, "from_config", classmethod(lambda cls, *a, **k: layer))
    res = run_triage(PI, cfg, cache=cache, offline=False, skip_selfcheck=True, llm=NullLLM())
    assert res.shortlist, res.warnings
    # a pool of 3 is raised to the shortlist size (top_k = 5): every shortlisted candidate has
    # literature, and nothing beyond the pool was fetched or credited
    top_k = cfg.output.top_k
    assert len(_openalex_formulas(http)) <= top_k
    assert all(s.record.literature.status == DataStatus.KNOWN for s in res.shortlist)
    assert any(f"fetched on demand for the top {top_k}" in w for w in res.warnings)
    assert all(s.record.literature.status == DataStatus.UNKNOWN for s in res.ranked_beyond_shortlist)


def test_pool_fill_never_demotes_pool_members():
    """Literature credit is non-negative: filling the pool can only move its members up."""
    http = FakeHttp()
    http.when("openalex.org", None, LIT)
    layer, cache = make_layer(http)
    _drop_literature(cache)
    cfg = layer.config
    eff, _ = resolve(cfg, Criteria(), load_hazard_table(cfg.toxicity.table_file))
    records = layer.build_candidates()
    before, _ = rank(records, cfg, eff)
    pool_ids = {s.record.material_id for s in before[:5]}
    filled, _ = layer.fill_literature([s.record for s in before[:5]])
    by_id = {r.material_id: r for r in filled}
    after, _ = rank([by_id.get(r.material_id, r) for r in records], cfg, eff)
    assert {s.record.material_id for s in after[:5]} == pool_ids


def test_warm_acquisition_leaves_literature_to_the_query_path():
    http = FakeHttp()
    http.when("openalex.org", None, LIT)
    layer, cache = make_layer(http)
    _drop_literature(cache)
    records = layer.build_candidates()
    report = run_acquisition(layer.config, cache, layer=layer, records=records, llm=NullLLM())
    assert report is not None
    assert not [a for a in report.attempts if a.kind == "literature"]
    assert not any("openalex" in url for url, _ in http.calls)
    # explicit kinds (add-material) do include literature
    report = fill_gaps(layer, records[:2], budget=10, kinds=GAP_KINDS)
    assert [a for a in report.attempts if a.kind == "literature"]


def test_fetch_mode_is_validated():
    with pytest.raises(ValueError):
        load_config("default", use_env=False, overrides={"literature": {"fetch": "sometimes"}})
    cfg = load_config("default", use_env=False, overrides={"literature": {"fetch": "never"}})
    assert cfg.literature.fetch == "never"
