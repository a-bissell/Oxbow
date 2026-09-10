"""Universe scoping: experimentally observed entries only, and the dielectric-minded allowlist."""

from __future__ import annotations

from typing import Any

from oxide_triage.cache import Cache
from oxide_triage.config import load_cation_allowlist, load_config
from oxide_triage.sources.assemble import DataLayer
from oxide_triage.sources.fixtures import load_fixture
from oxide_triage.sources.materials_project import MaterialsProject


class _Recording:
    """Answers the universe query with one observed and one theoretical entry, records params."""

    def __init__(self) -> None:
        self.params: list[dict[str, Any]] = []

    def get_json(self, url, params=None, headers=None):
        self.params.append(dict(params or {}))
        if "summary" in url and "elements" in (params or {}):
            return {
                "data": [
                    {
                        "material_id": "mp-obs",
                        "elements": ["Hf", "O"],
                        "theoretical": False,
                        "formula_pretty": "HfO2",
                    },
                    {
                        "material_id": "mp-theo",
                        "elements": ["Hf", "O"],
                        "theoretical": True,
                        "formula_pretty": "HfO2",
                    },
                ]
            }
        return {"data": []}


def test_observed_only_is_sent_to_the_api_and_enforced_locally():
    http = _Recording()
    mp = MaterialsProject(Cache(":memory:"), 90, False, api_key="k", http=http)  # type: ignore[arg-type]
    ids, status = mp.fetch_universe(["Hf"], 2, 0.1, 1.8, observed_only=True)
    assert status == "fetched" and ids == ["mp-obs"]  # the theoretical entry is dropped even if returned
    assert http.params[0]["theoretical"] == "false"


def test_all_entries_mode_keeps_theoretical_and_sends_no_flag():
    http = _Recording()
    mp = MaterialsProject(Cache(":memory:"), 90, False, api_key="k", http=http)  # type: ignore[arg-type]
    ids, _ = mp.fetch_universe(["Hf"], 2, 0.1, 1.8, observed_only=False)
    assert ids == ["mp-obs", "mp-theo"] and "theoretical" not in http.params[0]


def test_observed_only_changes_the_universe_key_but_not_the_historical_one():
    base = MaterialsProject.universe_key(["Hf"], 2, 0.1, 1.8)
    assert MaterialsProject.universe_key(["Hf"], 2, 0.1, 1.8, observed_only=False) == base
    assert MaterialsProject.universe_key(["Hf"], 2, 0.1, 1.8, observed_only=True) != base


def test_fixture_universe_respects_observed_only():
    cfg = load_config("default", use_env=False)
    assert cfg.candidates.observed_only is True
    cache = Cache(":memory:")
    load_fixture(cache, cfg)
    ids = DataLayer.from_config(cfg, cache=cache, offline=True).universe_ids()
    assert "fx-0001" in ids and "fx-0002" not in ids  # cubic HfO2 is the fixture's theoretical exemplar
    wide = load_config("default", use_env=False, overrides={"candidates": {"observed_only": False}})
    cache2 = Cache(":memory:")
    load_fixture(cache2, wide)
    assert "fx-0002" in DataLayer.from_config(wide, cache=cache2, offline=True).universe_ids()


def test_default_allowlist_is_dielectric_minded_and_wide_list_still_loads():
    default = set(load_cation_allowlist())
    wide = set(load_cation_allowlist("cation_allowlist_wide.yaml"))
    cfg = load_config("default", use_env=False)
    for w in cfg.selfcheck.workhorses:  # every workhorse's cation must be in the default list
        cation = w.rstrip("O0123456789").rstrip("0123456789")
        assert any(cation.startswith(el) for el in default), w
    assert {"Hf", "Zr", "Ta", "Al", "Si", "La", "Ba", "Pb", "Bi"} <= default
    assert not ({"Li", "Na", "K", "Fe", "Cu", "P", "B", "U"} & default)
    assert default < wide
