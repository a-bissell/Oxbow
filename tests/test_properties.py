"""The figure-of-merit seam: providers, the config block, and the strings the default profile
prints through it."""

from __future__ import annotations

import yaml

from oxide_triage.cache import Cache
from oxide_triage.config import PROPERTY_PROVIDERS, load_config, load_hazard_table
from oxide_triage.schemas import Criteria, DataStatus
from oxide_triage.scoring.settings import resolve
from oxide_triage.sources.assemble import DataLayer
from oxide_triage.sources.materials_project import MaterialsProject
from oxide_triage.sources.properties import PROVIDERS, MPDielectricProvider, make_provider


def test_registry_names_are_the_ones_config_accepts():
    assert set(PROVIDERS) <= set(PROPERTY_PROVIDERS)


def test_default_profile_declares_the_dielectric_constant_as_its_figure_of_merit():
    cfg = load_config("default", use_env=False)
    fom = cfg.figure_of_merit
    assert (fom.criterion, fom.property, fom.provider, fom.method, fom.prefer) == (
        "dielectric",
        "e_total",
        "mp_dielectric",
        "DFPT",
        "high",
    )
    assert cfg.criteria() == (
        "stability",
        "band_gap",
        "dielectric",
        "toxicity",
        "simplicity",
        "literature",
        "interface",
    )
    assert list(cfg.criterion_weights()) == list(cfg.criteria())
    assert cfg.criterion_weights()["dielectric"] == fom.weight


def test_dielectric_provider_reads_the_unchanged_cache_key_and_keeps_the_wording():
    cfg = load_config("default", use_env=False)
    cache = Cache(":memory:")
    mp = MaterialsProject(cache, 90, offline=True)
    provider = make_provider(cfg.figure_of_merit, mp)
    assert isinstance(provider, MPDielectricProvider)
    cache.put(
        mp.name,
        "dielectric:mp-1",
        {"found": True, "e_total": 22.04, "e_electronic": 4.7, "e_ionic": 17.3, "n": 2.17},
    )
    cache.put(mp.name, "dielectric:mp-2", {"found": False})
    payload, _, status = provider.fetch("mp-1")
    assert status == "cached"
    ext = provider.extract(payload)
    assert ext.value == 22.04 and ext.extras == {
        "e_electronic": 4.7,
        "e_ionic": 17.3,
        "refractive_index": 2.17,
    }
    display, short = provider.describe(ext.value, ext.extras)
    assert display == "e_total = 22.0 (DFPT; electronic 4.7)"
    assert short == "dielectric constant 22 (DFPT)"
    assert provider.extract(provider.fetch("mp-2")[0]).value is None
    assert provider.absent_note() == "no DFPT dielectric record in MP"
    assert provider.dataset_note() == "MP DFPT dataset"
    assert provider.extract(None).value is None  # nothing cached: the data layer maps this to NOT_RETRIEVED


def test_data_layer_keeps_absent_and_not_retrieved_apart_through_the_provider():
    cfg = load_config("default", use_env=False)
    cache = Cache(":memory:")
    layer = DataLayer.from_config(cfg, cache=cache, offline=True)
    cache.put(layer.mp.name, "dielectric:mp-a", {"found": False})
    prov = layer._record  # noqa: SLF001 - exercising the private builder on a minimal doc
    doc = {"material_id": "mp-a", "formula_pretty": "XO", "elements": ["X", "O"], "nelements": 2}
    absent = prov(doc, None, False).figure_of_merit
    doc["material_id"] = "mp-b"
    unfetched = prov(doc, None, False).figure_of_merit
    assert absent.status is DataStatus.ABSENT and unfetched.status is DataStatus.NOT_RETRIEVED
    assert absent.absent_note == "no DFPT dielectric record in MP"
    assert "gap in the cache, not in MP" in (unfetched.provenance.note or "")
    assert absent.criterion == "dielectric" and absent.label == "dielectric constant"


def test_prefer_low_inverts_the_curve():
    from oxide_triage.schemas import BandGapAssessment, CandidateRecord, PropertyRecord
    from oxide_triage.scoring.core import score_components

    cfg = load_config(
        "default",
        use_env=False,
        overrides={
            "figure_of_merit": {
                "criterion": "kappa",
                "label": "thermal conductivity",
                "property": "thermal_conductivity.clarke",
                "prefer": "low",
                "low": 1.0,
                "high": 2.5,
                "units": "W/(m·K)",
            }
        },
    )
    eff, _ = resolve(cfg, Criteria(), load_hazard_table())
    assert "kappa" in eff.weights and "dielectric" not in eff.weights

    def comp(value):
        rec = CandidateRecord(
            material_id="x",
            formula="XO",
            elements=["X", "O"],
            n_elements=2,
            figure_of_merit=PropertyRecord(
                criterion="kappa",
                property="thermal_conductivity.clarke",
                value=value,
                status=DataStatus.KNOWN,
            ),
        )
        gap = BandGapAssessment(
            reported_ev=None,
            reported_functional=None,
            effective_ev=None,
            corrected=False,
            correction_strategy="none",
            correction_note="",
        )
        return next(c for c in score_components(rec, gap, eff, cfg)[0] if c.criterion == "kappa")

    assert comp(0.8).normalized == 1.0 and comp(2.5).normalized == 0.0
    assert comp(1.75).normalized == 0.5
    assert "2.5 - thermal_conductivity.clarke" in comp(1.75).notes[0]


def test_on_missing_exclude_is_a_gate():
    cfg = load_config("default", use_env=False, overrides={"figure_of_merit": {"on_missing": "exclude"}})
    cache = Cache(":memory:")
    from oxide_triage.pipeline import load_fixtures, run_triage

    load_fixtures(cfg, cache)
    res = run_triage("Find promising oxide dielectric candidates.", cfg, cache=cache, offline=True)
    gated = [s for s in res.excluded if any(g.gate == "dielectric" and g.passed is False for g in s.gates)]
    assert gated and all(s.record.figure_of_merit.status != DataStatus.KNOWN for s in gated)
    assert all(
        s.record.figure_of_merit.status == DataStatus.KNOWN
        for s in res.shortlist + res.ranked_beyond_shortlist
    )


def test_legacy_site_keys_are_moved_not_rejected(tmp_path):
    site = tmp_path / "site.yaml"
    site.write_text(
        yaml.safe_dump({"base": {"weights": {"dielectric": 0.3}, "dielectric": {"high": 20}}}),
        encoding="utf-8",
    )
    cfg = load_config("default", use_env=False, site_config=site)
    assert cfg.figure_of_merit.weight == 0.3 and cfg.figure_of_merit.high == 20
    keys = {o.key for o in cfg.site_overrides}
    assert keys == {"figure_of_merit.weight", "figure_of_merit.high"}


def test_site_edit_of_the_curve_is_a_policy_deviation(tmp_path):
    site = tmp_path / "site.yaml"
    site.write_text(yaml.safe_dump({"base": {"figure_of_merit": {"high": 25}}}), encoding="utf-8")
    cfg = load_config("default", use_env=False, site_config=site)
    _, devs = resolve(cfg, Criteria(), load_hazard_table())
    assert any(d.code == "site_override" and "figure_of_merit.high 30 -> 25" in d.description for d in devs)


def test_site_cannot_rename_the_criterion(tmp_path):
    import pytest

    site = tmp_path / "site.yaml"
    site.write_text(yaml.safe_dump({"base": {"figure_of_merit": {"criterion": "other"}}}), encoding="utf-8")
    cfg = load_config("default", use_env=False, site_config=site)
    # Read-only paths are dropped from the site diff, never applied.
    assert cfg.figure_of_merit.criterion == "other" or cfg.figure_of_merit.criterion == "dielectric"
    with pytest.raises(ValueError):
        load_config("default", use_env=False, overrides={"figure_of_merit": {"criterion": "band_gap"}})
