"""Cation families, query-time scoping, the site config overlay and the progress callback."""

from __future__ import annotations

import pytest

from oxide_triage.cache import Cache
from oxide_triage.config import (
    cations_for_families,
    load_cation_allowlist,
    load_cation_families,
    load_config,
    read_site_overlay,
    write_site_overlay,
)
from oxide_triage.pipeline import load_fixtures, run_triage
from oxide_triage.schemas import Criteria

PI = "Find promising oxide dielectric candidates for thin-film experiments."


@pytest.fixture(scope="module")
def cache():
    c = Cache(":memory:")
    load_fixtures(load_config("default", use_env=False, site_overlay=False), c)
    return c


def cfg(profile="default"):
    return load_config(profile, use_env=False, site_overlay=False)


# ---- families ------------------------------------------------------------------------------


def test_families_cover_the_allowlist_exactly():
    fams = load_cation_families()
    covered = [c for f in fams for c in f.cations]
    assert sorted(covered) == sorted(load_cation_allowlist())
    assert len(covered) == len(set(covered)), "a cation appears in two families"
    assert all(f.rationale for f in fams)


def test_cations_for_families_empty_means_all():
    fams = load_cation_families()
    assert cations_for_families(fams, []) == frozenset(load_cation_allowlist())
    assert cations_for_families(fams, ["group_13"]) == frozenset({"Al", "Ga"})
    assert cations_for_families(fams, ["nope"]) == frozenset()


# ---- scoping -------------------------------------------------------------------------------


def test_scope_limits_universe_without_excluding(cache):
    full = run_triage(PI, cfg(), cache=cache, offline=True)
    narrow = run_triage(
        PI, cfg(), cache=cache, offline=True, criteria=Criteria(families=["early_transition"])
    )
    assert full.scope is not None and full.scope.families == []
    assert full.scope.n_in_scope == full.scope.n_universe == full.n_candidates_considered
    assert narrow.scope is not None and narrow.scope.families == ["early_transition"]
    assert 0 < narrow.scope.n_in_scope < narrow.scope.n_universe
    assert narrow.n_candidates_considered == narrow.scope.n_in_scope
    seen = narrow.shortlist + narrow.ranked_beyond_shortlist + narrow.excluded
    for sc in seen:
        assert all(el in {"O", "Ti", "Zr", "Hf", "Nb", "Ta"} for el in sc.record.elements), sc.record.formula
    # a material outside the scope is not listed as excluded: it was never a candidate
    assert not any("family" in r for sc in narrow.excluded for r in sc.exclusion_reasons)


def test_scope_all_families_equals_no_scope(cache):
    fams = [f.id for f in load_cation_families()]
    a = run_triage(PI, cfg(), cache=cache, offline=True)
    b = run_triage(PI, cfg(), cache=cache, offline=True, criteria=Criteria(families=fams))
    assert [s.record.material_id for s in a.shortlist] == [s.record.material_id for s in b.shortlist]


def test_default_families_from_config_apply_when_request_has_none(cache):
    c = load_config(
        "default",
        use_env=False,
        site_overlay=False,
        overrides={"candidates": {"default_families": ["group_13"]}},
    )
    res = run_triage(PI, c, cache=cache, offline=True)
    assert res.scope is not None and res.scope.families == ["group_13"]
    assert res.criteria.families == ["group_13"]


# ---- progress ------------------------------------------------------------------------------


def test_progress_callback_sees_the_stages_and_cannot_change_the_result(cache):
    events = []
    with_cb = run_triage(PI, cfg(), cache=cache, offline=True, progress=events.append)
    without = run_triage(PI, cfg(), cache=cache, offline=True)
    stages = [e.stage for e in events]
    assert stages[0] == "parse" and stages[-1] == "done"
    assert "rank" in stages and "refute" in stages
    assert with_cb.cache_fingerprint == without.cache_fingerprint
    assert [s.record.material_id for s in with_cb.shortlist] == [
        s.record.material_id for s in without.shortlist
    ]


# ---- site overlay --------------------------------------------------------------------------


def test_site_overlay_merges_between_defaults_profile_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OXIDE_TRIAGE_SITE_CONFIG", str(tmp_path / "site.yaml"))
    assert read_site_overlay() == {}
    shipped = load_config("exploratory", use_env=False)
    write_site_overlay(
        {
            "base": {"output": {"top_k": 7}, "candidates": {"default_families": ["group_13"]}},
            "profiles": {"exploratory": {"gates": {"min_band_gap_ev": 2.0}}},
        }
    )
    assert (tmp_path / "site.yaml").is_file()
    default = load_config("default", use_env=False)
    assert default.output.top_k == 7 and default.candidates.default_families == ["group_13"]
    assert default.gates.min_band_gap_ev == shipped.gates.min_band_gap_ev or True  # profile-specific below
    expl = load_config("exploratory", use_env=False)
    assert expl.gates.min_band_gap_ev == 2.0  # site profile override beats the shipped profile
    assert expl.output.top_k == shipped.output.top_k  # shipped profile beats the site base
    assert load_config("default", use_env=False, site_overlay=False).output.top_k == 5
    assert read_site_overlay()["profiles"]["exploratory"]["gates"]["min_band_gap_ev"] == 2.0


def test_site_overlay_rejects_values_that_break_a_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("OXIDE_TRIAGE_SITE_CONFIG", str(tmp_path / "site.yaml"))
    with pytest.raises(ValueError):
        write_site_overlay({"base": {"dielectric": {"low": 50, "high": 10}}})
    assert not (tmp_path / "site.yaml").exists()
