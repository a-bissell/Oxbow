from __future__ import annotations

from oxide_triage.config import load_config, load_hazard_table
from oxide_triage.refute import (
    SEVERITY_RANK,
    flatten,
    numeric_guard,
    primary_caveat,
    rule_caveats,
)
from oxide_triage.schemas import Criteria, DataStatus
from oxide_triage.scoring.core import score_candidate
from oxide_triage.scoring.settings import resolve
from tests.test_scoring import make_record

CFG = load_config("default", use_env=False)
EFF = resolve(CFG, Criteria(), load_hazard_table())[0]


def caveats_for(**kw):
    sc = score_candidate(make_record(**kw), CFG, EFF)
    sc.caveats = rule_caveats(sc, EFF, CFG)
    return {c.code: c for c in sc.caveats}, sc


def test_clean_candidate_gets_only_the_correction_note():
    codes, _ = caveats_for()
    assert set(codes) == {"band_gap_corrected"}


def test_missing_dielectric_and_single_source_flagged():
    codes, sc = caveats_for(e_total=None, oqmd=None)
    assert "dielectric_unknown" in codes and "single_source_stability" in codes
    assert "partial_data" in codes and sc.confidence != "high"


def test_disagreement_is_critical_and_carries_both_numbers():
    codes, _ = caveats_for(e_hull=0.01, oqmd=0.2)
    c = codes["cross_source_disagreement"]
    assert c.severity == "critical"
    assert c.evidence["mp_e_hull"] == 0.01 and c.evidence["oqmd_stability"] == 0.2


def test_hygroscopic_and_hazard_rules():
    codes, _ = caveats_for(formula="La2O3", elements=["La", "O"])
    assert codes["hygroscopic_risk"].severity == "warning"
    codes, _ = caveats_for(formula="BaZrO3", elements=["Ba", "Zr", "O"])
    assert codes["hazard_caution"].evidence["element"] == "Ba"
    codes, _ = caveats_for(formula="SrO", elements=["Sr", "O"])
    assert "hygroscopic_risk" in codes and "literature_count_noisy" in codes


def test_thin_literature_thresholds():
    assert "no_thin_film_literature" in caveats_for(thin_film=0)[0]
    assert "thin_literature" in caveats_for(thin_film=3)[0]
    assert "thin_literature" not in caveats_for(thin_film=300)[0]


def test_gap_near_threshold_and_metastable():
    codes, _ = caveats_for(gap=2.95, e_hull=0.03)  # 2.95*1.4 = 4.13, within 0.5 of 4.0
    assert "band_gap_near_threshold" in codes and codes["metastable"].severity == "warning"


def test_caveats_sorted_by_severity_then_code_and_primary_is_first():
    _, sc = caveats_for(e_hull=0.01, oqmd=0.3, e_total=None, formula="BaO", elements=["Ba", "O"])
    ranks = [SEVERITY_RANK[c.severity] for c in sc.caveats]
    assert ranks == sorted(ranks)
    assert primary_caveat(sc).code == "cross_source_disagreement"


def test_numeric_guard():
    facts = flatten({"a": {"e_hull": 0.012, "gap": 5.63}, "n": 4600, "s": "E_hull = 0.000 eV/atom"})
    assert numeric_guard("The gap of 5.63 eV rests on 4600 papers", facts)
    assert numeric_guard("hull distance 0.012", facts)
    assert not numeric_guard("dielectric constant is 27.5", facts)
    assert not numeric_guard("cite Smith 2019", facts)
    assert numeric_guard("no numbers here", facts)


def test_incomplete_retrieval_is_the_loudest_caveat():
    """A candidate the cache failed to fetch is pushed down the ranking for a reason that has
    nothing to do with the material. The refutation pass has to say so, and say it first."""
    r = make_record(e_total=None, thin_film=None, total=None)
    r.dielectric.status = DataStatus.NOT_RETRIEVED
    r.literature.status = DataStatus.NOT_RETRIEVED
    sc = score_candidate(r, CFG, EFF)
    sc.caveats = rule_caveats(sc, EFF, CFG)
    codes = [c.code for c in sc.caveats]

    assert "incomplete_retrieval" in codes
    top = primary_caveat(sc)
    assert top is not None and top.code == "incomplete_retrieval", codes
    assert top.severity == "critical"
    assert "not comparable" in top.text
    # and it must not be mistaken for a measured absence
    assert "dielectric_unknown" not in codes
    assert "literature_not_retrieved" in codes and "literature_unavailable" not in codes


def test_absent_data_keeps_its_own_measured_caveats():
    """The mirror case: MP genuinely holds no DFPT record. That is a fact about the material's
    coverage and keeps the ordinary caveat, with no comparability warning."""
    r = make_record(e_total=None)
    r.dielectric.status = DataStatus.ABSENT
    sc = score_candidate(r, CFG, EFF)
    sc.caveats = rule_caveats(sc, EFF, CFG)
    codes = [c.code for c in sc.caveats]
    assert "dielectric_unknown" in codes and "incomplete_retrieval" not in codes


def test_untested_cross_check_does_not_claim_single_source_evidence():
    r = make_record(oqmd=None)
    r.cross_check.status = DataStatus.NOT_RETRIEVED
    sc = score_candidate(r, CFG, EFF)
    sc.caveats = rule_caveats(sc, EFF, CFG)
    codes = [c.code for c in sc.caveats]
    assert "cross_check_untested" in codes and "single_source_stability" not in codes
