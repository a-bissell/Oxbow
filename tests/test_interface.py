"""The interface criterion: hull arithmetic, the record, the score, the caveat."""

from __future__ import annotations

import json

import pytest

from oxide_triage.cache import Cache
from oxide_triage.config import CRITERIA, load_config
from oxide_triage.pipeline import load_fixtures, run_triage
from oxide_triage.schemas import DataStatus
from oxide_triage.scoring.hull import hull_energy, interface_reaction, parse_formula
from oxide_triage.sources.fixtures import FIXTURE_PATH
from tests.test_pipeline import PI

# A toy Hf-O-Si hull in eV/atom (elements at 0). Numbers are close to MP's.
HF_O_SI = {"HfO2": -4.02, "HfSiO4": -3.66, "SiO2": -3.27, "HfSi": -0.86, "Hf2Si": -0.79, "Hf3Si2": -0.84}
# Ta-O-Si: Ta2O5 with Si wants to form TaSi2 + SiO2.
TA_O_SI = {"Ta2O5": -2.54, "TaSi2": -0.45, "Ta5Si3": -0.50, "SiO2": -3.27}


def test_parse_formula():
    assert parse_formula("HfO2") == {"Hf": 1, "O": 2}
    assert parse_formula("La2Zr2O7") == {"La": 2, "Zr": 2, "O": 7}
    assert parse_formula("Y3Al5O12") == {"Y": 3, "Al": 5, "O": 12}


def test_hull_energy_of_a_hull_phase_is_its_own_energy():
    import numpy as np

    phases = {**HF_O_SI, "Hf": 0.0, "O": 0.0, "Si": 0.0}
    elements = ["Hf", "O", "Si"]
    e, weights = hull_energy(phases, elements, np.array([1 / 3, 2 / 3, 0.0]))
    assert e == pytest.approx(-4.02, abs=1e-6) and set(weights) == {"HfO2"}


def test_stable_pair_has_zero_reaction_energy_and_no_products():
    out = interface_reaction("HfO2", HF_O_SI, "Si")
    assert out is not None
    assert out.reaction_energy_ev_atom == 0.0 and out.products == [] and out.x_substrate is None


def test_reactive_pair_reports_energy_products_and_composition():
    out = interface_reaction("Ta2O5", TA_O_SI, "Si")
    assert out is not None
    assert out.reaction_energy_ev_atom < -0.1
    assert {"TaSi2", "SiO2"} & set(out.products)
    assert out.x_substrate is not None and 0.05 <= out.x_substrate <= 0.95


def test_reference_is_the_hull_at_the_oxide_composition_not_the_candidate():
    # A metastable polymorph's excess above the hull is the stability criterion's business:
    # the answer must not depend on the candidate's own energy at all.
    assert interface_reaction("HfO2", HF_O_SI, "Si") == interface_reaction("HfO2", HF_O_SI, "Si", steps=19)
    out = interface_reaction("SiO2", {"SiO2": -3.27}, "Si")
    assert out is not None and out.reaction_energy_ev_atom == 0.0  # SiO2 cannot react with Si


def test_compound_substrate_must_be_a_hull_phase():
    assert interface_reaction("HfO2", HF_O_SI, "SrTiO3") is None
    phases = {**HF_O_SI, "SrTiO3": -3.5, "TiO2": -3.3, "SrO": -3.0}
    assert interface_reaction("HfO2", phases, "SrTiO3") is not None


def test_memoised_answer_does_not_depend_on_dict_order():
    a = interface_reaction("Ta2O5", TA_O_SI, "Si")
    b = interface_reaction("Ta2O5", dict(reversed(list(TA_O_SI.items()))), "Si")
    assert a == b


# ---- through the pipeline on the fixture ---------------------------------------------------


@pytest.fixture(scope="module")
def cache():
    cfg = load_config("default", use_env=False)
    c = Cache(":memory:")
    load_fixtures(cfg, c)
    return c


def test_fixture_carries_real_hull_data_and_the_textbook_pattern(cache):
    data = json.loads(FIXTURE_PATH.read_text())
    assert "thermo" in data and "Hf-O-Si" in data["thermo"]
    cfg = load_config("default", use_env=False)
    res = run_triage(PI, cfg, cache=cache, offline=True)
    rows = {
        s.record.formula: s
        for s in res.shortlist + res.ranked_beyond_shortlist + res.excluded + res.collapsed_polymorphs
    }
    for stable in ("HfO2", "Al2O3", "Y2O3", "MgO", "LaAlO3", "SrHfO3"):
        r = rows[stable].record.interface
        assert r.status is DataStatus.KNOWN and r.reaction_energy_ev_atom == 0.0, stable
    for reactive in ("Ta2O5", "SrTiO3", "BaTiO3", "Bi2O3"):
        r = rows[reactive].record.interface
        assert r.status is DataStatus.KNOWN and r.reaction_energy_ev_atom < -0.1 and r.products, reactive
    # the criterion is scored, weighted, and the missing-data policy sees it like any other
    assert "interface" in CRITERIA
    hf = rows["HfO2"]
    comp = next(c for c in hf.components if c.criterion == "interface")
    assert comp.normalized == 1.0 and comp.weight > 0 and "stable against Si" in comp.raw_label
    ta = rows["Ta2O5"]
    comp = next(c for c in ta.components if c.criterion == "interface")
    assert comp.normalized == 0.0 and "reacts with Si" in comp.raw_label
    # and the caveat names the products, critical when the reaction is past the zero point
    cav = next(c for c in ta.caveats if c.code == "substrate_reaction")
    assert cav.severity == "critical" and "TaSi2" in cav.text or "Ta5Si3" in cav.text
    assert not any(c.code == "substrate_reaction" for c in hf.caveats)
    # the rationale line says so in words
    assert "stable against Si" in (hf.rationale or "")


def test_substrate_is_a_config_knob_and_the_weight_can_be_zeroed(cache):
    cfg = load_config("default", use_env=False, overrides={"weights": {"interface": 0.0}})
    res = run_triage(PI, cfg, cache=cache, offline=True)
    hf = next(s for s in res.shortlist + res.ranked_beyond_shortlist if s.record.formula == "HfO2")
    comp = next(c for c in hf.components if c.criterion == "interface")
    assert comp.weight == 0.0 and comp.contribution == 0.0
    # a substrate that is not a hull phase in the system: honest ABSENT, not a crash
    cfg = load_config("default", use_env=False, overrides={"interface": {"substrate": "Ge"}})
    res = run_triage(PI, cfg, cache=cache, offline=True)
    hf = next(
        s for s in res.shortlist + res.ranked_beyond_shortlist + res.excluded if s.record.formula == "HfO2"
    )
    assert hf.record.interface.status in (DataStatus.ABSENT, DataStatus.NOT_RETRIEVED)


def test_selfcheck_accepts_a_workhorse_that_reacts_with_the_substrate(cache):
    """Ta2O5 reacts with Si, so on a Si-substrate triage it may sit below the median; the check
    names the reason instead of failing, and still fails on an unexplained drop."""
    from oxide_triage.selfcheck import run_selfcheck

    cfg = load_config("default", use_env=False)
    chk = run_selfcheck(cfg, cache, offline=True)
    assert chk.passed, chk.details
    # Force Ta2O5 into the ranking (the fixture excludes it on the gap gate) with a wide net and
    # a heavy interface weight so it sinks below the median for the stated reason.
    cfg = load_config(
        "default",
        use_env=False,
        overrides={"gates": {"min_band_gap_ev": 3.0}, "weights": {"interface": 0.6}},
    )
    chk = run_selfcheck(cfg, cache, offline=True)
    ta = next(d for d in chk.details if d.startswith("Ta2O5"))
    assert "explained: reacts with Si" in ta or "excluded by gate" in ta or "below median" not in ta, ta
    assert chk.passed, chk.details
