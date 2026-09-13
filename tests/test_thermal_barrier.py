"""The thermal-barrier profile: a second material class through the same engine, no code that
names it. Fixture data; every number is illustrative."""

from __future__ import annotations

import pytest

from oxide_triage.cache import Cache
from oxide_triage.config import load_cation_allowlist, load_cation_families, load_config, load_hazard_table
from oxide_triage.edges.parse import criterion_words, rule_parse
from oxide_triage.pipeline import load_fixtures, run_triage
from oxide_triage.schemas import DataStatus
from oxide_triage.selfcheck import read_selfcheck
from oxide_triage.sources.materials_project import MaterialsProject
from oxide_triage.sources.properties import MPElasticityProvider, make_provider

ALLOWLIST = "cation_allowlist_thermal_barrier.yaml"


@pytest.fixture(scope="module")
def cfg():
    return load_config("thermal-barrier", use_env=False)


@pytest.fixture(scope="module")
def cache(cfg):
    c = Cache(":memory:")
    load_fixtures(cfg, c)
    return c


@pytest.fixture(scope="module")
def result(cfg, cache):
    return run_triage(cfg.selfcheck.request, cfg, cache=cache, offline=True)


def test_profile_declares_a_different_figure_of_merit_substrate_and_universe(cfg):
    fom = cfg.figure_of_merit
    assert (fom.criterion, fom.provider, fom.prefer) == ("thermal_conductivity", "mp_elasticity", "low")
    assert cfg.criteria()[2] == "thermal_conductivity" and "dielectric" not in cfg.criteria()
    assert cfg.interface.substrate == "Al2O3"
    assert cfg.weights.band_gap == 0.0 and cfg.gates.min_band_gap_ev == 0.0
    assert cfg.candidates.cation_allowlist_file == ALLOWLIST
    assert cfg.refutation.polymorph_window_ev_atom == 0.02 and cfg.refutation.f_electron_gap_check


def test_families_cover_the_thermal_barrier_allowlist_exactly():
    fams = load_cation_families(ALLOWLIST)
    covered = [c for f in fams for c in f.cations]
    assert sorted(covered) == sorted(load_cation_allowlist(ALLOWLIST))
    assert len(covered) == len(set(covered))
    assert all(f.rationale for f in fams)
    assert "P" in covered and "Pb" not in covered


def test_elasticity_provider_rejects_flagged_records_as_absent_with_the_reason(cfg):
    cache = Cache(":memory:")
    mp = MaterialsProject(cache, 90, offline=True)
    provider = make_provider(cfg.figure_of_merit, mp)
    assert isinstance(provider, MPElasticityProvider)
    good = {
        "found": True,
        "thermal_conductivity": {"clarke": 1.43, "cahill": 1.59},
        "debye_temperature": 567,
        "bulk_modulus": {"vrh": 183},
        "shear_modulus": {"vrh": 90},
        "warnings": [],
    }
    ext = provider.extract(good)
    assert ext.value == 1.43 and ext.extras["cahill"] == 1.59 and ext.extras["debye_temperature"] == 567
    display, short = provider.describe(ext.value, ext.extras)
    assert display.startswith(
        "thermal_conductivity.clarke = 1.43 W/(m·K) (DFT elastic tensor, Clarke model; Cahill 1.59"
    )
    assert short == "minimum thermal conductivity (Clarke) 1.4 W/(m·K) (DFT elastic tensor, Clarke model)"
    bad = dict(good, warnings=["Fitting elastic tensor resulted in unphysical modulus"])
    rej = provider.extract(bad)
    assert rej.value is None and "unphysical" in (rej.reject_reason or "")
    assert (
        provider.extract({"found": False}).value is None
        and provider.extract({"found": False}).reject_reason is None
    )


def test_ranking_uses_the_inverted_curve_and_keeps_missing_data_semantics(result):
    ranked = result.shortlist + result.ranked_beyond_shortlist
    by = {s.record.formula: s for s in ranked}
    comp = lambda s: next(c for c in s.components if c.criterion == "thermal_conductivity")  # noqa: E731
    # Lower conductivity scores higher; alumina (a good conductor) scores 0.
    assert comp(by["Gd2O3"]).normalized == 1.0
    assert comp(by["Al2O3"]).normalized == 0.0
    assert comp(by["ZrO2"]).normalized == pytest.approx((2.5 - 1.43) / 1.5, abs=1e-3)
    # No elastic tensor at the source: ABSENT, unscored, caveated under the criterion's name.
    lz = by["La2Zr2O7"]
    assert lz.record.figure_of_merit.status is DataStatus.ABSENT
    assert comp(lz).normalized is None and "thermal_conductivity" in lz.missing_criteria
    assert any(c.code == "thermal_conductivity_unknown" for c in lz.caveats)
    # A record the source flags is ABSENT too, with the flag in the provenance note.
    gz = by["Gd2Zr2O7"]
    assert gz.record.figure_of_merit.status is DataStatus.ABSENT
    assert "unphysical" in (gz.record.figure_of_merit.provenance.note or "")
    assert "unphysical" in (gz.record.figure_of_merit.absent_note or "")
    # Nothing is scored on a band gap; the gate is off and the weight is zero.
    assert all(next(c for c in s.components if c.criterion == "band_gap").weight == 0 for s in ranked)
    assert not any(g.gate == "band_gap" and g.passed is False for s in result.excluded for g in s.gates)
    assert "dielectric" not in result.scoring.weights and result.scoring.figure_of_merit.units == "W/(m·K)"


def test_interface_is_judged_against_alumina(result):
    ranked = result.shortlist + result.ranked_beyond_shortlist
    y2o3 = next(s for s in ranked if s.record.formula == "Y2O3")
    assert y2o3.record.interface.substrate == "Al2O3"
    assert y2o3.record.interface.status is DataStatus.KNOWN
    assert y2o3.record.interface.reaction_energy_ev_atom < 0  # forms the garnet / perovskite aluminates
    zro2 = next(s for s in ranked if s.record.formula == "ZrO2")
    assert zro2.record.interface.reaction_energy_ev_atom >= -0.05  # zirconia and alumina do not react


def test_class_specific_refutation_rules_fire_only_here(result, cache):
    ranked = result.shortlist + result.ranked_beyond_shortlist
    zro2 = next(s for s in ranked if s.record.formula == "ZrO2")
    assert zro2.polymorphs and any(c.code == "polymorph_transformation_risk" for c in zro2.caveats)
    default = run_triage(
        "Find promising oxide dielectric candidates.",
        load_config("default", use_env=False),
        cache=cache,
        offline=True,
    )
    d_zro2 = next(
        s for s in default.shortlist + default.ranked_beyond_shortlist if s.record.formula == "ZrO2"
    )
    assert d_zro2.polymorphs and not any(c.code == "polymorph_transformation_risk" for c in d_zro2.caveats)


def test_self_check_passes_on_the_fixture_and_names_the_absent_workhorse(cfg, cache):
    check = read_selfcheck(cache, "thermal-barrier")
    assert check is not None and check.profile == "thermal-barrier"
    assert check.passed, check.details
    assert any(d.startswith("ZrO2:") for d in check.details)
    assert not any("exploratory" in d for d in check.details)
    # The default profile's verdict was also written for the profiles that share its universe.
    assert read_selfcheck(cache, "default").profile == "default"


def test_request_words_reach_the_parser_and_the_guard(cfg, result):
    assert result.guard.proceed and not result.guard.findings
    c = rule_parse(
        "Rank oxides for thermal barrier coatings; prioritise low thermal conductivity, top 8.",
        load_hazard_table(),
        words=criterion_words(cfg.figure_of_merit),
        vocabulary=cfg.figure_of_merit.vocabulary,
    )
    assert c.weight_overrides == {"thermal_conductivity": 0.4} and c.top_k == 8 and c.unhandled == []
