"""The self-check judges the active profile and stores one verdict per profile."""

from __future__ import annotations

from oxide_triage.cache import Cache
from oxide_triage.config import load_config
from oxide_triage.pipeline import load_fixtures, run_triage
from oxide_triage.selfcheck import META_KEY, SelfCheck, meta_key, read_selfcheck, run_selfcheck


def test_verdicts_are_stored_per_profile_and_the_default_keeps_the_legacy_key():
    cfg = load_config("default", use_env=False)
    cache = Cache(":memory:")
    load_fixtures(cfg, cache)  # runs the default check
    default = read_selfcheck(cache, "default")
    assert default is not None and default.profile == "default" and default.passed
    assert cache.get_meta(META_KEY) and cache.get_meta(meta_key("default"))
    cons = run_selfcheck(load_config("conservative", use_env=False), cache, offline=True)
    assert cons.profile == "conservative" and cache.get_meta(meta_key("conservative"))
    assert read_selfcheck(cache, "conservative").profile == "conservative"
    assert read_selfcheck(cache, "default").profile == "default"
    # A profile that has never been checked falls back to the default's verdict.
    assert read_selfcheck(cache, "exploratory").profile == "default"


def test_a_legacy_cache_with_only_the_old_key_is_still_read():
    cache = Cache(":memory:")
    old = SelfCheck(passed=True, checked_at="2026-01-01T00:00:00+00:00", fixture=True, n_candidates=3)
    cache.set_meta(META_KEY, old.model_dump_json())
    assert read_selfcheck(cache, "conservative").passed


def test_the_check_runs_the_profiles_own_request_and_names_an_absent_figure_of_merit():
    cfg = load_config(
        "default",
        use_env=False,
        overrides={
            "selfcheck": {
                "request": "Find promising oxide candidates. Return a ranked shortlist.",
                "workhorses": ["HfO2", "LaLuO3"],  # LaLuO3 has no dielectric record in the fixture
                "leaders": ["HfO2"],
                "wide_profile": None,
            },
            "figure_of_merit": {"weight": 0.6},
        },
    )
    cache = Cache(":memory:")
    load_fixtures(cfg, cache)
    check = run_selfcheck(cfg, cache, offline=True)
    res = run_triage(cfg.selfcheck.request, cfg, cache=cache, offline=True, skip_selfcheck=True)
    lalu = next(s for s in res.shortlist + res.ranked_beyond_shortlist if s.record.formula == "LaLuO3")
    assert lalu.record.figure_of_merit.status.value == "absent"
    line = next(d for d in check.details if d.startswith("LaLuO3"))
    if "below median" in line:
        assert "explained: no public dielectric constant record" in line
    assert not any(d.endswith("exploratory") for d in check.details)
    assert check.passed, check.details


def test_gate_reads_the_active_profiles_verdict():
    from oxide_triage.pipeline import _selfcheck_gate

    cfg = load_config("default", use_env=False)
    cache = Cache(":memory:")
    load_fixtures(cfg, cache)
    bad = read_selfcheck(cache, "default").model_copy(update={"passed": False, "profile": "conservative"})
    cache.set_meta(meta_key("conservative"), bad.model_dump_json())
    assert _selfcheck_gate(cfg, cache)[0] == "passed"
    assert _selfcheck_gate(load_config("conservative", use_env=False), cache)[0] == "failed"
