from __future__ import annotations

from oxide_triage.config import load_config, load_hazard_table
from oxide_triage.refute import SEVERITY_RANK, primary_caveat, refute, rule_caveats
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


def test_incomplete_retrieval_is_the_loudest_caveat():
    """A candidate the cache failed to fetch is pushed down the ranking for a reason that has
    nothing to do with the material. The refutation pass has to say so, and say it first."""
    r = make_record(e_total=None, thin_film=None, total=None)
    r.figure_of_merit.status = DataStatus.NOT_RETRIEVED
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
    r.figure_of_merit.status = DataStatus.ABSENT
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


# ---- a main caveat that is specific to its row ---------------------------------------------------


def test_within_a_severity_bench_relevance_beats_the_alphabet():
    # MgO: hygroscopic (info) and band-gap-corrected (info). Alphabetically "band_gap_corrected"
    # came first on every such row; a thin-film scientist wants the moisture note.
    _, sc = caveats_for(formula="MgO", elements=["Mg", "O"])
    codes = [c.code for c in sc.caveats]
    assert codes.index("hygroscopic_risk") < codes.index("band_gap_corrected")
    assert primary_caveat(sc).code == "hygroscopic_risk"


def test_a_caveat_every_candidate_shares_is_said_once_and_leaves_the_rows():
    from oxide_triage.refute import shared_caveats

    _, a = caveats_for(mid="a", formula="HfO2")
    _, b = caveats_for(mid="b", formula="MgO", elements=["Mg", "O"])
    notes = shared_caveats([a, b], CFG)
    assert [n.code for n in notes] == ["band_gap_corrected"]
    assert "Every band gap on the shortlist" in notes[0].text and "1.4x" in notes[0].text
    shared = [n.code for n in notes]
    assert primary_caveat(a, shared) is None  # nothing specific to HfO2 remains
    assert primary_caveat(b, shared).code == "hygroscopic_risk"
    # the per-candidate copies are untouched: the audit view still lists them
    assert "band_gap_corrected" in {c.code for c in a.caveats}
    # a single-row shortlist hoists nothing
    assert shared_caveats([a], CFG) == []


def test_only_codes_with_a_run_level_wording_are_hoisted():
    from oxide_triage.refute import shared_caveats

    _, a = caveats_for(mid="a", formula="La2O3", elements=["La", "O"])
    _, b = caveats_for(mid="b", formula="CaO", elements=["Ca", "O"])
    assert "hygroscopic_risk" in {c.code for c in a.caveats} & {c.code for c in b.caveats}
    assert "hygroscopic_risk" not in {n.code for n in shared_caveats([a, b], CFG)}


def test_common_substrates_get_a_literature_confound_note_without_touching_the_score():
    codes, sc = caveats_for(formula="LaAlO3", elements=["La", "Al", "O"])
    assert codes["substrate_literature_confound"].severity == "info"
    assert "substrate" in codes["substrate_literature_confound"].text
    plain_score = score_candidate(make_record(formula="LaAlO3", elements=["La", "Al", "O"]), CFG, EFF)
    assert plain_score.adjusted_score == sc.adjusted_score
    assert "substrate_literature_confound" not in caveats_for(formula="HfO2")[0]


def test_refutation_is_rules_only_whatever_the_model_configuration():
    cfg = load_config("default", use_env=False, overrides={"llm": {"provider": "anthropic"}})
    eff = resolve(cfg, Criteria(), load_hazard_table())[0]
    shortlist = [
        score_candidate(make_record(mid=f"t-{i}", formula=f, elements=None), cfg, eff)
        for i, f in enumerate(["HfO2", "ZrO2"])
    ]
    assert refute(shortlist, eff, cfg) == "rules"
    for sc in shortlist:
        assert sc.caveats == rule_caveats(sc, eff, cfg)
        assert all(c.origin == "rule" for c in sc.caveats)
