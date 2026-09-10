"""Adaptive acquisition: gap detection, route ladder, planner validation, budget, offline.

Routes are exercised against a fake HTTP layer so the mechanism is tested end to end without
network. Whether the *live* alternative endpoints behave as documented is a separate check that
needs a real connection (see README, "first live warm").
"""

from __future__ import annotations

from typing import Any

import pytest

from oxide_triage.acquire import (
    LADDER,
    Gap,
    LadderPlanner,
    LLMPlanner,
    detect_gaps,
    fill_gaps,
    read_report,
)
from oxide_triage.cache import Cache
from oxide_triage.config import load_config
from oxide_triage.formula import elements_of, parse_formula, reduced, same_stoichiometry
from oxide_triage.pipeline import run_triage
from oxide_triage.sources.assemble import DataLayer
from oxide_triage.sources.base import SourceError
from oxide_triage.sources.fixtures import load_fixture
from oxide_triage.sources.materials_project import MaterialsProject
from oxide_triage.sources.openalex import OpenAlex
from oxide_triage.sources.oqmd import OQMD
from oxide_triage.sources.pubchem import PubChem


class FakeHttp:
    """Answers by (url substring, matching params) rules; records every call."""

    def __init__(self) -> None:
        self.rules: list[tuple[str, dict[str, Any] | None, Any]] = []
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def when(self, url_part: str, params_subset: dict[str, Any] | None, response: Any) -> None:
        self.rules.append((url_part, params_subset, response))

    def get_json(
        self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        self.calls.append((url, params))
        for part, subset, response in self.rules:
            if part in url and all((params or {}).get(k) == v for k, v in (subset or {}).items()):
                if isinstance(response, Exception):
                    raise response
                return response
        raise SourceError(f"no fake rule for {url} {params}")


def make_layer(
    http: FakeHttp, offline: bool = False, overrides: dict[str, Any] | None = None
) -> tuple[DataLayer, Cache]:
    cfg = load_config("default", use_env=False, overrides=overrides)
    cache = Cache(":memory:")
    load_fixture(cache, cfg)
    layer = DataLayer.from_config(cfg, cache=cache, offline=offline)
    layer.mp = MaterialsProject(cache, 90, offline, api_key="test", http=http)  # type: ignore[arg-type]
    layer.oqmd = OQMD(cache, 90, offline, http=http)  # type: ignore[arg-type]
    layer.openalex = OpenAlex(cache, 90, offline, http=http, sample_size=2)  # type: ignore[arg-type]
    layer.pubchem = PubChem(cache, 90, offline, http=http)  # type: ignore[arg-type]
    return layer, cache


# ---- formula helpers -----------------------------------------------------------------------


def test_formula_parsing_and_matching():
    assert parse_formula("Y3Al5O12") == {"Y": 3, "Al": 5, "O": 12}
    assert reduced(parse_formula("Hf2O4")) == {"Hf": 1, "O": 2}
    assert same_stoichiometry("Al5Y3O12", "Y3Al5O12") and same_stoichiometry("Hf1O2", "HfO2")
    assert not same_stoichiometry("HfO2", "Hf2O3") and not same_stoichiometry("garbage!", "HfO2")
    assert elements_of("SrHfO3") == ["Hf", "O", "Sr"]
    assert parse_formula("Mg(OH)2") == {"Mg": 1, "O": 2, "H": 2}


# ---- gap detection --------------------------------------------------------------------------


def test_detect_gaps_on_fixture():
    layer, _ = make_layer(FakeHttp(), offline=True)
    gaps = detect_gaps(layer.build_candidates())
    kinds = {(g.formula, g.kind) for g in gaps}
    assert ("SrHfO3", "cross_check") in kinds  # fixture: no OQMD entry
    assert ("LaLuO3", "cross_check") in kinds
    assert ("Sc2O3", "functional") in kinds  # fixture: run_type None
    assert ("Y2O3", "dielectric") in kinds
    assert not any(k == "literature" for _, k in kinds)  # fixture literature is complete


# ---- routes fill gaps through the same cache keys --------------------------------------------


def test_chemsys_route_fills_oqmd_gap_and_provenance_says_so():
    http = FakeHttp()
    http.when(
        "oqmd.org",
        {"filter": "element_set=Hf,O,Sr AND ntypes=3"},
        {
            "data": [
                {
                    "name": "Sr2Hf2O6",
                    "entry_id": 777,
                    "delta_e": -3.5,
                    "stability": 0.004,
                    "spacegroup": "Pnma",
                },
                {"name": "SrHf2O5", "entry_id": 778, "delta_e": -3.0, "stability": 0.1, "spacegroup": "P1"},
            ]
        },
    )
    layer, cache = make_layer(http)
    records = layer.build_candidates()
    gaps = [g for g in detect_gaps(records) if g.formula == "SrHfO3" and g.kind == "cross_check"]
    report = fill_gaps(layer, records, ladder={"cross_check": ["oqmd_chemsys"]}, budget=10)
    assert gaps and report.n_filled >= 1
    a = next(x for x in report.attempts if x.formula == "SrHfO3")
    assert a.route == "oqmd_chemsys" and a.outcome == "filled" and "777" in a.note
    rebuilt = next(r for r in layer.build_candidates() if r.formula == "SrHfO3")
    assert rebuilt.cross_check.stability_ev_atom == 0.004  # stoichiometry-matched entry, not SrHf2O5
    assert "chemical-system" in rebuilt.cross_check.provenance.note
    assert read_report(cache) is not None and read_report(cache).n_filled == report.n_filled


def test_chemsys_route_reports_no_match_when_stoichiometry_differs():
    http = FakeHttp()
    http.when("oqmd.org", None, {"data": [{"name": "SrHf2O5", "entry_id": 1, "stability": 0.0}]})
    layer, _ = make_layer(http)
    records = layer.build_candidates()
    report = fill_gaps(layer, records, ladder={"cross_check": ["oqmd_chemsys"]}, budget=50)
    outcomes = {a.formula: a.outcome for a in report.attempts if a.kind == "cross_check"}
    assert outcomes["SrHfO3"] == "no_match" and report.n_filled == 0


def test_literature_ladder_falls_back_to_names_only():
    http = FakeHttp()
    # Formula query returns zero works; names-only query returns some.
    http.when(
        "openalex.org",
        {"filter": 'title_and_abstract.search:(LaLuO3 OR "lanthanum lutetium oxide")'},
        {"meta": {"count": 0}, "results": []},
    )
    http.when(
        "openalex.org",
        {
            "filter": 'title_and_abstract.search:(LaLuO3 OR "lanthanum lutetium oxide") AND ("thin film" OR "thin films" OR "atomic layer deposition" OR sputtered OR sputtering OR epitaxial OR "pulsed laser deposition" OR "chemical vapor deposition")'
        },
        {"meta": {"count": 0}, "results": []},
    )
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
        {
            "meta": {"count": 12},
            "results": [{"id": "https://openalex.org/W1", "title": "LaLuO3 films", "publication_year": 2012}],
        },
    )
    layer, cache = make_layer(http, overrides={"literature": {"fetch": "warm"}})
    # Simulate a warm where the literature fetch for LaLuO3 failed: drop the cached row.
    cache._conn.execute("DELETE FROM records WHERE source='openalex' AND key='formula:LaLuO3'")
    cache._conn.commit()
    records = layer.build_candidates()  # online: the formula query is retried here and returns 0 works
    lit = next(r for r in records if r.formula == "LaLuO3").literature
    assert lit.status.value == "known" and lit.total_works == 0
    report = fill_gaps(layer, records, ladder={"literature": LADDER["literature"]}, budget=10)
    routes = [(a.route, a.outcome) for a in report.attempts if a.formula == "LaLuO3"]
    assert routes == [("openalex_retry", "not_applicable"), ("openalex_names_only", "filled")]
    rebuilt = next(r for r in layer.build_candidates() if r.formula == "LaLuO3")
    assert rebuilt.literature.thin_film_works == 12 and rebuilt.literature.query_terms == [
        "lanthanum lutetium oxide"
    ]
    assert "common-name" in rebuilt.literature.provenance.note


def test_functional_refresh_route():
    http = FakeHttp()
    http.when(
        "materials/summary",
        {"material_ids": "fx-0011"},
        {
            "data": [
                {
                    "material_id": "fx-0011",
                    "formula_pretty": "Sc2O3",
                    "elements": ["Sc", "O"],
                    "nelements": 2,
                    "symmetry": {"crystal_system": "Cubic", "symbol": "Ia-3"},
                    "energy_above_hull": 0.0,
                    "formation_energy_per_atom": -3.92,
                    "is_stable": True,
                    "band_gap": 3.92,
                    "is_gap_direct": False,
                    "theoretical": False,
                    "deprecated": False,
                    "origins": [{"name": "band_gap", "task_id": "t-new"}],
                }
            ]
        },
    )
    http.when("materials/tasks", {"task_ids": "t-new"}, {"data": [{"task_id": "t-new", "run_type": "GGA"}]})
    layer, _ = make_layer(http)
    records = layer.build_candidates()
    report = fill_gaps(layer, records, ladder={"functional": ["mp_refresh_functional"]}, budget=10)
    a = next(x for x in report.attempts if x.formula == "Sc2O3")
    assert a.outcome == "filled" and a.note == "GGA"
    assert next(r for r in layer.build_candidates() if r.formula == "Sc2O3").band_gap.functional == "GGA"


def test_dielectric_gaps_are_unfillable_and_named():
    layer, _ = make_layer(FakeHttp())
    records = layer.build_candidates()
    report = fill_gaps(layer, records, ladder={"dielectric": []}, budget=10)
    assert report.attempts == [] and report.n_filled == 0
    diel = [g for g in report.unfillable if g.kind == "dielectric"]
    assert {g.formula for g in diel} >= {"Y2O3", "La2O3", "LaLuO3"}
    # and the record is still unknown, not estimated
    assert next(r for r in layer.build_candidates() if r.formula == "Y2O3").dielectric.e_total is None


# ---- budget, offline, errors ----------------------------------------------------------------


def test_budget_is_respected_and_reported():
    http = FakeHttp()
    http.when("oqmd.org", None, {"data": []})
    layer, _ = make_layer(http)
    records = layer.build_candidates()
    report = fill_gaps(layer, records, ladder={"cross_check": ["oqmd_chemsys"]}, budget=1)
    assert len(report.attempts) == 1 and report.budget_exhausted


def test_offline_layer_skips_without_calling_anything():
    http = FakeHttp()
    layer, _ = make_layer(http, offline=True)
    records = layer.build_candidates()
    report = fill_gaps(layer, records, budget=50)
    assert http.calls == []
    assert all(a.outcome in {"skipped_offline", "not_applicable"} for a in report.attempts)


def test_route_errors_are_recorded_not_raised():
    http = FakeHttp()
    http.when("oqmd.org", None, SourceError("boom"))
    layer, _ = make_layer(http)
    records = layer.build_candidates()
    report = fill_gaps(layer, records, ladder={"cross_check": ["oqmd_chemsys"]}, budget=5)
    assert report.attempts and all(a.outcome == "error" for a in report.attempts)


# ---- planners --------------------------------------------------------------------------------


class ScriptedLLM:
    name = "fake:planner"

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def complete_json(self, system, user, schema):
        self.calls += 1
        return self.answer


GAP = Gap(material_id="x", formula="LaLuO3", kind="literature", detail="d")


def test_ladder_planner_skips_tried():
    assert LadderPlanner().order(GAP, ["a", "b", "c"], ["a"]) == ["b", "c"]


def test_llm_planner_can_reorder_and_skip_but_not_invent():
    p = LLMPlanner(
        ScriptedLLM(
            {"routes": ["openalex_names_only", "fetch_from_icsd", "openalex_names_only"], "reason": "x"}
        )
    )
    assert p.order(GAP, ["openalex_retry", "openalex_names_only"], []) == ["openalex_names_only"]


def test_llm_planner_falls_back_to_ladder_on_garbage():
    p = LLMPlanner(ScriptedLLM({"routes": ["nonsense"], "reason": "x"}))
    assert p.order(GAP, ["openalex_retry", "openalex_names_only"], []) == [
        "openalex_retry",
        "openalex_names_only",
    ]
    p = LLMPlanner(ScriptedLLM(None))
    assert p.order(GAP, ["a", "b"], []) == ["a", "b"]


def test_llm_planner_not_consulted_for_single_route():
    llm = ScriptedLLM({"routes": [], "reason": ""})
    assert LLMPlanner(llm).order(GAP, ["only"], []) == ["only"] and llm.calls == 0


# ---- result surfaces the last pass -----------------------------------------------------------


def test_result_carries_acquisition_summary():
    http = FakeHttp()
    http.when("oqmd.org", None, {"data": []})
    layer, cache = make_layer(http)
    fill_gaps(layer, layer.build_candidates(), ladder={"cross_check": ["oqmd_chemsys"]}, budget=5)
    from oxide_triage.selfcheck import run_selfcheck

    cfg = load_config("default", use_env=False)
    run_selfcheck(cfg, cache)
    res = run_triage(
        "Find promising oxide dielectric candidates for thin-film experiments.",
        cfg,
        cache=cache,
        offline=True,
    )
    assert res.acquisition_summary and res.acquisition_summary["planner"] == "ladder"
    from oxide_triage.edges.render import render

    assert "Last acquisition pass" in render(res, "audit")


@pytest.mark.parametrize("kind", ["cross_check", "literature", "functional", "dielectric"])
def test_every_gap_kind_has_a_ladder_entry(kind):
    assert kind in LADDER


# ---- concurrent prefetch of formula-keyed sources -----------------------------------------------


def test_prefetch_fetches_each_formula_once_and_writes_cache_from_main_thread():
    http = FakeHttp()
    http.when("oqmd.org", None, {"data": [{"name": "X", "entry_id": 1, "stability": 0.0, "delta_e": -1.0}]})
    http.when("openalex.org", None, {"meta": {"count": 3}, "results": []})
    http.when("pubchem.ncbi.nlm.nih.gov/rest/pug/compound", None, {"IdentifierList": {"CID": [42]}})
    http.when(
        "pug_view",
        None,
        {"Record": {"Section": [{"Information": [{"Value": {"StringWithMarkup": [{"String": "H302"}]}}]}]}},
    )
    layer, cache = make_layer(http)
    # wipe formula-keyed rows for two formulas (one with two polymorphs: HfO2)
    for f in ("HfO2", "ZrO2"):
        for src in ("oqmd", "openalex", "pubchem"):
            cache._conn.execute("DELETE FROM records WHERE source=? AND key=?", (src, f"formula:{f}"))
    cache._conn.commit()
    counts = layer.prefetch_formula_sources(["HfO2", "HfO2", "ZrO2"], workers=3)
    assert counts == {"fetched": 6, "failed": 0}
    oqmd_calls = [c for c in http.calls if "oqmd" in c[0]]
    assert len(oqmd_calls) == 2  # HfO2 once despite two polymorphs, ZrO2 once
    assert cache.get("oqmd", "formula:HfO2")[0]["found"] is True
    assert cache.get("pubchem", "formula:ZrO2")[0]["ghs_codes"] == ["H302"]
    # second call: nothing left to fetch
    assert layer.prefetch_formula_sources(["HfO2", "ZrO2"], workers=3) == {}


def test_prefetch_records_failures_without_raising():
    http = FakeHttp()  # no rules: every request raises SourceError
    layer, cache = make_layer(http)
    cache._conn.execute("DELETE FROM records WHERE source='oqmd' AND key='formula:HfO2'")
    cache._conn.commit()
    counts = layer.prefetch_formula_sources(["HfO2"], workers=2)
    assert counts["failed"] == 1 and counts["fetched"] == 0
    assert cache.get("oqmd", "formula:HfO2") is None  # no row invented on failure


def test_prefetch_is_a_noop_offline():
    http = FakeHttp()
    layer, _ = make_layer(http, offline=True)
    assert layer.prefetch_formula_sources(["HfO2"], workers=4) == {} and http.calls == []
