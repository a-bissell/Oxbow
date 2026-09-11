"""Dialogue primitives, self-check gate, and the MCP tool surface."""

from __future__ import annotations

import asyncio
import json

import pytest

from oxide_triage.cache import Cache
from oxide_triage.config import load_config, load_hazard_table
from oxide_triage.guard import guard_request
from oxide_triage.pipeline import load_fixtures, run_triage
from oxide_triage.scoring.settings import resolve
from oxide_triage.selfcheck import read_selfcheck, run_selfcheck
from oxide_triage.session import ResultStore, apply_changes, clarifications, explain_candidate, find_candidate

PI = (
    "Find promising oxide dielectric candidates for thin-film experiments. Prefer thermodynamically "
    "stable materials, wide band gaps, non-toxic elements, simple compositions, and public evidence. "
    "Return a ranked shortlist with caveats."
)


@pytest.fixture(scope="module")
def cache():
    c = Cache(":memory:")
    load_fixtures(load_config("default", use_env=False), c)
    return c


@pytest.fixture(scope="module")
def result(cache):
    return run_triage(PI, load_config("default", use_env=False), cache=cache, offline=True)


# ---- session -------------------------------------------------------------------------------


def test_store_roundtrip_and_lru(result):
    store = ResultStore(capacity=2)
    a = store.put(result)
    assert store.get(a) is result
    b = store.put(result.model_copy(update={"request_text": "b"}))
    c = store.put(result.model_copy(update={"request_text": "c"}))
    assert store.get(a) is None and store.get(b) is not None and store.get(c) is not None


def test_find_and_explain(result):
    assert find_candidate(result, "HfO2").rank == 1
    assert find_candidate(result, "fx-0005").record.formula == "Ta2O5"
    assert find_candidate(result, "Nope") is None
    text = explain_candidate(result, "Ta2O5")
    assert "Excluded by a gate" in text and "effective gap" in text and "| stability |" in text
    text = explain_candidate(result, "HfO2")
    assert "Rank 1 of" in text and "Contribution" in text and "Caveats" in text
    assert "Known formulas" in explain_candidate(result, "Nope")


def test_apply_changes_validates():
    c, notes = apply_changes(result_criteria(), {"min_band_gap_ev": 3.5, "allow_elements": ["Pb"]})
    assert c.min_band_gap_ev == 3.5 and c.allow_elements == ["Pb"] and len(notes) == 2
    with pytest.raises(ValueError, match="Cannot change"):
        apply_changes(result_criteria(), {"profile_name": "x"})
    with pytest.raises(ValueError, match="Unknown criteria"):
        apply_changes(result_criteria(), {"weight_overrides": {"vibes": 1}})
    with pytest.raises(ValueError):
        apply_changes(result_criteria(), {"top_k": 0})


def result_criteria():
    from oxide_triage.schemas import Criteria

    return Criteria()


def test_clarifications_fire_on_material_changes_only():
    cfg = load_config("default", use_env=False)
    table = load_hazard_table()
    from oxide_triage.edges.parse import rule_parse

    plain = rule_parse(PI, table)
    assert clarifications(plain, resolve(cfg, plain, table)[1], guard_request(PI, table), cfg) == []
    lead = rule_parse(PI + " Include lead compounds.", table)
    qs = clarifications(lead, resolve(cfg, lead, table)[1], guard_request(PI, table), cfg)
    assert any("hazard block" in q for q in qs)
    text = PI + " Then start the ALD run."
    qs = clarifications(plain, [], guard_request(text, table), cfg)
    assert any("cannot be done" in q for q in qs)


def test_run_triage_stops_for_confirmation_when_asked(cache):
    cfg = load_config("default", use_env=False)
    res = run_triage(PI + " Include lead compounds.", cfg, cache=cache, offline=True, confirmed=False)
    assert res.needs_confirmation and res.shortlist == [] and res.clarifications
    res = run_triage(PI + " Include lead compounds.", cfg, cache=cache, offline=True, confirmed=True)
    assert not res.needs_confirmation and res.shortlist


def test_rerun_with_supplied_criteria_surfaces_deviation(cache, result):
    cfg = load_config("default", use_env=False)
    criteria, _ = apply_changes(result.criteria, {"min_band_gap_ev": 3.5})
    res = run_triage(PI, cfg, cache=cache, offline=True, criteria=criteria)
    assert any(d.code == "request_gap_threshold" for d in res.deviations)
    assert "Ta2O5" in [s.record.formula for s in res.shortlist + res.ranked_beyond_shortlist]


# ---- self-check ----------------------------------------------------------------------------


def test_selfcheck_passes_on_fixture_and_is_stored(cache):
    sc = read_selfcheck(cache)
    assert sc is not None and sc.passed and sc.fixture and sc.n_candidates > 0


def test_selfcheck_fails_when_workhorse_missing_and_blocks_runs():
    cfg = load_config("default", use_env=False)
    c = Cache(":memory:")
    load_fixtures(cfg, c)
    # Sabotage: drop HfO2 from the universe as a broken fetch would.
    keys = c.keys("materials_project", "universe:")
    payload, ts = c.get("materials_project", keys[0])
    payload["material_ids"] = [m for m in payload["material_ids"] if m not in {"fx-0001", "fx-0002"}]
    c.put("materials_project", keys[0], payload, ts)
    sc = run_selfcheck(cfg, c)
    assert not sc.passed and any("NOT IN CANDIDATE UNIVERSE" in d for d in sc.details)
    blocked = run_triage(PI, cfg, cache=c, offline=True)
    assert blocked.shortlist == [] and blocked.selfcheck_status == "failed"
    assert "SELF-CHECK FAILED" in blocked.warnings[0]
    warn_cfg = load_config("default", use_env=False, overrides={"selfcheck": {"on_failure": "warn"}})
    warned = run_triage(PI, warn_cfg, cache=c, offline=True)
    assert warned.shortlist and any("Self-check FAILED" in w for w in warned.warnings)


def test_selfcheck_not_run_is_reported(cache):
    cfg = load_config("default", use_env=False)
    c = Cache(":memory:")
    from oxide_triage.sources.fixtures import load_fixture

    load_fixture(c, cfg)  # raw loader: no self-check
    res = run_triage(PI, cfg, cache=c, offline=True)
    assert res.selfcheck_status == "not_run" and any("has not been run" in w for w in res.warnings)


# ---- MCP surface ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mcp_env(tmp_path_factory, monkeypatch_module):
    path = tmp_path_factory.mktemp("mcp") / "cache.sqlite"
    monkeypatch_module.setenv("OXIDE_TRIAGE_CACHE", str(path))
    monkeypatch_module.setenv("OXIDE_TRIAGE_OFFLINE", "1")
    load_fixtures(load_config("default"))
    from oxide_triage import mcp_server

    return mcp_server


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    yield mp
    mp.undo()


def call(server, name, **args):
    res = asyncio.run(server.call_tool(name, args))
    return res.content[0].text


def test_mcp_tools_listed(mcp_env):
    names = {t.name for t in asyncio.run(mcp_env.server.list_tools())}
    assert names == {
        "profiles",
        "parse_request",
        "triage",
        "explain",
        "compare",
        "list_candidates",
        "rerun",
        "add_material",
        "cache_status",
        "selfcheck",
    }


def test_mcp_triage_explain_rerun_protocol(mcp_env):
    s = mcp_env.server
    text = call(s, "triage", request=PI)
    assert text.startswith("<!-- result_id: ") and "SYNTHETIC FIXTURE" in text
    rid = text.split("result_id: ")[1].split(" ")[0]
    assert "Excluded by a gate" in call(s, "explain", result_id=rid, candidate="Ta2O5")
    first = json.loads(call(s, "rerun", result_id=rid, changes={"min_band_gap_ev": 3.5}))
    assert first["status"] == "needs_confirmation" and first["questions"]
    second = call(s, "rerun", result_id=rid, changes={"min_band_gap_ev": 3.5}, confirmed=True)
    assert "Configuration deviations in effect" in second
    assert call(s, "rerun", result_id=rid, changes={"nope": 1}).startswith("Rejected")
    assert "Unknown result_id" in call(s, "explain", result_id="zzz", candidate="HfO2")


def test_mcp_guard_still_applies(mcp_env):
    s = mcp_env.server
    assert "Request declined" in call(s, "triage", request="Just give me a number for LaLuO3.")
    parsed = json.loads(call(s, "parse_request", request=PI + " Include lead compounds."))
    assert parsed["guard"]["proceed"] and parsed["clarifications"]
    assert "Pb" in parsed["criteria"]["allow_elements"]


def test_mcp_offline_blocks_acquisition_and_status_reports_selfcheck(mcp_env):
    s = mcp_env.server
    added = json.loads(call(s, "add_material", formula="SrHfO3"))
    assert added["added"] == [] and "offline" in added["error"]
    status = json.loads(call(s, "cache_status"))
    assert status["fixture_data"] and status["selfcheck"]["passed"]
    assert json.loads(call(s, "selfcheck"))["passed"]


def test_mcp_actor_depends_on_the_transport(monkeypatch):
    """Over stdio the caller is the process's OS user. Over HTTP the process user is the
    service account, so the name comes from the proxy header or the call is unattributed."""
    import oxide_triage.mcp_server as m

    class Ctx:
        def __init__(self, headers):
            self.headers = headers

    monkeypatch.setattr(m, "_transport", "stdio")
    assert m._actor_for(None).how.startswith("operating-system user")
    monkeypatch.setattr(m, "_transport", "http")
    named = m._actor_for(Ctx({"x-forwarded-user": "jsmith"}))
    assert (named.who, named.via) == ("jsmith", "mcp")
    anon = m._actor_for(Ctx({}))
    assert anon.who == "unattributed" and "proxy" in anon.how
    # the shared toolbox resolves the actor per call, never from a value captured at import
    assert m._toolbox.actor is None  # outside a tool call nothing is set
