"""Retrospective benchmark of the interface criterion against Hubbard & Schlom (1996).

The fixture carries the real MP hull phases for every system on the list, so this runs offline
and the numbers are the live ones."""

from __future__ import annotations

import pytest

from oxide_triage.cache import Cache
from oxide_triage.config import load_config
from oxide_triage.pipeline import load_fixtures
from oxide_triage.validation import DEFAULT_SET, load_set, render_markdown, validate_interface


@pytest.fixture(scope="module")
def cache():
    cfg = load_config("default", use_env=False)
    c = Cache(":memory:")
    load_fixtures(cfg, c)
    return c


def test_set_is_cited_and_every_entry_is_tagged():
    data = load_set(DEFAULT_SET)
    assert "10.1557/JMR.1996.0350" in data["citation"]
    for group in ("proven_stable", "not_shown_unstable", "unstable", "borderline", "secondary"):
        for e in data[group]:
            assert e["formula"] and e["confidence"] in {"confirmed", "unsure", "reported"}, (group, e)
    assert {e["formula"] for e in data["proven_stable"]} >= {"BeO", "MgO", "ZrO2"}
    assert {e["formula"] for e in data["unstable"]} >= {"TiO2", "Ta2O5"}


def test_hard_assertions_all_agree_and_disagreements_are_named(cache):
    cfg = load_config("default", use_env=False)
    report = validate_interface(cfg, cache, offline=True)
    assert not report.missing, report.missing  # every system's hull is in the fixture
    assert report.hard_total == 21 and report.hard_agree == 21
    assert report.passed
    by = {r.formula: r for r in report.rows}
    # the textbook pattern, per oxide
    assert by["HfO2"].predicted == "stable" and by["Al2O3"].predicted == "stable"
    assert by["ZrO2"].predicted == "marginal" and by["ZrO2"].agrees  # marginal counts as not-reacting
    assert by["TiO2"].predicted == "reacts" and by["Ta2O5"].predicted == "reacts"
    assert "TaSi2" in by["Ta2O5"].products or "Ta5Si3" in by["Ta2O5"].products
    # the soft assertion the hull disagrees with is named, and does not fail the check
    assert by["SrO"].agrees is False and by["SrO"].predicted == "reacts"
    assert report.soft_total == 11 and report.soft_agree == 10
    # the secondary "believed stable" perovskites: two agree, two are named disagreements
    assert by["LaAlO3"].agrees and by["GdScO3"].agrees
    assert by["CaZrO3"].agrees is False and by["SrZrO3"].agrees is False
    assert by["SrTiO3"].agrees and by["BaTiO3"].agrees
    names = {r.formula for r in report.disagreements}
    assert names == {"SrO", "CaZrO3", "SrZrO3"}
    text = render_markdown(report)
    assert text.startswith("Retrospective benchmark:") and "Not an independent validation" in text
    assert "Held-out" not in text
    assert "21 of 21 agree" in text and "10 of 11 agree" in text and "Disagreements, named:" in text
    assert "SrO: the paper says stable; the hull says -0.136" in text


def test_marginal_band_is_twice_the_tolerance_not_the_scoring_ramp(cache):
    # TiO2 at -0.17 eV/atom scores 0.39 on the ramp (the ramp is a preference) but is a
    # reaction by any reading of the hull; the validation must not call it marginal.
    cfg = load_config("default", use_env=False, overrides={"interface": {"tolerance_ev_atom": 0.10}})
    report = validate_interface(cfg, cache, offline=True)
    by = {r.formula: r for r in report.rows}
    assert by["TiO2"].predicted == "marginal"  # inside 2 x 0.10 now
    assert by["Ta2O5"].predicted == "reacts"
    assert by["ZrO2"].predicted == "stable"  # inside the (wider) tolerance


def test_validate_cli_exists_and_exits_zero_on_the_fixture(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from oxide_triage.cli import app

    cfg = load_config("default", use_env=False)
    path = tmp_path / "c.sqlite"
    c = Cache(str(path))
    load_fixtures(cfg, c)
    c.close()
    monkeypatch.setenv("OXIDE_TRIAGE_CACHE", str(path))
    monkeypatch.setenv("OXIDE_TRIAGE_OFFLINE", "1")
    monkeypatch.delenv("MP_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["validate"])
    assert result.exit_code == 0, result.output
    assert "Hubbard & Schlom" in result.output and "21 of 21 agree" in result.output
