from __future__ import annotations

import pytest

from oxide_triage.config import (
    Config,
    deep_merge,
    list_profiles,
    load_cation_allowlist,
    load_config,
    load_hazard_table,
)


def test_default_loads_and_normalises_weights():
    cfg = load_config(use_env=False)
    assert isinstance(cfg, Config)
    assert sum(cfg.weights.normalized().values()) == pytest.approx(1.0)


def test_profiles_exist_and_differ():
    names = list_profiles()
    assert {"conservative", "exploratory", "ferroelectric-research"} <= set(names)
    hashes = {n: load_config(n, use_env=False).config_hash() for n in names}
    assert len(set(hashes.values())) == len(hashes)
    assert load_config(use_env=False).config_hash() not in hashes.values()


def test_profile_overrides_are_partial():
    base = load_config(use_env=False)
    cons = load_config("conservative", use_env=False)
    assert cons.gates.min_band_gap_ev > base.gates.min_band_gap_ev
    assert cons.band_gap.correction.strategy == base.band_gap.correction.strategy  # untouched


def test_unknown_profile_lists_available():
    with pytest.raises(FileNotFoundError, match="conservative"):
        load_config("nope", use_env=False)


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("OXIDE_TRIAGE_OFFLINE", "1")
    monkeypatch.setenv("OXIDE_TRIAGE_CACHE", "/tmp/x.sqlite")
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "http://vllm:8000/v1")
    cfg = load_config()
    assert cfg.cache.offline is True and cfg.cache.path == "/tmp/x.sqlite"
    assert cfg.llm.provider == "openai_compatible" and cfg.llm.base_url == "http://vllm:8000/v1"


def test_invalid_values_rejected():
    with pytest.raises(ValueError):
        load_config(use_env=False, overrides={"band_gap": {"correction": {"strategy": "magic"}}})
    with pytest.raises(ValueError):
        load_config(use_env=False, overrides={"dielectric": {"low": 50, "high": 10}})


def test_config_hash_ignores_cache_and_llm():
    a = load_config(use_env=False)
    b = load_config(use_env=False, overrides={"cache": {"offline": True}, "llm": {"provider": "anthropic"}})
    assert a.config_hash() == b.config_hash()
    c = load_config(use_env=False, overrides={"weights": {"stability": 0.9}})
    assert a.config_hash() != c.config_hash()


def test_deep_merge_does_not_mutate():
    base = {"a": {"b": 1, "c": 2}, "d": [1]}
    out = deep_merge(base, {"a": {"b": 9}})
    assert out == {"a": {"b": 9, "c": 2}, "d": [1]} and base["a"]["b"] == 1


def test_hazard_table_and_allowlist_consistent():
    table = load_hazard_table()
    cations = load_cation_allowlist()
    missing = [c for c in cations if c not in table.tiers]
    assert missing == [], f"cations without hazard entry: {missing}"
    assert table.lookup("Pb")[0] == 2 and table.lookup("Hf")[0] == 0
    assert table.lookup("Xx") == (table.default_tier, "not in hazard table", False)
