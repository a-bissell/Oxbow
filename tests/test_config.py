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


def test_dotenv_is_loaded_without_overriding(tmp_path, monkeypatch):
    from oxide_triage.config import load_dotenv

    env = tmp_path / ".env"
    env.write_text(
        "# comment\nMP_API_KEY=abc123\nexport OPENALEX_MAILTO='me@example.org'\nEMPTY=\nLLM_PROVIDER=\"anthropic\"\n"
        "not a line\n",
        encoding="utf-8",
    )
    for k in ("MP_API_KEY", "OPENALEX_MAILTO", "EMPTY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "none")  # already set: must win over the file
    loaded = load_dotenv([env])
    import os

    assert set(loaded) == {"MP_API_KEY", "OPENALEX_MAILTO"}
    assert os.environ["MP_API_KEY"] == "abc123" and os.environ["OPENALEX_MAILTO"] == "me@example.org"
    assert os.environ["LLM_PROVIDER"] == "none" and "EMPTY" not in os.environ


def test_load_config_reads_dotenv_from_cwd(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OXIDE_TRIAGE_OFFLINE=1\nMP_API_KEY=fromfile\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for k in ("OXIDE_TRIAGE_OFFLINE", "MP_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    import os

    assert cfg.cache.offline is True and os.environ["MP_API_KEY"] == "fromfile"
