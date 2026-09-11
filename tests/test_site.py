"""Site overrides end to end: the deviation on results, the self-check judging the site policy,
and the deviations log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from oxide_triage.cache import Cache
from oxide_triage.config import load_config, load_hazard_table
from oxide_triage.guard import guard_request
from oxide_triage.pipeline import load_fixtures, run_triage
from oxide_triage.schemas import Criteria
from oxide_triage.scoring.settings import resolve
from oxide_triage.selfcheck import run_selfcheck
from oxide_triage.session import clarifications

PI = (
    "Find promising oxide dielectric candidates for thin-film experiments. Prefer thermodynamically "
    "stable materials, wide band gaps, non-toxic elements, simple compositions, and public evidence. "
    "Return a ranked shortlist with caveats."
)


def _site(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "site.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def cache_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("site") / "cache.sqlite"
    load_fixtures(load_config("default", use_env=False, overrides={"cache": {"path": str(path)}}))
    return path


def test_resolve_emits_one_site_deviation_for_policy_keys_only(tmp_path):
    table = load_hazard_table()
    site = _site(
        tmp_path,
        {
            "base": {
                "weights": {"stability": 0.5},
                "gates": {"min_band_gap_ev": 4.5},
                "cache": {"ttl_days": 1},
            },
            "profiles": {"conservative": {"weights": {"band_gap": 0.1}}},
        },
    )
    _, devs = resolve(load_config("default", use_env=False, site_config=site), Criteria(), table)
    site_devs = [d for d in devs if d.origin == "site"]
    assert len(site_devs) == 1 and site_devs[0].code == "site_override"
    text = site_devs[0].description
    assert "profile 'default'" in text
    assert "weights.stability 0.25 -> 0.5" in text and "gates.min_band_gap_ev 4 -> 4.5" in text
    assert "ttl_days" not in text  # runtime keys never make a banner
    cfg_cons = load_config("conservative", use_env=False, site_config=site)
    _, devs_cons = resolve(cfg_cons, Criteria(), table)
    text_cons = next(d for d in devs_cons if d.origin == "site").description
    # conservative.yaml sets its own weights and gap gate, so the base edits lose to it; only the
    # profile-scoped edit is in force.
    assert "weights.band_gap 0.25 -> 0.1" in text_cons
    assert "weights.stability" not in text_cons and "min_band_gap_ev" not in text_cons
    assert clarifications(Criteria(), devs, guard_request(PI, table), cfg_cons) == []
    (tmp_path / "r").mkdir()
    runtime = _site(tmp_path / "r", {"base": {"cache": {"ttl_days": 1}, "agent": {"number_guard": "off"}}})
    _, devs2 = resolve(load_config(use_env=False, site_config=runtime), Criteria(), table)
    assert not [d for d in devs2 if d.origin == "site"]


def test_selfcheck_judges_the_site_policy(tmp_path, cache_path):
    cache = Cache(str(cache_path))
    try:
        pristine = load_config("default", use_env=False, overrides={"cache": {"path": str(cache_path)}})
        assert run_selfcheck(pristine, cache).passed
        # A site edit to the exploratory profile that excludes everything. The check's second run
        # reloads that profile internally, so this only fails if the site file is propagated.
        broken = _site(tmp_path, {"profiles": {"exploratory": {"gates": {"min_band_gap_ev": 9.0}}}})
        cfg = load_config(
            "default", use_env=False, overrides={"cache": {"path": str(cache_path)}}, site_config=broken
        )
        assert cfg.site_config_path == str(broken)
        result = run_selfcheck(cfg, cache)
        assert not result.passed and any("FAIL" in d for d in result.details)
        # and afterwards the pristine check passes again (it does not read the site file)
        assert run_selfcheck(pristine, cache).passed
    finally:
        cache.close()


def test_run_logs_site_deviation(tmp_path, cache_path):
    site = _site(tmp_path, {"base": {"gates": {"min_band_gap_ev": 4.5}}})
    cfg = load_config(
        "default", use_env=False, overrides={"cache": {"path": str(cache_path)}}, site_config=site
    )
    cache = Cache(str(cache_path))
    try:
        res = run_triage(PI, cfg, cache=cache, offline=True)
    finally:
        cache.close()
    assert any(d.origin == "site" for d in res.deviations)
    log = cache_path.with_name("deviations.jsonl")
    entries = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert any(
        d["origin"] == "site" and d["code"] == "site_override" for e in entries for d in e["deviations"]
    )
    pristine = load_config("default", use_env=False, overrides={"cache": {"path": str(cache_path)}})
    assert pristine.config_hash() != cfg.config_hash() and res.config_hash == cfg.config_hash()


# ---- the deviation log names who asked ---------------------------------------------------------


def test_deviation_log_records_the_actor(tmp_path):
    import json

    from oxide_triage.actor import Actor
    from oxide_triage.config import load_config
    from oxide_triage.pipeline import load_fixtures, run_triage

    cfg = load_config("default", overrides={"cache": {"path": str(tmp_path / "c.sqlite")}}, use_env=False)
    load_fixtures(cfg)
    who = Actor(who="jsmith", via="web", how="X-Forwarded-User header set by the reverse proxy")
    run_triage(
        "Find oxide dielectrics. Include lead compounds.", cfg, offline=True, confirmed=True, actor=who
    )
    run_triage("Find oxide dielectrics. Include lead compounds.", cfg, offline=True, confirmed=True)
    rows = [json.loads(line) for line in (tmp_path / "deviations.jsonl").read_text().splitlines()]
    assert rows[0]["actor"] == who.model_dump()
    assert rows[1]["actor"]["who"] == "unattributed"  # a caller that passes nothing is still recorded


def test_web_actor_comes_from_the_proxy_header_only():
    from oxide_triage.actor import web_actor

    a = web_actor({"x-forwarded-user": "jsmith"}, "X-Forwarded-User")
    assert a.who == "jsmith" and a.via == "web"
    b = web_actor({}, "X-Forwarded-User")
    assert b.who == "unattributed" and "no authenticating proxy" in b.how


def test_never_lift_outranks_a_profile_allowlist():
    """A profile or site file that allowlists an element on never_lift does not admit it."""
    from oxide_triage.config import load_config, load_hazard_table
    from oxide_triage.schemas import Criteria
    from oxide_triage.scoring.settings import resolve

    cfg = load_config(
        "default",
        overrides={"toxicity": {"element_allowlist": ["Pb", "U"], "never_lift": ["U", "Pu"]}},
        use_env=False,
    )
    eff, devs = resolve(cfg, Criteria(allow_elements=["Pu"]), load_hazard_table())
    assert "U" in eff.blocked_elements and "Pu" in eff.blocked_elements
    assert "Pb" not in eff.blocked_elements  # the ordinary allowance still works
    assert "U" not in eff.allowed_despite_tier and "Pu" not in eff.allowed_despite_tier
    codes = {d.code for d in devs}
    assert "allowlist_never_lift" in codes
    assert not any("U" in d.description for d in devs if d.code == "profile_element_allowlist")
