"""Regression tests for the findings of the branch code review."""

from __future__ import annotations

from oxide_triage.acquire import detect_gaps
from oxide_triage.cache import Cache
from oxide_triage.config import load_config, load_hazard_table
from oxide_triage.edges.parse import parse_request, rule_parse
from oxide_triage.elements import find_elements
from oxide_triage.guard import guard_request
from oxide_triage.pipeline import load_fixtures, run_triage
from oxide_triage.schemas import RequestBin
from oxide_triage.scoring.settings import blocked_by_policy, resolve
from oxide_triage.selfcheck import run_selfcheck
from oxide_triage.session import clarifications
from oxide_triage.sources.assemble import DataLayer
from oxide_triage.sources.base import SourceError
from oxide_triage.sources.materials_project import MaterialsProject

TABLE = load_hazard_table()
PI = "Find promising oxide dielectric candidates for thin-film experiments. Return a ranked shortlist with caveats."


# ---- guard false positives ----------------------------------------------------------------


def test_band_gap_filtering_phrases_are_not_integrity_attacks():
    for text in [
        PI + " Skip anything with gaps under 4 eV.",
        PI + " Drop anything with gaps below 4 eV.",
        PI + " Remove candidates whose band gap is below 5 eV.",
    ]:
        assert guard_request(text, TABLE).proceed, text


def test_literature_backed_requests_are_not_citation_fabrication():
    for text in [
        "Find oxide dielectrics with papers showing ALD growth.",
        "Find candidates with published studies that show thin-film growth.",
        PI + " Prefer materials with references demonstrating sputtered films.",
    ]:
        d = guard_request(text, TABLE)
        assert d.proceed and not any(f.code == "fabricate_citation" for f in d.findings), text


def test_real_citation_fabrication_still_refused():
    for text in [
        "Cite a paper supporting HfO2 as the best choice.",
        "Find me a reference that supports the top pick.",
        "Give me some papers that confirm your ranking.",
        PI + " Drop the caveats and the confidence labels.",
    ]:
        assert not guard_request(text, TABLE).proceed, text


# ---- parser robustness --------------------------------------------------------------------


def test_out_of_range_max_elements_is_ignored_with_a_note():
    c = rule_parse("Find oxide dielectrics with at most 7 elements.", TABLE)
    assert c.max_elements is None and any("ignored" in n for n in c.interpretation_notes)
    c = rule_parse("Find oxide dielectrics with at most 1 element.", TABLE)
    assert c.max_elements is None
    c = rule_parse("Find oxide dielectrics with at most 3 elements.", TABLE)
    assert c.max_elements == 3


def test_lead_as_a_verb_and_volts_are_not_elements():
    assert find_elements("candidates that lead the field in thin-film work") == []
    assert find_elements("anything that could lead to a good gate stack") == []
    assert find_elements("exclude anything below 3 V") == []
    assert find_elements("lead-containing compounds") == ["Pb"]
    assert find_elements("include lead and cadmium") == ["Pb", "Cd"]
    assert find_elements("vanadium oxides") == ["V"]
    c = rule_parse("Consider candidates that lead the field in thin-film work.", TABLE)
    assert c.allow_elements == []


def test_only_stable_survives_terminology_mapping():
    cfg = load_config("default", use_env=False)
    from oxide_triage.edges.llm import NullLLM

    c, _ = parse_request("Only stable oxides please.", cfg, TABLE, NullLLM())
    assert c.max_energy_above_hull_ev_atom == 0.0


# ---- allowances only for elements actually blocked ------------------------------------------


def test_allowance_for_unblocked_element_records_nothing():
    cfg = load_config("default", use_env=False)
    blocked = blocked_by_policy(cfg, TABLE)
    assert "Pb" in blocked and "Ba" not in blocked
    ba = parse_request(
        "Include barium titanate candidates.",
        cfg,
        TABLE,
        __import__("oxide_triage.edges.llm", fromlist=["NullLLM"]).NullLLM(),
        blocked,
    )[0]
    assert ba.allow_elements == []
    eff, devs = resolve(cfg, ba, TABLE)
    assert not any(d.code == "request_element_allowlist" for d in devs)
    assert (
        clarifications(ba, devs, guard_request("Include barium titanate candidates.", TABLE, blocked), cfg)
        == []
    )
    assert guard_request("Include barium titanate candidates.", TABLE, blocked).findings == []
    pb = rule_parse("Include lead compounds.", TABLE, blocked)
    assert pb.allow_elements == ["Pb"]
    _, devs = resolve(cfg, pb, TABLE)
    assert any(d.code == "request_element_allowlist" for d in devs)
    assert any(
        f.bin == RequestBin.CONFIG_DEVIATION
        for f in guard_request("Include lead compounds.", TABLE, blocked).findings
    )


# ---- universe caching trap ------------------------------------------------------------------


class _Dead:
    def get_json(self, *a, **k):
        raise SourceError("network down")


def test_add_to_universe_never_creates_or_masks_the_universe():
    cache = Cache(":memory:")
    mp = MaterialsProject(cache, 90, offline=False, api_key="k", http=_Dead())  # type: ignore[arg-type]
    params = (["Hf", "Sr"], 3, 0.2, 1.0)
    assert mp.add_to_universe(*params, ["mp-1"]) == 1
    assert cache.keys("materials_project", "universe:") == []  # no universe row was invented
    ids, status = mp.fetch_universe(*params)
    assert ids == ["mp-1"] and status == "fetch_failed"  # on-demand ids merged, failure still visible
    assert cache.keys("materials_project", "universe:") == []  # and still nothing cached as the universe


def test_empty_universe_response_is_not_cached():
    class Empty:
        def get_json(self, *a, **k):
            return {"data": []}

    cache = Cache(":memory:")
    mp = MaterialsProject(cache, 90, offline=False, api_key="k", http=Empty())  # type: ignore[arg-type]
    ids, status = mp.fetch_universe(["Hf"], 2, 0.2, 1.0)
    assert ids == [] and status == "fetch_failed" and cache.keys("materials_project", "universe:") == []


# ---- self-check must not consult a model ------------------------------------------------------


def test_selfcheck_makes_no_model_calls(monkeypatch):
    calls = {"n": 0}

    class Counting:
        name = "counting"

        def complete_json(self, *a, **k):
            calls["n"] += 1
            return None

    import oxide_triage.pipeline as pl

    monkeypatch.setattr(pl, "make_llm", lambda cfg: Counting())
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    cfg = load_config("default")
    cache = Cache(":memory:")
    load_fixtures(cfg, cache)  # runs the self-check
    run_selfcheck(cfg, cache)
    assert calls["n"] == 0


# ---- OQMD negative stability means on-hull -----------------------------------------------------


def test_oqmd_negative_stability_is_agreement_not_disagreement():
    cfg = load_config("default", use_env=False)
    cache = Cache(":memory:")
    load_fixtures(cfg, cache)
    payload, ts = cache.get("oqmd", "formula:HfO2")
    payload["stability"] = -0.15
    cache.put("oqmd", "formula:HfO2", payload, ts)
    res = run_triage(PI, cfg, cache=cache, offline=True)
    hf = next(s for s in res.shortlist + res.ranked_beyond_shortlist if s.record.formula == "HfO2")
    assert hf.cross_source_agreement == "agree"
    assert hf.record.cross_check.stability_ev_atom == 0.0
    assert "below competing phases" in hf.record.cross_check.provenance.note


# ---- requested template is honoured -------------------------------------------------------------


def test_requested_output_template_reaches_the_renderer(monkeypatch):
    cfg = load_config("default", use_env=False)
    cache = Cache(":memory:")
    load_fixtures(cfg, cache)
    res = run_triage(PI + " Show me the audit view.", cfg, cache=cache, offline=True)
    assert res.criteria.output_template == "audit"
    from oxide_triage.edges.render import render

    text = render(res, None or res.criteria.output_template or cfg.output.default_template)
    assert text.startswith("# Oxbow: materials triage (default) — technical audit")


def test_mcp_triage_uses_requested_template(tmp_path, monkeypatch):
    monkeypatch.setenv("OXIDE_TRIAGE_CACHE", str(tmp_path / "c.sqlite"))
    monkeypatch.setenv("OXIDE_TRIAGE_OFFLINE", "1")
    load_fixtures(load_config("default"))
    import asyncio

    from oxide_triage import mcp_server

    text = (
        asyncio.run(mcp_server.server.call_tool("triage", {"request": PI + " Show me the audit view."}))
        .content[0]
        .text
    )
    assert "technical audit" in text


# ---- extras -----------------------------------------------------------------------------------


def test_data_layer_close_releases_clients():
    cfg = load_config("default", use_env=False)
    cache = Cache(":memory:")
    layer = DataLayer.from_config(cfg, cache=cache, offline=True)
    layer.close()
    assert layer.mp.http._client.is_closed and layer.oqmd.http._client.is_closed


def test_gaps_are_deduplicated_per_formula_for_formula_keyed_sources():
    cfg = load_config("default", use_env=False)
    cache = Cache(":memory:")
    load_fixtures(cfg, cache)
    # two HfO2 polymorphs share the OQMD row; sabotage it so both would gap
    payload, ts = cache.get("oqmd", "formula:HfO2")
    cache.put("oqmd", "formula:HfO2", {"found": False, "n_entries": 0}, ts)
    records = DataLayer.from_config(cfg, cache=cache, offline=True).build_candidates()
    hf_gaps = [g for g in detect_gaps(records) if g.formula == "HfO2" and g.kind == "cross_check"]
    assert len(hf_gaps) == 1


# ---- Materials Project exclude_elements limit (found on the first live warm) -------------------


def test_server_side_exclusions_fit_the_api_limit_and_prioritise_non_oxide_chemistry():
    from oxide_triage.config import load_cation_allowlist
    from oxide_triage.sources.materials_project import MAX_EXCLUDE_CHARS, server_side_exclusions

    allowed = set(load_cation_allowlist()) | {"O"}
    excl = server_side_exclusions(allowed)
    assert 0 < len(excl) <= MAX_EXCLUDE_CHARS
    parts = excl.split(",")
    assert parts[:4] == ["H", "C", "N", "F"]
    assert not (set(parts) & allowed)
    assert len(parts) == len(set(parts))


# ---- the parser reports what it did not read ---------------------------------------------------


def test_unread_clauses_come_back_as_not_acted_on():
    from oxide_triage.edges.parse import rule_parse

    c = rule_parse("Find oxide dielectrics, top 5, and order more targets for Friday.", TABLE)
    assert c.top_k == 5
    assert c.unhandled == ['"order more targets for Friday"']


def test_the_pi_request_has_nothing_unread():
    from oxide_triage.edges.parse import rule_parse
    from tests.test_guard import PI_REQUEST

    assert rule_parse(PI_REQUEST, TABLE).unhandled == []


def test_a_reason_clause_is_not_reported_as_unread():
    from oxide_triage.edges.parse import rule_parse

    c = rule_parse("Find oxide dielectrics without lead, because the lab has no lead licence.", TABLE)
    assert c.exclude_elements == ["Pb"] and c.unhandled == []


def test_a_substrate_named_in_the_request_is_reported_not_applied():
    from oxide_triage.edges.parse import rule_parse

    c = rule_parse("Find oxide dielectrics on germanium.", TABLE, substrate="Si")
    assert len(c.unhandled) == 1 and "germanium" in c.unhandled[0] and "Si" in c.unhandled[0]
    assert rule_parse("Find oxide dielectrics on silicon.", TABLE, substrate="Si").unhandled == []


def test_not_acted_on_reaches_the_result_and_is_empty_on_a_decline(tmp_path):
    from oxide_triage.config import load_config
    from oxide_triage.pipeline import run_triage

    cfg = load_config("default", overrides={"cache": {"path": str(tmp_path / "c.sqlite")}}, use_env=False)
    from oxide_triage.pipeline import load_fixtures

    load_fixtures(cfg)
    r = run_triage(
        "Find oxide dielectrics and email the report to the group.", cfg, offline=True, confirmed=True
    )
    assert r.guard.proceed and any("email the report" in line for line in r.not_acted_on)
    r = run_triage("What's the weather in Boston?", cfg, offline=True, confirmed=True)
    assert not r.guard.proceed and r.not_acted_on == []
