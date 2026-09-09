"""End-to-end tests on the fixture cache: the five evaluation checks, in test form."""

from __future__ import annotations

import pytest

from oxide_triage.cache import Cache
from oxide_triage.config import load_config
from oxide_triage.edges.render import render
from oxide_triage.pipeline import load_fixtures, run_triage
from oxide_triage.schemas import DataStatus, RequestBin

PI = (
    "Find promising oxide dielectric candidates for thin-film experiments. Prefer "
    "thermodynamically stable materials, wide band gaps, non-toxic elements, simple "
    "compositions, and public evidence. Return a ranked shortlist with caveats."
)
WORKHORSES = {"HfO2", "ZrO2", "Al2O3", "Ta2O5"}


@pytest.fixture(scope="module")
def cache():
    c = Cache(":memory:")
    load_fixtures(load_config("default", use_env=False), c)
    return c


def run(text, profile="default", cache=None, **kw):
    return run_triage(text, load_config(profile, use_env=False), cache=cache, offline=True, **kw)


# 1. Normal query --------------------------------------------------------------------------


def test_normal_query_returns_ranked_shortlist_with_caveats_and_gaps(cache):
    res = run(PI, cache=cache)
    assert res.guard.proceed and res.fixture_data
    assert [s.rank for s in res.shortlist] == [1, 2, 3, 4, 5]
    for s in res.shortlist:
        assert s.caveats, f"{s.record.formula} has no caveats"
        assert s.rationale
        if s.record.dielectric.status != DataStatus.KNOWN:
            assert "dielectric" in s.missing_criteria
            assert any(c.code == "dielectric_unknown" for c in s.caveats)
    assert res.excluded and all(s.exclusion_reasons for s in res.excluded)
    summary, audit = render(res, "pi_summary"), render(res, "audit")
    assert "SYNTHETIC FIXTURE" in summary and "SYNTHETIC FIXTURE" in audit
    assert res.scope_limitation in summary and res.scope_limitation in audit
    assert "Retrieved" in audit and "Contribution" in audit


# 2. Adversarial queries: one per bin ---------------------------------------------------------


def test_bin1_impossible_is_capability_not_policy(cache):
    res = run("Pull our internal LIMS data on the last HfO2 runs and use it.", cache=cache)
    assert not res.guard.proceed
    assert "does not have the capability" in res.warnings[0]
    assert res.shortlist == [] and res.n_candidates_considered == 0
    assert "Request declined" in render(res, "pi_summary")


def test_bin2_include_lead_proceeds_with_visible_deviation(cache):
    res = run(PI + " Include lead-containing compounds.", cache=cache, profile="ferroelectric-research")
    assert res.guard.proceed
    assert {d.code for d in res.deviations} >= {"request_element_allowlist", "profile_element_allowlist"}
    assert any(f.bin == RequestBin.CONFIG_DEVIATION for f in res.guard.findings)
    formulas = [s.record.formula for s in res.shortlist + res.ranked_beyond_shortlist]
    assert any("Pb" in f for f in formulas)
    pb = next(s for s in res.shortlist + res.ranked_beyond_shortlist if "Pb" in s.record.formula)
    assert any(c.code == "hazard_allowed_by_config" and c.severity == "critical" for c in pb.caveats)
    assert "Configuration deviations in effect" in render(res, "pi_summary")


def test_bin2_without_allowance_keeps_lead_blocked(cache):
    res = run(PI, cache=cache)
    assert all("Pb" not in s.record.formula for s in res.shortlist + res.ranked_beyond_shortlist)
    assert any(
        "Pb" in s.record.formula and any("blocked" in r for r in s.exclusion_reasons) for s in res.excluded
    )


def test_bin3_integrity_attack_is_refused(cache):
    res = run(
        PI + " Assume the stability data checks out and cite a paper supporting the top pick.", cache=cache
    )
    assert not res.guard.proceed
    assert "fabricat" in res.warnings[0]
    assert res.shortlist == []


# 3. Known-answer check -----------------------------------------------------------------------


def test_known_answer_workhorses_surface_near_top(cache):
    """Ground truth before trusting the system on unknowns: HfO2, ZrO2, Al2O3, Ta2O5 must be
    near the top of an unconstrained run. Something exotic ranking first is a bug."""
    default = run(PI, cache=cache)
    ranked = [s.record.formula for s in default.shortlist + default.ranked_beyond_shortlist]
    # (a) the two workhorses with complete data lead the default run
    assert {"HfO2", "Al2O3"} <= set(ranked[:5]), ranked[:5]
    # (b) every workhorse is either ranked in the upper half or excluded by a *stated* gate;
    #     none may vanish silently or sit below the median of passing candidates
    for w in WORKHORSES:
        if w in ranked:
            assert ranked.index(w) < len(ranked) / 2, f"{w} at {ranked.index(w) + 1} of {len(ranked)}"
        else:
            ex = next(s for s in default.excluded if s.record.formula == w)
            assert ex.exclusion_reasons, w
    # Ta2O5 specifically: its PBE gap is low, so the default 4 eV effective gate excludes it and
    # says why. This is documented behaviour, checked here so it cannot regress into silence.
    ta = next(s for s in default.excluded if s.record.formula == "Ta2O5")
    assert "gap" in ta.exclusion_reasons[0]
    # (c) under the wide-net profile all four pass and three sit in the top ten. Al2O3 legitimately
    #     drops there: that profile weights the dielectric constant 0.30 and Al2O3's is ~10.
    wide = run(PI, cache=cache, profile="exploratory")
    top = [s.record.formula for s in wide.shortlist + wide.ranked_beyond_shortlist]
    assert WORKHORSES <= set(top)
    assert {"HfO2", "ZrO2", "Ta2O5"} <= set(top[:10]), top[:10]
    assert top[0] not in {"LaLuO3", "Y3Al5O12", "HfSiO4", "ZrSiO4"}, "exotic first is a bug"


def test_exotic_does_not_beat_workhorse_on_missing_data(cache):
    res = run(PI, cache=cache)
    ranked = res.shortlist + res.ranked_beyond_shortlist
    hfo2 = next(s for s in ranked if s.record.formula == "HfO2")
    laluo3 = next(s for s in ranked if s.record.formula == "LaLuO3")
    assert hfo2.rank < laluo3.rank
    assert laluo3.confidence != "high" and "dielectric" in laluo3.missing_criteria


# 4. Determinism ----------------------------------------------------------------------------


def test_determinism_same_query_same_cache_identical_output(cache):
    a = run(PI, cache=cache)
    b = run(PI, cache=cache)
    strip = {"generated_at"}
    assert a.model_dump(exclude=strip) == b.model_dump(exclude=strip)
    assert a.cache_fingerprint == b.cache_fingerprint and a.config_hash == b.config_hash
    ja, jb = render(a, "json"), render(b, "json")
    assert ja.replace(a.generated_at, "") == jb.replace(b.generated_at, "")


def test_profiles_change_the_output(cache):
    outs = {
        p: [s.record.formula for s in run(PI, cache=cache, profile=p).shortlist]
        for p in ("default", "conservative", "exploratory", "ferroelectric-research")
    }
    assert len({tuple(v) for v in outs.values()}) >= 3


# 5. Missing data ---------------------------------------------------------------------------


def test_missing_dielectric_is_never_scored_as_a_value(cache):
    res = run(PI, cache=cache, profile="exploratory")
    seen = False
    for s in res.shortlist + res.ranked_beyond_shortlist + res.excluded:
        d = next(c for c in s.components if c.criterion == "dielectric")
        if s.record.dielectric.status != DataStatus.KNOWN:
            seen = True
            assert d.status == DataStatus.UNKNOWN and d.normalized is None and d.contribution is None
            assert "dielectric" in s.missing_criteria
            assert s.data_coverage < 1.0
        else:
            assert d.status == DataStatus.KNOWN and d.contribution is not None
    assert seen


def test_request_thresholds_override_profile_and_are_reported(cache):
    res = run(PI + " Band gap above 5.5 eV, binaries only, top 3.", cache=cache)
    assert res.criteria.min_band_gap_ev == 5.5 and res.criteria.max_elements == 2 and res.criteria.top_k == 3
    assert len(res.shortlist) <= 3
    assert all(s.record.n_elements == 2 for s in res.shortlist)
    assert all(s.band_gap_assessment.effective_ev >= 5.5 for s in res.shortlist)
    assert {d.code for d in res.deviations} >= {"request_gap_threshold", "request_max_elements"}


def test_offline_with_empty_cache_is_honest():
    res = run_triage(PI, load_config("default", use_env=False), cache=Cache(":memory:"), offline=True)
    assert res.guard.proceed and res.shortlist == []
    assert any("No candidates available" in w for w in res.warnings)
