"""Per-source worker counts and request-rate caps for the warm."""

from __future__ import annotations

from oxide_triage.cache import Cache
from oxide_triage.config import load_config
from oxide_triage.sources.assemble import DataLayer


def test_defaults_cap_oqmd_and_pubchem():
    cfg = load_config("default", use_env=False)
    c = cfg.candidates
    assert c.workers_for("oqmd") == 8 and c.max_rps_for("oqmd") == 1.0
    assert c.workers_for("pubchem") == 4 and c.max_rps_for("pubchem") == 4.0
    assert (
        c.workers_for("materials_project") == c.fetch_workers and c.max_rps_for("materials_project") is None
    )


def test_override_workers_only_keeps_default_rate():
    # profile/env overrides deep-merge with default.yaml: touching workers leaves max_rps alone
    cfg = load_config("default", use_env=False, overrides={"candidates": {"fetch": {"oqmd": {"workers": 1}}}})
    assert cfg.candidates.workers_for("oqmd") == 1 and cfg.candidates.max_rps_for("oqmd") == 1.0
    cfg = load_config(
        "default", use_env=False, overrides={"candidates": {"fetch": {"jarvis": {"workers": 2}}}}
    )
    assert cfg.candidates.workers_for("jarvis") == 2 and cfg.candidates.max_rps_for("jarvis") is None


def test_layer_builds_one_capped_client_per_source():
    cfg = load_config("default", use_env=False)
    layer = DataLayer.from_config(cfg, cache=Cache(":memory:"), offline=True)
    try:
        assert layer.oqmd.http.limiter is not None and layer.oqmd.http.limiter.interval == 1.0
        assert layer.pubchem.http.limiter is not None and layer.pubchem.http.limiter.interval == 0.25
        assert layer.mp.http.limiter is None
        assert len({id(x.http) for x in (layer.mp, layer.oqmd, layer.openalex, layer.pubchem)}) == 4
    finally:
        layer.close()


def test_zero_workers_leaves_source_to_sequential_path(monkeypatch):
    from tests.test_acquire import FakeHttp, make_layer

    http = FakeHttp()
    http.when("oqmd.org", None, {"data": []})
    http.when("pubchem", None, None)
    layer, cache = make_layer(http, overrides={"candidates": {"fetch": {"oqmd": {"workers": 0}}}})
    cache._conn.execute("DELETE FROM records WHERE source IN ('oqmd','pubchem')")
    cache._conn.commit()
    formulas = ["HfO2", "ZrO2"]
    counts = layer.prefetch_formula_sources(formulas, workers=4, sources={"oqmd", "pubchem"})
    assert counts["fetched"] + counts["failed"] == 2  # pubchem only; oqmd skipped by the pool
    assert not any("oqmd" in url for url, _ in http.calls)
