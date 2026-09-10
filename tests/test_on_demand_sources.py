"""The formula-keyed sources (OQMD, OpenAlex, PubChem) are fetched at query time for the
top-ranked pool, not during the warm.

OQMD is slow and OpenAlex meters a daily budget, so the warm touches Materials Project only and a
query fills what it can use. Literature and compound hazards only add credit or caveats, but an
OQMD disagreement lowers a score, so the pipeline re-ranks after each fill and repeats until the
pool is settled; the shortlist is drawn from a fully retrieved pool."""

from __future__ import annotations

import pytest

from oxide_triage.acquire import GAP_KINDS, fill_gaps
from oxide_triage.config import load_config, load_hazard_table
from oxide_triage.edges.llm import NullLLM
from oxide_triage.pipeline import run_acquisition, run_triage
from oxide_triage.schemas import Criteria, DataStatus
from oxide_triage.scoring.core import rank
from oxide_triage.scoring.settings import resolve
from oxide_triage.selfcheck import run_selfcheck
from oxide_triage.sources.assemble import DataLayer
from tests.test_acquire import FakeHttp, make_layer
from tests.test_pipeline import PI

LIT = {
    "meta": {"count": 7},
    "results": [{"id": "https://openalex.org/W9", "title": "t", "publication_year": 2020}],
}
OQMD_AGREE = {"data": [{"name": "X", "entry_id": 1, "stability": 0.0, "delta_e": -3.0, "spacegroup": "P1"}]}
SOURCES = ("oqmd", "openalex", "pubchem")


def _drop_formula_sources(cache) -> None:
    cache._conn.execute("DELETE FROM records WHERE source IN ('oqmd','openalex','pubchem')")
    cache._conn.commit()


def _hosts(http: FakeHttp) -> set[str]:
    return {h for url, _ in http.calls for h in ("oqmd", "openalex", "pubchem") if h in url}


def _formulas_queried(http: FakeHttp, host: str) -> set[str]:
    out = set()
    for url, params in http.calls:
        if host == "oqmd" and "oqmd" in url:
            out.add((params or {}).get("composition", ""))
        if host == "openalex" and "openalex" in url:
            f = (params or {}).get("filter", "")
            out.add(f.split("(", 1)[1].split(" ", 1)[0].rstrip(")") if "(" in f else f)
    return out - {""}


def _online_layer(http: FakeHttp, **overrides):
    http.when("openalex.org", None, LIT)
    http.when("oqmd.org", None, OQMD_AGREE)
    http.when("pubchem", None, None)  # documented "no record": answered, so ABSENT rather than unretrieved
    layer, cache = make_layer(http, overrides=overrides or None)
    _drop_formula_sources(cache)
    return layer, cache


def test_warm_touches_no_formula_source_by_default():
    http = FakeHttp()
    layer, _ = _online_layer(http)
    assert layer.config.candidates.formula_sources == "on_demand"
    records = layer.build_candidates()
    assert records and not _hosts(http)
    r = records[0]
    assert r.cross_check.status is DataStatus.NOT_RETRIEVED
    assert r.literature.status is DataStatus.NOT_RETRIEVED
    assert r.hazard.pubchem_status is DataStatus.NOT_RETRIEVED
    assert "on demand" in (r.cross_check.provenance.note or "")
    assert layer.needs_formula_sources(r)


def test_warm_mode_fetches_every_formula_source():
    http = FakeHttp()
    layer, _ = _online_layer(http, candidates={"formula_sources": "warm"})
    records = layer.build_candidates()
    assert _hosts(http) == set(SOURCES)
    assert _formulas_queried(http, "oqmd") == {r.formula for r in records}
    assert all(
        r.cross_check.status is DataStatus.KNOWN and r.literature.status is DataStatus.KNOWN for r in records
    )


def test_fill_touches_only_the_given_records_and_is_then_free():
    http = FakeHttp()
    layer, _ = _online_layer(http)
    records = layer.build_candidates()
    pool = records[:3]
    filled, counts = layer.fill_formula_sources(pool)
    assert _formulas_queried(http, "oqmd") == {r.formula for r in pool}
    assert counts["resolved"] == 3
    for r in filled:
        assert r.cross_check.status is DataStatus.KNOWN and r.literature.total_works == 7
        assert r.hazard.pubchem_status in (DataStatus.KNOWN, DataStatus.ABSENT)  # answered either way
        assert not layer.needs_formula_sources(r)
    n = len(http.calls)
    layer.fill_formula_sources(pool)
    assert len(http.calls) == n


def test_fill_applies_the_ladder_fallbacks_inline():
    """OQMD finds nothing by composition -> chemical-system route; OpenAlex zero works -> names."""
    http = FakeHttp()
    http.when("oqmd.org", {"composition": "LaLuO3"}, {"data": []})
    http.when(
        "oqmd.org",
        {"filter": "element_set=La,Lu,O AND ntypes=3"},
        {
            "data": [
                {
                    "name": "LaLuO3",
                    "entry_id": 7,
                    "stability": 0.0,
                    "delta_e": -3.5,
                    "composition": "La1Lu1O3",
                }
            ]
        },
    )
    http.when("oqmd.org", None, {"data": []})
    http.when(
        "openalex.org",
        {"filter": 'title_and_abstract.search:("lanthanum lutetium oxide")'},
        {"meta": {"count": 41}, "results": []},
    )
    http.when(
        "openalex.org",
        {
            "filter": 'title_and_abstract.search:("lanthanum lutetium oxide") AND ("thin film" OR "thin films" OR "atomic layer deposition" OR sputtered OR sputtering OR epitaxial OR "pulsed laser deposition" OR "chemical vapor deposition")'
        },
        {"meta": {"count": 12}, "results": []},
    )
    http.when("openalex.org", None, {"meta": {"count": 0}, "results": []})
    layer, cache = make_layer(http)
    _drop_formula_sources(cache)
    rec = next(r for r in layer.build_candidates() if r.formula == "LaLuO3")
    (filled,), _ = layer.fill_formula_sources([rec])
    assert filled.cross_check.status is DataStatus.KNOWN
    assert "chemical-system" in (filled.cross_check.provenance.note or "")
    assert filled.literature.total_works == 41 and "common-name" in (filled.literature.provenance.note or "")


def test_fill_offline_is_a_no_op():
    http = FakeHttp()
    layer, cache = make_layer(http, offline=True)
    _drop_formula_sources(cache)
    records = layer.build_candidates()
    filled, counts = layer.fill_formula_sources(records[:2])
    assert counts == {"skipped_offline": 2} and not http.calls
    assert all(layer.needs_formula_sources(r) for r in filled)


def test_run_triage_fills_the_pool_and_scopes_completeness(monkeypatch):
    http = FakeHttp()
    layer, cache = _online_layer(http, candidates={"on_demand_pool": 3})
    cfg = layer.config
    monkeypatch.setattr(DataLayer, "from_config", classmethod(lambda cls, *a, **k: layer))
    res = run_triage(PI, cfg, cache=cache, offline=False, skip_selfcheck=True, llm=NullLLM())
    assert res.shortlist, res.warnings
    top_k = cfg.output.top_k  # a pool of 3 is raised to the shortlist size
    assert len(_formulas_queried(http, "oqmd")) <= top_k
    assert all(not layer.needs_formula_sources(s.record) for s in res.shortlist)
    assert all(layer.needs_formula_sources(s.record) for s in res.ranked_beyond_shortlist)
    assert any("queried on demand for the top" in w for w in res.warnings)
    assert res.retrieval is not None and res.retrieval.comparable and "on-demand pool" in res.retrieval.note


def test_pool_settles_when_a_disagreement_demotes_a_leader(monkeypatch):
    """The initial leader's OQMD entry disagrees with MP, so it drops out of the pool after the
    first fill; the candidate that takes its place is then fetched in a second round."""
    http = FakeHttp()
    http.when("openalex.org", None, LIT)
    http.when("pubchem", None, None)
    layer, cache = make_layer(
        http,
        overrides={
            "candidates": {"on_demand_pool": 1},
            "output": {"top_k": 1},
            "stability": {"disagreement_penalty": 1.0},  # make the disagreement decisive
        },
    )
    _drop_formula_sources(cache)
    cfg = layer.config
    eff, _ = resolve(cfg, Criteria(), load_hazard_table(cfg.toxicity.table_file))
    first, _ = rank(layer.build_candidates(), cfg, eff)
    leader = first[0].record.formula
    http.when(
        "oqmd.org",
        {"composition": leader},
        {"data": [{"name": leader, "entry_id": 1, "stability": 0.4, "delta_e": -1.0}]},
    )
    http.when("oqmd.org", None, OQMD_AGREE)
    monkeypatch.setattr(DataLayer, "from_config", classmethod(lambda cls, *a, **k: layer))
    res = run_triage(PI, cfg, cache=cache, offline=False, skip_selfcheck=True, llm=NullLLM())
    assert res.shortlist[0].record.formula != leader
    assert not layer.needs_formula_sources(res.shortlist[0].record)
    assert any("over 2 rounds" in w for w in res.warnings), res.warnings
    assert {leader, res.shortlist[0].record.formula} <= _formulas_queried(http, "oqmd")


def test_warm_acquisition_leaves_formula_gaps_to_the_query_path():
    http = FakeHttp()
    layer, cache = _online_layer(http)
    records = layer.build_candidates()
    report = run_acquisition(layer.config, cache, layer=layer, records=records, llm=NullLLM())
    assert report is not None
    assert not [a for a in report.attempts if a.kind in ("literature", "cross_check")]
    assert not _hosts(http)
    report = fill_gaps(layer, records[:2], budget=10, kinds=GAP_KINDS)  # add-material passes every kind
    assert [a for a in report.attempts if a.kind in ("literature", "cross_check")]


def test_selfcheck_goes_online_for_the_pool_only_on_a_live_cache():
    fixture_cfg = load_config("default", use_env=False)
    http = FakeHttp()
    layer, cache = _online_layer(http)
    assert cache.has_fixture_data
    run_selfcheck(fixture_cfg, cache)  # fixture cache: stays offline, no fetch
    assert not http.calls


def test_config_validation():
    with pytest.raises(ValueError):
        load_config("default", use_env=False, overrides={"candidates": {"formula_sources": "sometimes"}})
    cfg = load_config("default", use_env=False, overrides={"candidates": {"formula_sources": "warm"}})
    assert cfg.candidates.formula_sources == "warm"
