"""A request may name the substrate; the interface criterion follows it and the run says so."""

from __future__ import annotations

import pytest

from oxide_triage.cache import Cache
from oxide_triage.config import load_config, load_hazard_table
from oxide_triage.edges.parse import rule_parse
from oxide_triage.pipeline import load_fixtures, run_triage
from oxide_triage.schemas import Criteria, DataStatus
from oxide_triage.session import apply_changes
from tests.test_pipeline import PI

TABLE = load_hazard_table()


@pytest.fixture(scope="module")
def cache():
    c = Cache(":memory:")
    load_fixtures(load_config("default", use_env=False), c)
    return c


def _rows(res):
    return res.shortlist + res.ranked_beyond_shortlist + res.excluded


def _find(res, formula):
    return next(s for s in _rows(res) if s.record.formula == formula)


# ---- parsing ---------------------------------------------------------------------------------


def test_named_substrates_map_to_hull_phase_formulas():
    assert rule_parse("Oxides on germanium.", TABLE, substrate="Si").substrate == "Ge"
    assert rule_parse("Oxides on a sapphire substrate.", TABLE, substrate="Si").substrate == "Al2O3"
    assert rule_parse("Oxides on SrTiO3 wafers.", TABLE, substrate="Si").substrate == "SrTiO3"
    assert rule_parse("Oxides on silicon carbide.", TABLE, substrate="Si").substrate == "SiC"


def test_the_profiles_own_substrate_is_not_a_change():
    c = rule_parse("Oxides on silicon.", TABLE, substrate="Si")
    assert c.substrate is None and c.unhandled == []
    c = rule_parse("Oxides on sapphire.", TABLE, substrate="Al2O3")
    assert c.substrate is None and c.unhandled == []


def test_a_substrate_with_no_hull_phase_is_reported_not_applied():
    c = rule_parse("Find oxide dielectrics on glass.", TABLE, substrate="Si")
    assert c.substrate is None
    assert len(c.unhandled) == 1 and "glass" in c.unhandled[0] and "Si" in c.unhandled[0]


def test_criteria_only_accept_a_formula_of_real_elements():
    assert Criteria(substrate=" Ge ").substrate == "Ge"
    assert Criteria(substrate="SrTiO3").substrate == "SrTiO3"
    for bad in ("glass", "Ge; drop table", "Xx2O3", ""):
        with pytest.raises(ValueError):
            Criteria(substrate=bad)


# ---- the run ---------------------------------------------------------------------------------


def test_the_run_computes_the_interface_against_the_named_substrate(cache):
    cfg = load_config("default", use_env=False)
    res = run_triage(PI + " The films go on SrTiO3.", cfg, cache=cache, offline=True)
    dev = next(d for d in res.deviations if d.code == "request_substrate")
    assert "SrTiO3" in dev.description and "Si" in dev.description and dev.origin == "request"
    assert res.criteria.substrate == "SrTiO3"
    assert res.scoring.gates["substrate"] == "SrTiO3"
    assert res.not_acted_on == []
    assert {s.record.interface.substrate for s in _rows(res)} == {"SrTiO3"}
    # The fixture carries the Al-O-Sr-Ti hull (SrTiO3 is a phase in it): a real answer.
    al = _find(res, "Al2O3")
    assert al.record.interface.status is DataStatus.KNOWN
    assert al.record.interface.provenance.source_id == "Al-O-Sr-Ti"
    # No Hf-O-Sr-Ti hull offline: the gap is named on the candidate, not hidden or crashed on.
    hf = _find(res, "HfO2")
    assert hf.record.interface.status in (DataStatus.ABSENT, DataStatus.NOT_RETRIEVED)
    # The profile's own config hash is unchanged: the substrate is a deviation, not a new profile.
    assert res.config_hash == cfg.config_hash()


def test_silica_on_germanium_is_a_computed_answer_from_the_fixture_hull(cache):
    cfg = load_config("default", use_env=False)
    res = run_triage(PI + " Deposited on germanium.", cfg, cache=cache, offline=True)
    si = _find(res, "SiO2")
    assert si.record.interface.substrate == "Ge" and si.record.interface.status is DataStatus.KNOWN
    assert si.record.interface.reaction_energy_ev_atom is not None


def test_a_rerun_may_change_the_substrate(cache):
    cfg = load_config("default", use_env=False)
    first = run_triage(PI, cfg, cache=cache, offline=True)
    assert not any(d.code == "request_substrate" for d in first.deviations)
    criteria, notes = apply_changes(first.criteria, {"substrate": "Ge"})
    assert notes == ["rerun: substrate = 'Ge'"]
    res = run_triage(PI, cfg, cache=cache, offline=True, criteria=criteria)
    assert any(d.code == "request_substrate" for d in res.deviations)
    assert {s.record.interface.substrate for s in _rows(res)} == {"Ge"}
    with pytest.raises(ValueError):
        apply_changes(first.criteria, {"substrate": "glass"})
