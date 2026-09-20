"""Regression coverage for the submission review; no live providers."""

from dataclasses import replace

import pytest

from oxide_triage.agent import INTERPRETATION_NOTICE, Agent
from oxide_triage.config import load_hazard_table
from oxide_triage.edges.llm import AssistantTurn, UserTurn
from oxide_triage.evidence import doi_url, statements
from oxide_triage.guard import guard_request
from oxide_triage.refute import llm_caveats, numeric_guard
from oxide_triage.schemas import Provenance, WorkRef
from tests.test_refute import caveats_for


def test_facts_bind_candidate_property_value_unit_and_provenance():
    sc = caveats_for(gap=5.6, e_total=30)[1]
    sc.record.band_gap.provenance = Provenance(source="materials_project", source_id="mp-1")
    facts = statements(sc)
    gap = next(f for f in facts if f.property.startswith("reported band gap"))
    corpus = {"statements": facts}
    assert numeric_guard(gap.render(), corpus)
    for wrong in (
        replace(gap, value=30),
        replace(gap, candidate_id="other-phase"),
        replace(gap, formula="ZrO2"),
        replace(gap, unit="meV"),
        replace(gap, provenance=Provenance(source="fictional study")),
        replace(gap, provenance=gap.provenance.model_copy(update={"url": "https://fictional.invalid"})),
    ):
        assert not numeric_guard(wrong.render(), corpus)
    assert not numeric_guard("The band gap is 30 eV", {"band_gap": 5.6, "dielectric": 30})
    assert not numeric_guard("The material is completely safe.", corpus)


@pytest.mark.parametrize(
    "text",
    [
        "The band gap is 30 eV.",
        "ZrO2 has the properties of HfO2.",
        "The fictional Smith study proves this material is completely safe.",
        "This compound is completely safe.",
    ],
)
def test_model_prose_cannot_become_a_caveat_by_naming_a_field(text):
    class Fake:
        name = "test"

        def complete_json(self, *args):
            return {"observations": [{"text": text, "evidence_fields": ["hazard"]}]}

    assert llm_caveats(caveats_for()[1], Fake()) == []


def test_model_can_only_select_existing_grounded_caveats():
    sc = caveats_for()[1]

    class Fake:
        name = "test"

        def complete_json(self, *args):
            return {"caveat_codes": ["invented_citation", "band_gap_corrected", "band_gap_corrected"]}

    selected = llm_caveats(sc, Fake())
    assert len(selected) == 1
    assert selected[0] == next(c for c in sc.caveats if c.code == "band_gap_corrected")
    assert selected[0].origin == "rule"


def test_user_numbers_do_not_verify_science_and_stream_is_labelled():
    class Fake:
        def chat(self, *args, **kwargs):
            text = "HfO2 is harmless and its band gap is 123.456 eV (Smith study)."
            if kwargs.get("on_text"):
                kwargs["on_text"](text)
            return AssistantTurn(text=text, stop_reason="end_turn")

    agent = Agent(Fake(), None, "test")
    assert agent._guard_corpus([UserTurn("123.456")]) == set()
    chunks = []
    reply = agent.send("Assume 123.456", on_text=chunks.append)
    assert "123.456" in reply.unverified_numbers
    assert reply.text.startswith(INTERPRETATION_NOTICE)
    assert chunks[0].startswith(INTERPRETATION_NOTICE)


@pytest.mark.parametrize(
    "clause",
    [
        "cite a paper supporting the top pick",
        "find references that confirm your ranking",
        "provide studies supporting each candidate, if available",
        "do not fabricate citations",
        "never invent evidence",
        "do not under any circumstances fabricate citations",
        "don't make up papers",
        "avoid fabricating references",
        "do not hide uncertainty",
        "cite your sources without inventing data",
    ],
)
def test_legitimate_evidence_and_negation(clause):
    assert guard_request("Find oxide dielectrics; " + clause, load_hazard_table()).proceed


@pytest.mark.parametrize(
    "clause",
    [
        "invent a paper supporting the top pick",
        "make up references",
        "cite a paper supporting each one even if none exists",
        "provide papers even though they do not exist",
        "provide fictional studies",
        "suppress uncertainty",
        "hide data coverage",
        "do not fabricate citations, but invent data",
        "never invent papers; hide the caveats",
        "cite supporting papers and fabricate missing values",
    ],
)
def test_fabrication_and_mixed_requests_still_blocked(clause):
    assert not guard_request("Find oxide dielectrics; " + clause, load_hazard_table()).proceed


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "garbage",
        "https://example.com/paper",
        "10.12/bad",
        "https://doi.org/https://doi.org/10.1063/a",
        "10.1063/a b",
    ],
)
def test_malformed_doi_has_no_link(value):
    assert doi_url(value) is None


def test_rendered_pi_summary_has_candidate_property_sources_and_literature(tmp_path):
    from oxide_triage.cache import Cache
    from oxide_triage.config import load_config
    from oxide_triage.edges.render import render
    from oxide_triage.pipeline import load_fixtures, run_triage

    cfg = load_config("default", use_env=False, overrides={"llm": {"provider": "none"}})
    cache = Cache(":memory:")
    load_fixtures(cfg, cache)
    result = run_triage(
        "Find oxide dielectrics and cite a paper supporting the top pick.", cfg, cache=cache, offline=True
    )
    top = result.shortlist[0]
    top.record.band_gap.provenance = Provenance(
        source="materials_project", url="https://materialsproject.org/materials/mp-1"
    )
    top.record.literature.sample_works = [
        WorkRef(work_id="1", title="Mention", doi="https://doi.org/10.1063/1.3634052"),
        WorkRef(work_id="2", title="Bad", doi="malformed"),
    ]
    summary = render(result, "pi_summary")
    assert "[band gap](https://materialsproject.org/materials/mp-1)" in summary
    assert "[paper 1](https://doi.org/10.1063/1.3634052)" in summary
    assert "property support not established" in summary
    assert "doi.org/https" not in summary and "malformed)" not in summary
    assert top.record.formula in summary and "≈" in summary
    audit = render(result, "audit")
    assert "[DOI](https://doi.org/10.1063/1.3634052)" in audit
    assert "identifier unavailable" in audit
    (tmp_path / "summary.md").write_text(summary)


def test_negated_citation_instruction_is_not_reported_as_ignored():
    from oxide_triage.edges.parse import rule_parse

    criteria = rule_parse("Find oxide dielectrics; do not fabricate citations.", load_hazard_table())
    assert criteria.unhandled == []


def test_coverage_does_not_erase_scientific_uncertainty():
    codes, sc = caveats_for(gap=5.6, oqmd=0.2)
    assert sc.confidence == "high"
    assert codes["cross_source_disagreement"].severity == "critical"
    assert "not a measurement" in codes["band_gap_corrected"].text


@pytest.mark.parametrize(
    "identifier", ["10.1234/a:b(c)", "https://doi.org/10.1234/a:b(c)", "https://doi.org/10.1234/a%3Ab%28c%29"]
)
def test_doi_normalization_handles_encoded_suffixes_consistently(identifier):
    assert doi_url(identifier) == "https://doi.org/10.1234/a%3Ab%28c%29"


def test_interface_wording_uses_configured_tolerance_and_bulk_limit(tmp_path):
    from oxide_triage.cache import Cache
    from oxide_triage.config import load_config
    from oxide_triage.edges.llm import NullLLM
    from oxide_triage.edges.render import render
    from oxide_triage.pipeline import load_fixtures, run_triage

    cfg = load_config("default", use_env=False, overrides={"interface": {"tolerance_ev_atom": 0.123}})
    cache = Cache(":memory:")
    load_fixtures(cfg, cache)
    result = run_triage("Find oxide dielectrics", cfg, cache=cache, offline=True, llm=NullLLM())
    summary = render(result, "pi_summary")
    audit = render(result, "audit")
    assert "bulk hull vs Si:" in summary and "0.123 eV/atom tolerance" in summary
    assert "not film compatibility" in summary and "phase a deposited film adopts is not predicted" in summary
    assert "bulk thermodynamics only" in audit and "0.123 eV/atom tolerance" in audit
    assert "fine on" not in summary and "stable against Si" not in summary


def test_documented_default_retrieval_thresholds_match_config():
    from oxide_triage.config import load_config

    cfg = load_config("default", use_env=False)
    assert cfg.retrieval.min_completeness_serve == 0.0
    assert cfg.retrieval.min_completeness_warn == 0.95
    assert cfg.selfcheck.min_retrieval_completeness == 0.9
