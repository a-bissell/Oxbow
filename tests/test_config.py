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


# ---- site overrides ------------------------------------------------------------------------

import logging  # noqa: E402

import yaml  # noqa: E402

from oxide_triage.config import (  # noqa: E402
    DICT_PATHS,
    SiteOverrides,
    config_layers,
    diff_layer,
    is_policy_key,
    load_site_overrides,
    save_site_overrides,
    site_config_path,
)


def _site(tmp_path, data):
    path = tmp_path / "site.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_site_layers_merge_in_order(tmp_path):
    site = _site(
        tmp_path,
        {
            "base": {"gates": {"min_band_gap_ev": 4.5, "max_elements": 2}, "output": {"top_k": 7}},
            "profiles": {
                "conservative": {"gates": {"max_elements": 5}},
                "default": {"output": {"top_k": 9}},
            },
        },
    )
    default = load_config("default", use_env=False, site_config=site)
    cons = load_config("conservative", use_env=False, site_config=site)
    expl = load_config("exploratory", use_env=False, site_config=site)
    assert default.gates.min_band_gap_ev == 4.5 and default.output.top_k == 9  # site.profiles.default
    assert cons.gates.min_band_gap_ev == 5.0  # the profile file beats site.base
    assert cons.gates.max_elements == 5  # site.profiles.conservative beats the profile file
    assert cons.output.top_k == 7  # site.base reaches every profile
    assert expl.gates.max_elements == 4 and expl.output.top_k == 10  # profile values untouched


def test_site_discovery_rules(tmp_path, monkeypatch):
    site = _site(tmp_path, {"base": {"output": {"top_k": 3}}})
    assert load_config(use_env=False).output.top_k == 5  # pristine without an explicit path
    assert load_config(use_env=False, site_config=site).output.top_k == 3
    monkeypatch.setenv("OXIDE_TRIAGE_SITE_CONFIG", str(site))
    assert load_config().output.top_k == 3 and load_config().site_config_path == str(site)
    monkeypatch.setenv("OXIDE_TRIAGE_SITE_CONFIG", "off")
    assert load_config().output.top_k == 5 and load_config().site_config_path is None
    monkeypatch.delenv("OXIDE_TRIAGE_SITE_CONFIG")
    monkeypatch.setenv("OXIDE_TRIAGE_CACHE", str(tmp_path / "cache.sqlite"))
    assert site_config_path() == tmp_path / "site.yaml"
    assert load_config().output.top_k == 3  # discovered next to the cache
    monkeypatch.setenv("OXIDE_TRIAGE_CACHE", ":memory:")
    assert site_config_path() is None


def test_env_beats_site(tmp_path, monkeypatch):
    site = _site(tmp_path, {"base": {"cache": {"offline": True}, "llm": {"model": "site-model"}}})
    monkeypatch.setenv("OXIDE_TRIAGE_SITE_CONFIG", str(site))
    monkeypatch.setenv("OXIDE_TRIAGE_OFFLINE", "0")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    cfg = load_config()
    assert cfg.cache.offline is False and cfg.llm.model == "env-model"
    layers = config_layers()
    assert layers.origin_of("llm.model") == "env" and layers.origin_of("cache.offline") == "env"


def test_site_strips_reserved_keys_and_rejects_unknown(tmp_path, caplog):
    site = _site(
        tmp_path, {"base": {"profile_name": "hacked", "cache": {"path": "/elsewhere", "ttl_days": 7}}}
    )
    with caplog.at_level(logging.WARNING, logger="oxide_triage.config"):
        cfg = load_config(use_env=False, site_config=site)
    assert cfg.profile_name == "default" and cfg.cache.path == "data/cache.sqlite" and cfg.cache.ttl_days == 7
    assert "profile_name" in caplog.text and "cache.path" in caplog.text
    bad = _site(tmp_path, {"base": {"weights": {"stabilty": 1}}})
    with pytest.raises(ValueError, match="site.base: unknown configuration key 'weights.stabilty'"):
        load_config(use_env=False, site_config=bad)
    bad2 = _site(tmp_path, {"profiles": {"conservative": {"gates": {"min_gap": 1}}}})
    with pytest.raises(
        ValueError, match="site.profiles.conservative: unknown configuration key 'gates.min_gap'"
    ):
        load_config("conservative", use_env=False, site_config=bad2)


def test_site_dict_leaves_replace_but_profiles_merge(tmp_path):
    site = _site(
        tmp_path,
        {
            "base": {
                "terminology": {"hafnia": "HfO2"},
                "candidates": {"fetch": {"pubchem": {"workers": 2}}},
                "simplicity": {"scores": {2: 1.0, 3: 0.5}},
            }
        },
    )
    cfg = load_config(use_env=False, site_config=site)
    assert cfg.terminology == {"hafnia": "HfO2"}  # alias list replaced, others gone
    assert set(cfg.candidates.fetch) == {"pubchem"} and cfg.candidates.fetch["pubchem"].workers == 2
    assert cfg.simplicity.scores == {2: 1.0, 3: 0.5}
    # a profile file still deep-merges (exploratory sets band_gap.preference only)
    expl = load_config("exploratory", use_env=False, site_config=site)
    assert expl.band_gap.correction.scalar_factor == 1.4 and expl.band_gap.preference.ideal_ev == 5.0
    assert "candidates.fetch" in DICT_PATHS and "terminology" in DICT_PATHS


def test_site_round_trip_and_atomic_write(tmp_path):
    so = SiteOverrides(
        base={"simplicity": {"scores": {2: 1.0, 3: 0.4}}, "gates": {"min_band_gap_ev": 4.5}},
        profiles={"conservative": {"output": {"top_k": 3}}, "exploratory": {}},
    )
    path = tmp_path / "nested" / "site.yaml"
    save_site_overrides(path, so)
    text = path.read_text(encoding="utf-8")
    assert (
        text.startswith("# Site configuration overrides")
        and not (tmp_path / "nested" / "site.yaml.tmp").exists()
    )
    back = load_site_overrides(path)
    assert back.base == so.base and back.profiles == {"conservative": {"output": {"top_k": 3}}}
    assert list(back.base["simplicity"]["scores"]) == [2, 3]  # int keys survive YAML
    assert load_site_overrides(tmp_path / "missing.yaml").is_empty()


def test_site_overrides_recorded_and_hash_semantics(tmp_path):
    site = _site(
        tmp_path,
        {
            "base": {
                "weights": {"stability": 0.5},
                "cache": {"ttl_days": 1},
                "llm": {"model": "m"},
                "agent": {"max_tool_rounds": 2},
            }
        },
    )
    base = load_config(use_env=False)
    cfg = load_config(use_env=False, site_config=site)
    keys = {o.key: (o.shipped, o.value) for o in cfg.site_overrides}
    assert keys == {
        "weights.stability": (0.25, 0.5),
        "cache.ttl_days": (90, 1),
        "llm.model": (None, "m"),
        "agent.max_tool_rounds": (8, 2),
    }
    assert [o.key for o in cfg.policy_overrides()] == ["weights.stability"]
    assert cfg.config_hash() != base.config_hash()
    runtime_only = (
        _site(tmp_path / "r", {"base": {"cache": {"ttl_days": 1}, "llm": {"model": "m"}}})
        if (tmp_path / "r").mkdir() is None
        else None
    )
    assert load_config(use_env=False, site_config=runtime_only).config_hash() == base.config_hash()
    assert "site_overrides" not in cfg.model_dump() and "site_config_path" not in cfg.model_dump_json()


def test_diff_layer_and_origins(tmp_path):
    site = _site(
        tmp_path,
        {"base": {"gates": {"min_band_gap_ev": 4.5}}, "profiles": {"conservative": {"output": {"top_k": 3}}}},
    )
    layers = config_layers("conservative", use_env=False, site_config=site)
    assert layers.origin_of("gates.min_band_gap_ev") == "profile"  # conservative.yaml sets it
    assert layers.origin_of("gates.max_elements") == "profile"
    assert layers.origin_of("output.top_k") == "site.profile"
    assert layers.origin_of("dielectric.low") == "default"
    base_layers = config_layers("default", use_env=False, site_config=site)
    assert base_layers.origin_of("gates.min_band_gap_ev") == "site.base"
    reference = base_layers.reference_for("base")
    edited = base_layers.current_for("base")
    edited["gates"]["max_elements"] = 2
    edited["profile_name"] = "renamed"
    edited["terminology"] = {"hafnia": "HfO2"}
    diff = diff_layer(reference, edited)
    assert diff == {"gates": {"min_band_gap_ev": 4.5, "max_elements": 2}, "terminology": {"hafnia": "HfO2"}}


def test_is_policy_key():
    assert is_policy_key("weights.stability") and is_policy_key("gates.min_band_gap_ev")
    assert is_policy_key("candidates.min_reported_gap_ev") and is_policy_key("literature.fetch")
    assert not is_policy_key("candidates.fetch") and not is_policy_key("terminology")
    assert not is_policy_key("cache.ttl_days") and not is_policy_key("agent.max_tool_rounds")


def test_config_dir_resolves_from_env_then_cwd_then_checkout(tmp_path, monkeypatch):
    """An installed wheel has no config/ beside the package: the directory comes from the
    environment or the working directory, and a missing one is a clear error, not a traceback
    into site-packages."""
    import shutil

    from oxide_triage.config import DEFAULT_CONFIG_DIR, load_config, resolve_config_dir

    site = tmp_path / "release" / "config"
    shutil.copytree(DEFAULT_CONFIG_DIR, site)
    monkeypatch.delenv("OXIDE_TRIAGE_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_config_dir() == DEFAULT_CONFIG_DIR  # the checkout wins while it exists
    monkeypatch.setenv("OXIDE_TRIAGE_CONFIG_DIR", str(site))
    assert resolve_config_dir() == site
    assert load_config("conservative", config_dir=site, use_env=False).profile_name == "conservative"
    monkeypatch.chdir(tmp_path / "release")
    monkeypatch.delenv("OXIDE_TRIAGE_CONFIG_DIR")
    assert resolve_config_dir() == DEFAULT_CONFIG_DIR
    with pytest.raises(FileNotFoundError, match="OXIDE_TRIAGE_CONFIG_DIR"):
        load_config("default", config_dir=tmp_path / "nowhere", use_env=False)


def test_shipped_yaml_has_no_unknown_keys():
    """``Config`` ignores unknown keys, so a misspelt or renamed key in a shipped file would be
    dropped silently and the value would fall back to the default. Every leaf of every shipped
    file must be a known configuration path."""
    import yaml

    from oxide_triage.config import DEFAULT_CONFIG_DIR, LEAF_PATHS, flatten_leaves

    files = [DEFAULT_CONFIG_DIR / "default.yaml", *sorted((DEFAULT_CONFIG_DIR / "profiles").glob("*.yaml"))]
    assert files
    for path in files:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        unknown = sorted(p for p in flatten_leaves(data) if p not in LEAF_PATHS)
        assert not unknown, f"{path.name}: unknown configuration keys {unknown}"
