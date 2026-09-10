"""Tests for the deterministic core. This is the code that must be provably correct."""

from __future__ import annotations

import copy

import pytest

from oxide_triage.config import load_config, load_hazard_table
from oxide_triage.schemas import (
    BandGapRecord,
    CandidateRecord,
    Criteria,
    CrossCheckRecord,
    DataStatus,
    DielectricRecord,
    HazardRecord,
    LiteratureRecord,
    StabilityRecord,
)
from oxide_triage.scoring.bandgap import assess_band_gap
from oxide_triage.scoring.core import (
    aggregate,
    evaluate_gates,
    rank,
    retrieval_completeness,
    score_candidate,
    score_components,
)
from oxide_triage.scoring.settings import resolve

TABLE = load_hazard_table()


def make_record(
    mid: str = "t-1",
    formula: str = "HfO2",
    elements: list[str] | None = None,
    e_hull: float | None = 0.0,
    gap: float | None = 4.0,
    functional: str | None = "GGA",
    e_total: float | None = 22.0,
    oqmd: float | None = 0.0,
    thin_film: int | None = 100,
    total: int | None = 1000,
) -> CandidateRecord:
    elements = elements or ["Hf", "O"]
    tiers = {el: TABLE.lookup(el)[0] for el in elements if el != "O"}
    worst = max(tiers.values()) if tiers else 0
    return CandidateRecord(
        material_id=mid,
        formula=formula,
        elements=elements,
        n_elements=len(elements),
        stability=StabilityRecord(
            energy_above_hull_ev_atom=e_hull,
            status=DataStatus.KNOWN if e_hull is not None else DataStatus.ABSENT,
            functional="test",
        ),
        band_gap=BandGapRecord(
            value_ev=gap,
            functional=functional,
            status=DataStatus.KNOWN if gap is not None else DataStatus.ABSENT,
        ),
        dielectric=DielectricRecord(
            e_total=e_total, status=DataStatus.KNOWN if e_total is not None else DataStatus.ABSENT
        ),
        cross_check=CrossCheckRecord(
            stability_ev_atom=oqmd, status=DataStatus.KNOWN if oqmd is not None else DataStatus.ABSENT
        ),
        literature=LiteratureRecord(
            total_works=total,
            thin_film_works=thin_film,
            status=DataStatus.KNOWN if thin_film is not None else DataStatus.ABSENT,
        ),
        hazard=HazardRecord(
            element_tiers=tiers,
            worst_tier=worst,
            worst_elements=[el for el, t in tiers.items() if t == worst],
            status=DataStatus.KNOWN,
            table_version=TABLE.version,
        ),
    )


@pytest.fixture
def cfg():
    return load_config("default", use_env=False)


@pytest.fixture
def eff(cfg):
    return resolve(cfg, Criteria(), TABLE)[0]


# ---- band gap correction -----------------------------------------------------------------


class TestBandGap:
    def test_scalar_factor_labels_corrected(self, cfg):
        c = copy.deepcopy(cfg.band_gap)
        c.correction.strategy = "scalar_factor"
        a = assess_band_gap(BandGapRecord(value_ev=4.0, functional="GGA", status=DataStatus.KNOWN), c)
        assert a.corrected is True
        assert a.effective_ev == pytest.approx(5.6)
        assert "CORRECTED" in a.correction_note
        assert a.reported_functional == "GGA"

    def test_none_strategy_leaves_value(self, cfg):
        c = copy.deepcopy(cfg.band_gap)
        c.correction.strategy = "none"
        a = assess_band_gap(BandGapRecord(value_ev=4.0, functional="GGA", status=DataStatus.KNOWN), c)
        assert a.corrected is False
        assert a.effective_ev == 4.0

    def test_hse_never_scaled(self, cfg):
        for strategy in ("scalar_factor", "hse_preferred"):
            c = copy.deepcopy(cfg.band_gap)
            c.correction.strategy = strategy
            a = assess_band_gap(BandGapRecord(value_ev=5.8, functional="HSE06", status=DataStatus.KNOWN), c)
            assert a.corrected is False
            assert a.effective_ev == 5.8

    def test_hse_preferred_fallback_none(self, cfg):
        c = copy.deepcopy(cfg.band_gap)
        c.correction.strategy = "hse_preferred"
        c.correction.fallback = "none"
        a = assess_band_gap(BandGapRecord(value_ev=4.0, functional="GGA", status=DataStatus.KNOWN), c)
        assert a.corrected is False and a.effective_ev == 4.0

    def test_unknown_gap_stays_unknown(self, cfg):
        a = assess_band_gap(BandGapRecord(status=DataStatus.ABSENT), cfg.band_gap)
        assert a.effective_ev is None and a.corrected is False


# ---- gates ------------------------------------------------------------------------------


class TestGates:
    def test_passes_all(self, cfg, eff):
        r = make_record()
        gates, reasons = evaluate_gates(r, assess_band_gap(r.band_gap, cfg.band_gap), eff)
        assert reasons == []
        assert all(g.passed for g in gates)

    def test_hull_gate(self, cfg, eff):
        r = make_record(e_hull=0.2)
        _, reasons = evaluate_gates(r, assess_band_gap(r.band_gap, cfg.band_gap), eff)
        assert any("E_hull" in x for x in reasons)

    def test_gap_gate_uses_effective_value(self, cfg, eff):
        # 3.0 eV PBE x 1.4 = 4.2 >= 4.0 passes; with strategy none it fails
        r = make_record(gap=3.0)
        _, reasons = evaluate_gates(r, assess_band_gap(r.band_gap, cfg.band_gap), eff)
        assert reasons == []
        c = copy.deepcopy(cfg.band_gap)
        c.correction.strategy = "none"
        _, reasons = evaluate_gates(r, assess_band_gap(r.band_gap, c), eff)
        assert any("gap" in x for x in reasons)

    def test_blocklist_gate(self, cfg, eff):
        r = make_record(formula="PbTiO3", elements=["Pb", "Ti", "O"])
        _, reasons = evaluate_gates(r, assess_band_gap(r.band_gap, cfg.band_gap), eff)
        assert any("Pb" in x for x in reasons)

    def test_allowlist_lifts_block_and_records_deviation(self, cfg):
        eff2, devs = resolve(cfg, Criteria(allow_elements=["Pb"]), TABLE)
        r = make_record(formula="PbTiO3", elements=["Pb", "Ti", "O"])
        _, reasons = evaluate_gates(r, assess_band_gap(r.band_gap, cfg.band_gap), eff2)
        assert reasons == []
        assert any(d.code == "request_element_allowlist" for d in devs)

    def test_missing_stability_excluded_by_default(self, cfg, eff):
        r = make_record(e_hull=None)
        gates, reasons = evaluate_gates(r, assess_band_gap(r.band_gap, cfg.band_gap), eff)
        assert reasons and "unknown" in reasons[0]
        assert [g for g in gates if g.gate == "stability"][0].passed is False

    def test_missing_stability_flag_mode_is_indeterminate(self, cfg):
        cfg2 = load_config("default", use_env=False, overrides={"gates": {"on_missing_stability": "flag"}})
        eff2 = resolve(cfg2, Criteria(), TABLE)[0]
        r = make_record(e_hull=None)
        gates, reasons = evaluate_gates(r, assess_band_gap(r.band_gap, cfg2.band_gap), eff2)
        assert reasons == []
        assert [g for g in gates if g.gate == "stability"][0].passed is None

    def test_max_elements(self, cfg):
        eff2 = resolve(cfg, Criteria(max_elements=2), TABLE)[0]
        r = make_record(formula="SrTiO3", elements=["Sr", "Ti", "O"])
        _, reasons = evaluate_gates(r, assess_band_gap(r.band_gap, cfg.band_gap), eff2)
        assert any("elements" in x for x in reasons)

    def test_required_and_excluded_elements(self, cfg):
        eff2 = resolve(cfg, Criteria(include_elements=["Hf"], exclude_elements=["Zr"]), TABLE)[0]
        r = make_record(formula="ZrO2", elements=["Zr", "O"])
        _, reasons = evaluate_gates(r, assess_band_gap(r.band_gap, cfg.band_gap), eff2)
        assert len(reasons) == 2


# ---- components & aggregation -------------------------------------------------------------


class TestComponents:
    def test_every_component_present_and_inspectable(self, cfg, eff):
        r = make_record()
        comps, agreement = score_components(r, assess_band_gap(r.band_gap, cfg.band_gap), eff, cfg)
        assert [c.criterion for c in comps] == [
            "stability",
            "band_gap",
            "dielectric",
            "toxicity",
            "simplicity",
            "literature",
        ]
        assert agreement == "agree"
        for c in comps:
            assert c.status == DataStatus.KNOWN
            assert c.contribution == pytest.approx(c.weight * c.normalized, abs=1e-6)
        assert sum(c.weight for c in comps) == pytest.approx(1.0)

    def test_cross_check_bonus_and_penalty(self, cfg, eff):
        base = make_record(oqmd=None)
        agree = make_record(oqmd=0.0)
        disagree = make_record(oqmd=0.3)
        get = lambda r: [
            c
            for c in score_components(r, assess_band_gap(r.band_gap, cfg.band_gap), eff, cfg)[0]
            if c.criterion == "stability"
        ][0]
        b, a, d = get(base), get(agree), get(disagree)
        assert b.normalized == pytest.approx(1.0)  # already capped
        assert a.normalized == pytest.approx(1.0)
        assert d.normalized == pytest.approx(1.0 - cfg.stability.disagreement_penalty)
        assert any("one source" in n for n in b.notes)

    def test_unknown_dielectric_is_unknown_not_zero(self, cfg, eff):
        r = make_record(e_total=None)
        comps, _ = score_components(r, assess_band_gap(r.band_gap, cfg.band_gap), eff, cfg)
        d = [c for c in comps if c.criterion == "dielectric"][0]
        assert d.status == DataStatus.ABSENT
        assert d.normalized is None and d.contribution is None
        assert "UNKNOWN" in d.raw_label

        zero = make_record(e_total=5.0)  # below `low` scores 0 but is KNOWN
        comps0, _ = score_components(zero, assess_band_gap(zero.band_gap, cfg.band_gap), eff, cfg)
        d0 = [c for c in comps0 if c.criterion == "dielectric"][0]
        assert d0.status == DataStatus.KNOWN and d0.normalized == 0.0

    def test_missing_data_reduces_coverage_and_confidence(self, cfg, eff):
        full = score_candidate(make_record(), cfg, eff)
        partial = score_candidate(make_record(e_total=None), cfg, eff)
        assert full.data_coverage == pytest.approx(1.0) and full.confidence == "high"
        assert partial.data_coverage == pytest.approx(1.0 - eff.weights["dielectric"])
        assert partial.missing_criteria == ["dielectric"]
        assert partial.confidence in {"medium", "low"}

    def test_no_credit_policy_never_rewards_missing_data(self, cfg, eff):
        """A candidate with no dielectric value must not outrank an identical one whose
        dielectric value is known but mediocre. (The known-answer eval caught this.)"""
        known = score_candidate(make_record(mid="a", e_total=15.0), cfg, eff)
        unknown = score_candidate(make_record(mid="b", e_total=None), cfg, eff)
        assert unknown.raw_score > known.raw_score  # renormalised raw *is* higher ...
        assert unknown.adjusted_score < known.adjusted_score  # ... but ranking score is not

    def test_renormalize_policy_is_available_and_different(self, cfg):
        cfg2 = load_config("default", use_env=False, overrides={"missing_data": {"policy": "renormalize"}})
        eff2 = resolve(cfg2, Criteria(), TABLE)[0]
        s = score_candidate(make_record(e_total=None), cfg2, eff2)
        raw, cov = s.raw_score, s.data_coverage
        assert s.adjusted_score == pytest.approx(
            max(0.0, raw - cfg2.missing_data.penalty * (1 - cov)), abs=1e-6
        )

    def test_aggregate_formula_no_credit(self, cfg, eff):
        s = score_candidate(make_record(e_total=None), cfg, eff)
        known = [c for c in s.components if c.status == DataStatus.KNOWN]
        raw = sum(c.weight * c.normalized for c in known) / sum(c.weight for c in known)
        cov = sum(c.weight for c in known)
        assert s.raw_score == pytest.approx(raw, abs=1e-6)
        assert s.adjusted_score == pytest.approx(raw * cov - cfg.missing_data.penalty * (1 - cov), abs=1e-6)

    def test_all_unknown_gives_none_score(self, cfg, eff):
        r = make_record(e_hull=None, gap=None, e_total=None, oqmd=None, thin_film=None, total=None)
        r.hazard.status = DataStatus.ABSENT
        comps, _ = score_components(r, assess_band_gap(r.band_gap, cfg.band_gap), eff, cfg)
        raw, cov, adj, missing, _absent, _nr, _gap, conf = aggregate(comps, cfg)
        # simplicity is always computable, so coverage is exactly its weight
        assert cov == pytest.approx(eff.weights["simplicity"])
        assert conf == "low"
        assert set(missing) == {"stability", "band_gap", "dielectric", "toxicity", "literature"}

    def test_toxicity_tiers(self, cfg, eff):
        t0 = score_candidate(make_record(), cfg, eff)
        t1 = score_candidate(make_record(formula="BaZrO3", elements=["Ba", "Zr", "O"]), cfg, eff)
        get = lambda s: [c for c in s.components if c.criterion == "toxicity"][0].normalized
        assert get(t0) == 1.0 and get(t1) == 0.5


# ---- ranking ----------------------------------------------------------------------------


class TestRanking:
    def test_deterministic_and_tie_broken_by_id(self, cfg, eff):
        recs = [make_record(mid="z-2"), make_record(mid="a-1"), make_record(mid="m-3", e_total=35.0)]
        r1, _ = rank(recs, cfg, eff)
        r2, _ = rank(list(reversed(recs)), cfg, eff)
        assert [s.record.material_id for s in r1] == [s.record.material_id for s in r2]
        assert [s.record.material_id for s in r1] == ["m-3", "a-1", "z-2"]
        assert [s.rank for s in r1] == [1, 2, 3]
        assert r1[0].model_dump() == r2[0].model_dump()

    def test_excluded_are_separated_with_reasons(self, cfg, eff):
        recs = [make_record(mid="ok"), make_record(mid="bad", e_hull=0.5)]
        ranked, excluded = rank(recs, cfg, eff)
        assert [s.record.material_id for s in ranked] == ["ok"]
        assert excluded[0].record.material_id == "bad" and excluded[0].exclusion_reasons

    def test_weights_change_order(self, cfg):
        wide_gap_low_k = make_record(mid="gap", gap=6.5, e_total=12.0)
        high_k_low_gap = make_record(mid="k", gap=3.0, e_total=45.0)
        eff_gap = resolve(cfg, Criteria(weight_overrides={"band_gap": 0.6, "dielectric": 0.05}), TABLE)[0]
        eff_k = resolve(cfg, Criteria(weight_overrides={"band_gap": 0.05, "dielectric": 0.6}), TABLE)[0]
        assert rank([wide_gap_low_k, high_k_low_gap], cfg, eff_gap)[0][0].record.material_id == "gap"
        assert rank([wide_gap_low_k, high_k_low_gap], cfg, eff_k)[0][0].record.material_id == "k"


class TestRetrievalProvenance:
    """ABSENT vs NOT_RETRIEVED: the source has no record, versus this cache never asked.

    Scoring must treat them identically — we do not know the value either way, and letting an
    unfetched candidate score as though its data were good is exactly the failure this split
    exists to prevent. Everything the reader sees must keep them apart, because only one of the
    two is fixable by warming the cache.
    """

    def _pair(self, cfg, eff):
        """The same material, its dielectric absent from the source vs never retrieved."""
        absent = make_record(e_total=None)
        absent.dielectric.status = DataStatus.ABSENT
        unfetched = make_record(e_total=None)
        unfetched.dielectric.status = DataStatus.NOT_RETRIEVED
        return score_candidate(absent, cfg, eff), score_candidate(unfetched, cfg, eff)

    def test_scores_are_identical_whatever_the_reason(self, cfg, eff):
        a, u = self._pair(cfg, eff)
        assert a.adjusted_score == u.adjusted_score
        assert a.data_coverage == u.data_coverage
        assert a.missing_criteria == u.missing_criteria == ["dielectric"]

    def test_the_reason_is_reported_and_flags_comparability(self, cfg, eff):
        a, u = self._pair(cfg, eff)
        assert a.absent_criteria == ["dielectric"] and a.not_retrieved_criteria == []
        assert u.not_retrieved_criteria == ["dielectric"] and u.absent_criteria == []
        # only the unfetched candidate is off the common footing
        assert a.comparable is True and a.retrieval_gap == 0.0
        assert u.comparable is False
        assert u.retrieval_gap == pytest.approx(eff.weights["dielectric"])

    def test_component_carries_the_reason_in_its_note(self, cfg, eff):
        a, u = self._pair(cfg, eff)
        note_of = lambda s: " ".join(n for c in s.components if c.criterion == "dielectric" for n in c.notes)
        assert "NOT RETRIEVED" in note_of(u)
        assert "NOT RETRIEVED" not in note_of(a)

    def test_unfetched_data_never_earns_confidence(self, cfg, eff):
        _, u = self._pair(cfg, eff)
        assert u.confidence == "low"

    def test_untested_cross_check_is_not_a_single_source_claim(self, cfg, eff):
        """'No OQMD entry' is evidence; 'OQMD was never asked' is not."""
        no_entry = make_record(oqmd=None)
        no_entry.cross_check.status = DataStatus.ABSENT
        never_asked = make_record(oqmd=None)
        never_asked.cross_check.status = DataStatus.NOT_RETRIEVED
        assert score_candidate(no_entry, cfg, eff).cross_source_agreement == "unavailable"
        assert score_candidate(never_asked, cfg, eff).cross_source_agreement == "untested"

    def _mixed(self, n_good: int, n_bad: int):
        recs = [make_record(mid=f"g-{i}") for i in range(n_good)]
        for i in range(n_bad):
            r = make_record(mid=f"b-{i}", e_total=None, thin_film=None, total=None)
            r.dielectric.status = DataStatus.NOT_RETRIEVED
            r.literature.status = DataStatus.NOT_RETRIEVED
            recs.append(r)
        return recs

    def test_completeness_reports_the_shape_of_the_gap(self, cfg, eff):
        ranked, _ = rank(self._mixed(4, 6), cfg, eff)
        rc = retrieval_completeness(ranked, cfg)
        assert rc.n_ranked == 10 and rc.n_fully_retrieved == 4
        assert rc.not_retrieved_by_criterion == {"dielectric": 6, "literature": 6}
        assert 0.0 < rc.completeness < 1.0
        assert not rc.comparable and "INCOMPLETE RETRIEVAL" in rc.note
        assert "dielectric (6 candidates)" in rc.note

    def test_a_small_gap_stays_within_the_floor(self, cfg, eff):
        """The floor is a judgement call, not a purity test: a couple of holes in ten candidates
        is still a comparable ranking, and saying otherwise would cry wolf on every real cache."""
        ranked, _ = rank(self._mixed(9, 1), cfg, eff)
        rc = retrieval_completeness(ranked, cfg)
        assert rc.comparable and rc.completeness >= cfg.retrieval.min_completeness_warn

    def test_a_fully_retrieved_cache_is_comparable(self, cfg, eff):
        ranked, _ = rank([make_record(mid=f"g-{i}") for i in range(5)], cfg, eff)
        rc = retrieval_completeness(ranked, cfg)
        assert rc.completeness == 1.0 and rc.comparable
        assert rc.not_retrieved_by_criterion == {}

    def test_missing_cross_check_counts_against_completeness(self, cfg, eff):
        """The cross-check moves the score through the stability bonus rather than a weight of
        its own; a completeness measure that ignored it once reported 90% on a cache missing
        84% of its cross-checks."""
        recs = []
        for i in range(4):
            r = make_record(mid=f"x-{i}", oqmd=None)
            r.cross_check.status = DataStatus.NOT_RETRIEVED
            recs.append(r)
        ranked, _ = rank(recs, cfg, eff)
        rc = retrieval_completeness(ranked, cfg)
        assert rc.not_retrieved_by_criterion.get("cross_check") == 4
        assert rc.completeness < 1.0 and rc.n_fully_retrieved == 0
