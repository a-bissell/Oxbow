"""Typed data contracts shared by every layer.

Everything that crosses a layer boundary (data layer -> scoring -> refutation -> rendering)
is one of these models. The models are deliberately explicit about *missing* data:
a missing value is a first-class state, distinct from a zero or a low value, and it records
whether the value is absent from the source or simply was never retrieved.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from oxide_triage.elements import SYMBOLS
from oxide_triage.formula import parse_formula

SCOPE_LIMITATION = (
    "This system ranks on thermodynamic and electronic criteria computed from public "
    "databases, including the bulk thermodynamic stability of each oxide in contact with the "
    "configured substrate. Deposition feasibility, reaction kinetics, interlayer formation, film "
    "morphology, epitaxy and hygroscopic degradation under ambient handling are not modeled and "
    "must be assessed by the experimentalist."
)


class DataStatus(StrEnum):
    """Whether a value is backed by data, and when it is not, *why* not.

    The split between ABSENT and NOT_RETRIEVED is the difference between a fact about the
    source and a fact about this cache. ABSENT is stable: OQMD holds no entry for this
    formula, and it will still hold none tomorrow. NOT_RETRIEVED is an accident of one warm
    run — a 429, an exhausted daily budget, an offline query, a fetch that never happened.

    Collapsing the two is not cosmetic. Because missing criteria reduce ``data_coverage``
    and coverage scales the adjusted score, an incomplete warm is otherwise indistinguishable
    from a material with genuinely sparse data, and the ranking silently sorts on which
    fetches happened to finish. Scoring still treats both as "no value" (we do not know it
    either way); reporting, the refutation pass and the self-check do not.
    """

    KNOWN = "known"
    ABSENT = "absent"  # queried successfully; the source holds no record for this key
    NOT_RETRIEVED = (
        "not_retrieved"  # never successfully queried here: offline, failed, rate-limited, not yet run
    )
    NOT_APPLICABLE = "not_applicable"  # the criterion does not apply to this record

    @property
    def is_known(self) -> bool:
        return self is DataStatus.KNOWN

    @property
    def is_unknown(self) -> bool:
        """No usable value, for either reason. Use where the *reason* genuinely does not matter."""
        return self in (DataStatus.ABSENT, DataStatus.NOT_RETRIEVED)


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
    status: DataStatus = DataStatus.NOT_RETRIEVED
    provenance: Provenance | None = None


class BandGapRecord(BaseModel):
    value_ev: float | None = None
    functional: str | None = None  # e.g. GGA, GGA+U, r2SCAN, HSE06, unknown
    is_direct: bool | None = None
    status: DataStatus = DataStatus.NOT_RETRIEVED
    provenance: Provenance | None = None


class PropertyRecord(BaseModel):
    """The application figure of merit for one material: one property from one provider (see
    ``sources/properties.py``), with the same status vocabulary as every other field group.
    ``display`` and ``short`` are the provider's wording of the value for the audit view and
    the one-line rationale; ``absent_note`` says why there is no value when the source holds
    none. ``extras`` carries the companion numbers the provider returned (the electronic and
    ionic parts of a dielectric tensor, the Debye temperature beside a thermal conductivity)."""

    criterion: str = "figure_of_merit"
    property: str = ""
    label: str = "figure of merit"
    units: str = ""
    method: str | None = None
    value: float | None = None
    extras: dict[str, float] = Field(default_factory=dict)
    display: str | None = None
    short: str | None = None
    absent_note: str | None = None
    status: DataStatus = DataStatus.NOT_RETRIEVED
    provenance: Provenance | None = None


class CrossCheckRecord(BaseModel):
    """Independent stability value from a second database (OQMD)."""

    stability_ev_atom: float | None = None  # OQMD 'stability' = distance to hull
    formation_energy_ev_atom: float | None = None
    matched_formula: str | None = None
    status: DataStatus = DataStatus.NOT_RETRIEVED
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
    status: DataStatus = DataStatus.NOT_RETRIEVED
    provenance: Provenance | None = None


class HazardRecord(BaseModel):
    element_tiers: dict[str, int] = Field(default_factory=dict)
    element_basis: dict[str, str] = Field(default_factory=dict)
    worst_tier: int | None = None
    worst_elements: list[str] = Field(default_factory=list)
    table_version: str | None = None
    pubchem_cid: int | None = None
    ghs_hazard_codes: list[str] = Field(default_factory=list)
    pubchem_status: DataStatus = DataStatus.NOT_RETRIEVED
    status: DataStatus = DataStatus.NOT_RETRIEVED
    provenance: Provenance | None = None


class InterfaceRecord(BaseModel):
    """Thermodynamic stability of the oxide in contact with a substrate, computed from the
    convex hull of the oxide's elements plus the substrate's (see ``scoring/hull.py``).
    ``reaction_energy_ev_atom`` is the most exothermic reaction found, 0 when none."""

    substrate: str | None = None
    reaction_energy_ev_atom: float | None = None
    x_substrate: float | None = None  # atom fraction of substrate at the most exothermic point
    products: list[str] = Field(default_factory=list)  # hull phases the pair would form
    thermo_type: str | None = None
    n_phases: int | None = None  # stable phases in the hull the answer came from
    status: DataStatus = DataStatus.NOT_RETRIEVED
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
    figure_of_merit: PropertyRecord = Field(default_factory=PropertyRecord)
    cross_check: CrossCheckRecord = Field(default_factory=CrossCheckRecord)
    literature: LiteratureRecord = Field(default_factory=LiteratureRecord)
    hazard: HazardRecord = Field(default_factory=HazardRecord)
    interface: InterfaceRecord = Field(default_factory=InterfaceRecord)
    is_fixture: bool = False


# --------------------------------------------------------------------------------------
# Structured request (front edge output)
# --------------------------------------------------------------------------------------

TemplateName = Literal["pi_summary", "advanced", "audit", "json", "html"]


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
    families: list[str] = Field(default_factory=list)  # cation families in scope; empty = all
    # The substrate the interface criterion is computed against, when the request names one
    # ("on germanium"): an element or a hull-phase formula. None = the profile's substrate.
    substrate: str | None = None
    interpretation_notes: list[str] = Field(default_factory=list)
    # Clauses of the request no rule consumed: printed back as "not acted on" so a scientist
    # never has to guess whether an ask was honoured.
    unhandled: list[str] = Field(default_factory=list)

    @field_validator("substrate")
    @classmethod
    def _substrate_is_a_formula(cls, v: str | None) -> str | None:
        """A substrate is a chemical formula of real elements, never free text: it is used
        as a hull-phase key and printed into provenance notes."""
        if v is None:
            return None
        v = v.strip()
        try:
            elements = parse_formula(v)
        except ValueError as exc:
            raise ValueError(f"substrate must be a chemical formula, got {v!r}") from exc
        if unknown := sorted(set(elements) - SYMBOLS):
            raise ValueError(f"substrate {v!r} names unknown element(s): {', '.join(unknown)}")
        return v


class RequestBin(StrEnum):
    TRIAGE = "triage"  # ordinary request
    IMPOSSIBLE = "architecturally_impossible"  # Bin 1
    CONFIG_DEVIATION = "configuration_deviation"  # Bin 2 (proceed, surface loudly)
    INTEGRITY = "evidence_integrity_attack"  # Bin 3 (refuse)
    OVERRIDE = "override_attempt"  # Bin 0 (proceed; the request asks for a mode that does not exist)
    OUT_OF_SCOPE = "out_of_scope"  # not a materials-triage ask at all (decline, say what the tool does)
    HAZARD_POLICY = "hazard_policy"  # asks to lift an element the site never lifts by request (decline)


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


class PolymorphRef(BaseModel):
    """Another phase of the same compound that passed the gates and is collapsed under the
    leading row. Its own numbers are kept so a reader can see how the phases differ."""

    material_id: str
    crystal_system: str | None = None
    spacegroup_symbol: str | None = None
    energy_above_hull_ev_atom: float | None = None
    effective_band_gap_ev: float | None = None
    adjusted_score: float | None = None
    rank_by_material: int | None = None  # its rank before grouping, over materials


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
    missing_criteria: list[str] = Field(default_factory=list)  # absent + not-retrieved, for display
    absent_criteria: list[str] = Field(
        default_factory=list
    )  # the source has no record: a fact about the data
    not_retrieved_criteria: list[str] = Field(
        default_factory=list
    )  # never fetched here: a fact about the cache
    retrieval_gap: float = 0.0  # fraction of total weight that was never retrieved
    comparable: bool = True  # False when retrieval_gap > 0: this score is not on equal footing
    adjusted_score: float | None = None
    # Legacy wire name: a scoring-data coverage band, not scientific confidence.
    confidence: Literal["high", "medium", "low"] = "low"
    cross_source_agreement: Literal["agree", "disagree", "unavailable", "untested"] = "untested"
    caveats: list[Caveat] = Field(default_factory=list)
    rationale: str | None = None
    # Polymorph grouping (post-core, see oxide_triage.grouping): a compound occupies one row.
    tier: int | None = None  # 1 = within output.tie_band of the leader; ranks inside a tier are arbitrary
    rank_by_material: int | None = None  # rank before grouping, over materials
    polymorphs: list[PolymorphRef] = Field(default_factory=list)  # other phases collapsed here
    collapsed_under: str | None = None  # material_id of the leading phase, when this one is collapsed


class Deviation(BaseModel):
    """A configuration choice that departs from the base profile and must be shown."""

    code: str
    description: str
    origin: Literal["request", "profile", "cli", "site"]


class RetrievalCompleteness(BaseModel):
    """How much of the data the ranking *wanted* was actually retrieved into this cache.

    A ranking is only comparable across candidates when this is complete. When it is not,
    candidates whose fetches finished outrank equally good candidates whose fetches did not,
    for a reason that has nothing to do with the materials.
    """

    completeness: float  # fraction of scored criterion-weight across ranked candidates that was retrieved
    n_ranked: int
    n_fully_retrieved: int
    not_retrieved_by_criterion: dict[str, int] = Field(default_factory=dict)
    absent_by_criterion: dict[str, int] = Field(default_factory=dict)
    comparable: bool  # completeness >= the configured floor
    note: str


class FigureOfMeritInfo(BaseModel):
    """What the seventh criterion is in this run, for templates and the front end."""

    criterion: str
    label: str
    units: str = ""
    method: str = ""
    property: str = ""
    provider: str = ""
    prefer: str = "high"


class ScoringExplanation(BaseModel):
    formula: str
    missing_data_penalty: float
    missing_data_policy: str
    confidence_thresholds: dict[str, float]
    weights: dict[str, float]
    gates: dict[str, Any]
    figure_of_merit: FigureOfMeritInfo | None = None


class ScopeInfo(BaseModel):
    """Which part of the cached universe a run considered. Scope is not a gate: materials outside
    it are not excluded candidates, they were never candidates for this run."""

    families: list[str] = Field(default_factory=list)  # empty = every family
    n_universe: int = 0  # materials in the cache
    n_in_scope: int = 0  # materials whose cations all belong to a selected family


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
    # Passing phases collapsed under another row of the same compound (full objects, so
    # `explain` still works on them). Empty when output.group_polymorphs is off.
    collapsed_polymorphs: list[ScoredCandidate] = Field(default_factory=list)
    tie_band: float = 0.0  # output.tie_band in force; candidates within it of a tier's leader share the tier
    n_candidates_considered: int = 0
    scope: ScopeInfo | None = None
    retrieval: RetrievalCompleteness | None = None  # how much of the ranked set was actually fetched
    warnings: list[str] = Field(default_factory=list)
    # Parts of the request the run did not act on: unparsed clauses, capabilities the
    # deployment lacks, modes that do not exist. Shown on every output, so a request that was
    # only partly honoured never reads as if it were honoured in full.
    not_acted_on: list[str] = Field(default_factory=list)
    # Caveats every shortlisted candidate carries, said once for the run so each row's main
    # caveat can be the one specific to it. The per-candidate copies stay on the candidates.
    run_notes: list[Caveat] = Field(default_factory=list)
    llm_usage: dict[str, str] = Field(default_factory=dict)  # edge -> provider/model or "none"
    clarifications: list[str] = Field(default_factory=list)  # questions worth asking before running
    needs_confirmation: bool = False  # True when clarifications exist and the run was not confirmed
    selfcheck_status: str | None = None  # passed | failed | not_run | skipped
    acquisition_summary: dict[str, Any] | None = None  # last gap-filling pass on this cache
