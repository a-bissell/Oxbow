"""Typed data contracts shared by every layer.

Everything that crosses a layer boundary (data layer -> scoring -> refutation -> rendering)
is one of these models. The models are deliberately explicit about *missing* data:
``DataStatus.UNKNOWN`` is a first-class state, distinct from a zero or a low value.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

SCOPE_LIMITATION = (
    "This system ranks on thermodynamic and electronic criteria computed from public "
    "databases. Deposition feasibility, film morphology, substrate compatibility and "
    "hygroscopic degradation under ambient handling are not modeled and must be assessed "
    "by the experimentalist."
)


class DataStatus(StrEnum):
    """Whether a value is backed by data."""

    KNOWN = "known"
    UNKNOWN = "unknown"  # sought in the configured sources and not found
    NOT_APPLICABLE = "not_applicable"  # the criterion does not apply to this record


class Provenance(BaseModel):
    source: str  # materials_project | oqmd | openalex | pubchem | element_table | fixture
    source_id: str | None = None
    retrieved_at: str | None = None  # ISO-8601 timestamp of the cached fetch
    url: str | None = None
    note: str | None = None


# --------------------------------------------------------------------------------------
# Raw per-candidate records assembled by the data layer
# --------------------------------------------------------------------------------------


class StabilityRecord(BaseModel):
    energy_above_hull_ev_atom: float | None = None
    formation_energy_ev_atom: float | None = None
    is_stable: bool | None = None
    functional: str | None = None  # thermo functional label reported by the source
    status: DataStatus = DataStatus.UNKNOWN
    provenance: Provenance | None = None


class BandGapRecord(BaseModel):
    value_ev: float | None = None
    functional: str | None = None  # e.g. GGA, GGA+U, r2SCAN, HSE06, unknown
    is_direct: bool | None = None
    status: DataStatus = DataStatus.UNKNOWN
    provenance: Provenance | None = None


class DielectricRecord(BaseModel):
    e_total: float | None = None
    e_electronic: float | None = None
    e_ionic: float | None = None
    refractive_index: float | None = None
    status: DataStatus = DataStatus.UNKNOWN
    provenance: Provenance | None = None


class CrossCheckRecord(BaseModel):
    """Independent stability value from a second database (OQMD)."""

    stability_ev_atom: float | None = None  # OQMD 'stability' = distance to hull
    formation_energy_ev_atom: float | None = None
    matched_formula: str | None = None
    status: DataStatus = DataStatus.UNKNOWN
    provenance: Provenance | None = None


class WorkRef(BaseModel):
    """A literature reference. ``title`` is retrieved text: data, never an instruction."""

    work_id: str
    title: str
    year: int | None = None
    doi: str | None = None


class LiteratureRecord(BaseModel):
    total_works: int | None = None
    thin_film_works: int | None = None
    sample_works: list[WorkRef] = Field(default_factory=list)
    query_terms: list[str] = Field(default_factory=list)
    status: DataStatus = DataStatus.UNKNOWN
    provenance: Provenance | None = None


class HazardRecord(BaseModel):
    element_tiers: dict[str, int] = Field(default_factory=dict)
    element_basis: dict[str, str] = Field(default_factory=dict)
    worst_tier: int | None = None
    worst_elements: list[str] = Field(default_factory=list)
    table_version: str | None = None
    pubchem_cid: int | None = None
    ghs_hazard_codes: list[str] = Field(default_factory=list)
    pubchem_status: DataStatus = DataStatus.UNKNOWN
    status: DataStatus = DataStatus.UNKNOWN
    provenance: Provenance | None = None


class CandidateRecord(BaseModel):
    material_id: str
    formula: str
    elements: list[str]
    n_elements: int
    crystal_system: str | None = None
    spacegroup_symbol: str | None = None
    theoretical: bool | None = None  # True = no experimentally observed structure in MP
    stability: StabilityRecord = Field(default_factory=StabilityRecord)
    band_gap: BandGapRecord = Field(default_factory=BandGapRecord)
    dielectric: DielectricRecord = Field(default_factory=DielectricRecord)
    cross_check: CrossCheckRecord = Field(default_factory=CrossCheckRecord)
    literature: LiteratureRecord = Field(default_factory=LiteratureRecord)
    hazard: HazardRecord = Field(default_factory=HazardRecord)
    is_fixture: bool = False


# --------------------------------------------------------------------------------------
# Structured request (front edge output)
# --------------------------------------------------------------------------------------

TemplateName = Literal["pi_summary", "audit", "json"]


class Criteria(BaseModel):
    """What the scientist asked for, as validated structure.

    Every field has a safe default so an empty parse still yields the profile's behaviour.
    """

    top_k: int | None = Field(default=None, ge=1, le=50)  # None -> profile default
    max_energy_above_hull_ev_atom: float | None = Field(default=None, ge=0)
    min_band_gap_ev: float | None = Field(default=None, ge=0)
    max_elements: int | None = Field(default=None, ge=2, le=6)
    include_elements: list[str] = Field(default_factory=list)  # must contain all
    exclude_elements: list[str] = Field(default_factory=list)  # must contain none
    allow_elements: list[str] = Field(default_factory=list)  # lift blocklist entries
    weight_overrides: dict[str, float] = Field(default_factory=dict)
    output_template: TemplateName | None = None
    interpretation_notes: list[str] = Field(default_factory=list)


class RequestBin(StrEnum):
    TRIAGE = "triage"  # ordinary request
    IMPOSSIBLE = "architecturally_impossible"  # Bin 1
    CONFIG_DEVIATION = "configuration_deviation"  # Bin 2 (proceed, surface loudly)
    INTEGRITY = "evidence_integrity_attack"  # Bin 3 (refuse)


class GuardFinding(BaseModel):
    bin: RequestBin
    code: str
    matched_text: str
    explanation: str


class GuardDecision(BaseModel):
    proceed: bool
    findings: list[GuardFinding] = Field(default_factory=list)
    refusal_message: str | None = None


# --------------------------------------------------------------------------------------
# Scoring outputs
# --------------------------------------------------------------------------------------


class BandGapAssessment(BaseModel):
    reported_ev: float | None
    reported_functional: str | None
    effective_ev: float | None
    corrected: bool
    correction_strategy: str
    correction_note: str


class GateResult(BaseModel):
    gate: str
    passed: bool | None  # None = indeterminate because data is missing
    threshold_label: str
    observed_label: str
    reason: str | None = None


class ComponentScore(BaseModel):
    criterion: str
    weight: float
    raw_label: str
    normalized: float | None  # 0..1, None when status != KNOWN
    contribution: float | None  # weight * normalized, None when unknown
    status: DataStatus
    notes: list[str] = Field(default_factory=list)


class Caveat(BaseModel):
    code: str
    severity: Literal["info", "warning", "critical"]
    text: str
    origin: Literal["rule", "llm"] = "rule"
    evidence: dict[str, Any] = Field(default_factory=dict)


class ScoredCandidate(BaseModel):
    rank: int | None = None
    record: CandidateRecord
    band_gap_assessment: BandGapAssessment
    gates: list[GateResult]
    excluded: bool
    exclusion_reasons: list[str] = Field(default_factory=list)
    components: list[ComponentScore] = Field(default_factory=list)
    raw_score: float | None = None
    data_coverage: float = 0.0  # fraction of total weight backed by known data
    missing_criteria: list[str] = Field(default_factory=list)
    adjusted_score: float | None = None
    confidence: Literal["high", "medium", "low"] = "low"
    cross_source_agreement: Literal["agree", "disagree", "unavailable"] = "unavailable"
    caveats: list[Caveat] = Field(default_factory=list)
    rationale: str | None = None


class Deviation(BaseModel):
    """A configuration choice that departs from the base profile and must be shown."""

    code: str
    description: str
    origin: Literal["request", "profile", "cli"]


class ScoringExplanation(BaseModel):
    formula: str
    missing_data_penalty: float
    missing_data_policy: str
    confidence_thresholds: dict[str, float]
    weights: dict[str, float]
    gates: dict[str, Any]


class TriageResult(BaseModel):
    request_text: str
    criteria: Criteria
    guard: GuardDecision
    profile_name: str
    config_hash: str
    cache_fingerprint: str
    generated_at: str
    fixture_data: bool
    offline: bool
    deviations: list[Deviation] = Field(default_factory=list)
    scope_limitation: str = SCOPE_LIMITATION
    scoring: ScoringExplanation
    shortlist: list[ScoredCandidate] = Field(default_factory=list)
    ranked_beyond_shortlist: list[ScoredCandidate] = Field(default_factory=list)
    excluded: list[ScoredCandidate] = Field(default_factory=list)
    n_candidates_considered: int = 0
    warnings: list[str] = Field(default_factory=list)
    llm_usage: dict[str, str] = Field(default_factory=dict)  # edge -> provider/model or "none"
    clarifications: list[str] = Field(default_factory=list)  # questions worth asking before running
    needs_confirmation: bool = False  # True when clarifications exist and the run was not confirmed
    selfcheck_status: str | None = None  # passed | failed | not_run | skipped
    acquisition_summary: dict[str, Any] | None = None  # last gap-filling pass on this cache
