"""The Admin page: schema-driven widgets, the data_editor converters, and the page in read-only
and admin mode through Streamlit's test harness."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")

from oxide_triage.config import (  # noqa: E402
    Config,
    SourceFetchConfig,
    leaf_fields,
    load_config,
)
from oxide_triage.pipeline import load_fixtures  # noqa: E402
from oxide_triage.ui.admin_form import (  # noqa: E402
    all_specs,
    classify,
    clean_cell,
    parse_csv_list,
    records_to_int_map,
    records_to_models,
    records_to_str_map,
    sections,
)
from oxide_triage.ui.admin_page import RANKING_SECTIONS, RUNTIME_SECTIONS  # noqa: E402


def test_every_config_leaf_has_a_widget():
    specs = {s.path: s for s in all_specs()}
    assert set(specs) == set(leaf_fields(Config))
    assert all(s.kind for s in specs.values())
    assert (
        specs["gates.max_elements"].kind == "int"
        and specs["gates.max_elements"].ge == 2
        and specs["gates.max_elements"].le == 6
    )
    assert (
        specs["band_gap.correction.strategy"].kind == "literal"
        and "hse_preferred" in specs["band_gap.correction.strategy"].options
    )
    assert specs["llm.model"].kind == "optional_str" and specs["cache.path"].kind == "readonly"
    assert (
        specs["toxicity.tier_scores"].kind == "int_map"
        and specs["candidates.fetch"].model is SourceFetchConfig
    )
    assert (
        specs["missing_data.confidence_thresholds"].kind == "str_float_map"
        and specs["terminology"].kind == "str_map"
    )


def test_tabs_partition_the_sections():
    on_page = set(RANKING_SECTIONS) | set(RUNTIME_SECTIONS)
    assert on_page == set(sections()) - {"profile_name"}
    assert not set(RANKING_SECTIONS) & set(RUNTIME_SECTIONS)


def test_unknown_annotation_is_loud():
    from pydantic import BaseModel

    class Odd(BaseModel):
        x: tuple[int, int]

    with pytest.raises(TypeError, match="no widget"):
        classify("x", Odd.model_fields["x"])


def test_converters_undo_the_data_editor_round_trip():
    assert (
        clean_cell(float("nan")) is None
        and clean_cell(8.0) == 8
        and clean_cell(8.5) == 8.5
        and clean_cell("a") == "a"
    )
    assert records_to_int_map(
        [{"key": 2.0, "value": 1.0}, {"key": float("nan"), "value": 0.5}, {"key": 3, "value": 0.6}]
    ) == {2: 1.0, 3: 0.6}
    assert records_to_str_map(
        [{"key": " hafnia ", "value": "HfO2"}, {"key": None, "value": "x"}, {"key": "empty", "value": ""}]
    ) == {"hafnia": "HfO2"}
    assert records_to_str_map([{"key": "high", "value": 0.9}], float) == {"high": 0.9}
    rows = [
        {"key": "oqmd", "workers": 8.0, "max_rps": 1.0},
        {"key": "pubchem", "workers": float("nan"), "max_rps": 4.0},
        {"key": "", "workers": 1, "max_rps": 1},
    ]
    assert records_to_models(rows, SourceFetchConfig) == {
        "oqmd": {"workers": 8, "max_rps": 1.0},
        "pubchem": {"max_rps": 4.0},
    }
    with pytest.raises(ValueError):
        records_to_models([{"key": "oqmd", "workers": 8.5, "max_rps": 1.0}], SourceFetchConfig)
    assert parse_csv_list(" HSE06, PBE0 ,,", str) == ["HSE06", "PBE0"] and parse_csv_list("1, 2", int) == [
        1,
        2,
    ]
    assert math.isnan(float("nan"))  # sanity for the reader: nan is what st.data_editor returns for blanks


# ---- the page through AppTest ---------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    cache = tmp_path / "cache.sqlite"
    site = tmp_path / "site.yaml"
    monkeypatch.setenv("OXIDE_TRIAGE_CACHE", str(cache))
    monkeypatch.setenv("OXIDE_TRIAGE_SITE_CONFIG", str(site))
    monkeypatch.setenv("OXIDE_TRIAGE_OFFLINE", "1")
    monkeypatch.delenv("OXIDE_TRIAGE_ADMIN", raising=False)
    load_fixtures(load_config("default"))
    return {"cache": cache, "site": site}


def _page():
    from oxide_triage.config import load_config
    from oxide_triage.ui.admin_page import admin_page
    from oxide_triage.ui.sidebar import SidebarState

    cfg = load_config("default")
    admin_page(SidebarState(profile="default", config=cfg, template="pi_summary", offline=True))


def _run_page(timeout=120):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_page, default_timeout=timeout)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def test_admin_page_read_only_without_flag(env):
    at = _run_page()
    assert any("Read-only" in i.value for i in at.info)
    assert not [b for b in at.button if b.key == "admin:save"]
    weight = next(n for n in at.number_input if n.key and n.key.endswith(":weights.stability"))
    assert weight.disabled and weight.value == 0.25


def test_admin_page_saves_a_site_override(env, monkeypatch):
    monkeypatch.setenv("OXIDE_TRIAGE_ADMIN", "1")
    at = _run_page()
    assert not any("Read-only" in i.value for i in at.info)
    weight = next(n for n in at.number_input if n.key and n.key.endswith(":weights.stability"))
    assert not weight.disabled
    weight.set_value(0.5).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    save = next(b for b in at.button if b.key == "admin:save")
    assert not save.disabled
    assert any("Ranking hash changes" in w.value for w in at.warning)
    save.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    written = yaml.safe_load(Path(env["site"]).read_text())
    assert written["base"] == {"weights": {"stability": 0.5}} and written.get("profiles", {}) == {}
    cfg = load_config("default")
    assert cfg.weights.stability == 0.5 and [o.key for o in cfg.site_overrides] == ["weights.stability"]
    # env-locked fields are disabled while the variable is set
    provider = next(s for s in at.selectbox if s.key and s.key.endswith(":llm.provider"))
    assert not provider.disabled
    monkeypatch.setenv("LLM_PROVIDER", "none")
    at2 = _run_page()
    provider = next(s for s in at2.selectbox if s.key and s.key.endswith(":llm.provider"))
    assert provider.disabled and "LLM_PROVIDER" in (provider.help or "")


def test_admin_page_refuses_stale_save(env, monkeypatch):
    monkeypatch.setenv("OXIDE_TRIAGE_ADMIN", "1")
    at = _run_page()
    weight = next(n for n in at.number_input if n.key and n.key.endswith(":weights.stability"))
    weight.set_value(0.4).run()
    Path(env["site"]).write_text(
        yaml.safe_dump({"base": {"output": {"top_k": 3}}}), encoding="utf-8"
    )  # someone else saved
    next(b for b in at.button if b.key == "admin:save").click().run()
    assert any("changed on disk" in e.value for e in at.error)
    assert yaml.safe_load(Path(env["site"]).read_text())["base"] == {"output": {"top_k": 3}}  # untouched


def test_cli_config_and_doctor_show_the_site_file(env, monkeypatch):
    from typer.testing import CliRunner

    from oxide_triage.cli import app

    Path(env["site"]).write_text(
        yaml.safe_dump({"base": {"gates": {"min_band_gap_ev": 4.5}}}), encoding="utf-8"
    )
    runner = CliRunner()
    out = runner.invoke(app, ["config", "--changed-only"])
    assert out.exit_code == 0 and "gates.min_band_gap_ev" in out.output and "site.base" in out.output
    js = runner.invoke(app, ["config", "--json"])
    assert js.exit_code == 0
    import json

    data = json.loads(js.output)
    assert data["site_file"] == str(env["site"])
    assert {"key": "gates.min_band_gap_ev", "value": 4.5, "origin": "site.base"} in data["values"]
    doc = runner.invoke(app, ["doctor"])
    assert doc.exit_code == 0 and "site file:" in doc.output and "1 override(s)" in doc.output
