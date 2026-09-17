// Types for the result objects and API payloads the front end renders. They mirror
// oxide_triage/schemas.py; only the fields the interface reads are declared.

export type DataStatus = "known" | "absent" | "not_retrieved" | "unknown" | string;

export interface Provenance {
  source: string;
  source_id?: string | null;
  retrieved_at?: string | null;
  url?: string | null;
  note?: string | null;
}

export interface CandidateRecord {
  material_id: string;
  formula: string;
  elements: string[];
  n_elements: number;
  crystal_system?: string | null;
  spacegroup_symbol?: string | null;
  theoretical?: boolean | null;
  stability: { energy_above_hull_ev_atom?: number | null; is_stable?: boolean | null; status: DataStatus; provenance?: Provenance | null };
  band_gap: { value_ev?: number | null; functional?: string | null; status: DataStatus; provenance?: Provenance | null };
  figure_of_merit: {
    criterion: string;
    property: string;
    label: string;
    units: string;
    method?: string | null;
    value?: number | null;
    extras: Record<string, number>;
    display?: string | null;
    short?: string | null;
    absent_note?: string | null;
    status: DataStatus;
    provenance?: Provenance | null;
  };
  cross_check: { stability_ev_atom?: number | null; matched_formula?: string | null; status: DataStatus; provenance?: Provenance | null };
  literature: {
    total_works?: number | null;
    thin_film_works?: number | null;
    sample_works: { work_id: string; title: string; year?: number | null; doi?: string | null }[];
    query_terms: string[];
    status: DataStatus;
    provenance?: Provenance | null;
  };
  hazard: {
    element_tiers: Record<string, number>;
    worst_tier?: number | null;
    worst_elements: string[];
    ghs_hazard_codes: string[];
    pubchem_status: DataStatus;
    status: DataStatus;
  };
  is_fixture: boolean;
}

/** What the seventh criterion is in a run: the profile's application figure of merit. */
export interface FigureOfMeritInfo {
  criterion: string;
  label: string;
  units?: string;
  method?: string;
  property?: string;
  provider?: string;
  prefer?: string;
}

export interface GateResult {
  gate: string;
  passed: boolean | null;
  threshold_label: string;
  observed_label: string;
  reason?: string | null;
}

export interface ComponentScore {
  criterion: string;
  weight: number;
  raw_label: string;
  normalized: number | null;
  contribution: number | null;
  status: DataStatus;
  notes: string[];
}

export interface Caveat {
  code: string;
  severity: "info" | "warning" | "critical";
  text: string;
  origin: "rule" | "llm";
}

export interface ScoredCandidate {
  rank: number | null;
  record: CandidateRecord;
  band_gap_assessment: {
    reported_ev: number | null;
    reported_functional: string | null;
    effective_ev: number | null;
    corrected: boolean;
    correction_strategy: string;
    correction_note: string;
  };
  gates: GateResult[];
  excluded: boolean;
  exclusion_reasons: string[];
  components: ComponentScore[];
  raw_score: number | null;
  data_coverage: number;
  missing_criteria: string[];
  absent_criteria: string[];
  not_retrieved_criteria: string[];
  retrieval_gap: number;
  comparable: boolean;
  adjusted_score: number | null;
  confidence: "high" | "medium" | "low";
  cross_source_agreement: "agree" | "disagree" | "unavailable" | "untested";
  caveats: Caveat[];
  rationale?: string | null;
  tier?: number | null;
  rank_by_material?: number | null;
  polymorphs?: PolymorphRef[];
  collapsed_under?: string | null;
}

export interface PolymorphRef {
  material_id: string;
  crystal_system?: string | null;
  spacegroup_symbol?: string | null;
  energy_above_hull_ev_atom?: number | null;
  effective_band_gap_ev?: number | null;
  adjusted_score?: number | null;
  rank_by_material?: number | null;
}

export interface Deviation {
  code: string;
  description: string;
  origin: "request" | "profile" | "cli";
}

export interface TriageResult {
  request_text: string;
  criteria: Record<string, unknown> & { families?: string[]; interpretation_notes?: string[] };
  guard: { proceed: boolean; findings: { bin: string; code: string; matched_text: string; explanation: string }[]; refusal_message?: string | null };
  not_acted_on?: string[];
  run_notes?: Caveat[];
  profile_name: string;
  config_hash: string;
  cache_fingerprint: string;
  generated_at: string;
  fixture_data: boolean;
  offline: boolean;
  deviations: Deviation[];
  scope_limitation: string;
  scoring: {
    formula: string;
    missing_data_penalty: number;
    missing_data_policy: string;
    weights: Record<string, number>;
    gates: Record<string, unknown>;
    figure_of_merit?: FigureOfMeritInfo | null;
  };
  shortlist: ScoredCandidate[];
  ranked_beyond_shortlist: ScoredCandidate[];
  excluded: ScoredCandidate[];
  collapsed_polymorphs?: ScoredCandidate[];
  tie_band?: number;
  n_candidates_considered: number;
  scope?: { families: string[]; n_universe: number; n_in_scope: number } | null;
  retrieval?: {
    completeness: number;
    n_ranked: number;
    n_fully_retrieved: number;
    not_retrieved_by_criterion: Record<string, number>;
    absent_by_criterion: Record<string, number>;
    comparable: boolean;
    note: string;
  } | null;
  warnings: string[];
  clarifications: string[];
  needs_confirmation: boolean;
  selfcheck_status?: string | null;
}

// ---- conversation ---------------------------------------------------------------------

export interface Focus {
  result_id: string;
  candidate: string;
}

export interface Scope {
  profile?: string | null;
  families?: string[] | null;
  top_k?: number | null;
  min_band_gap_ev?: number | null;
  max_energy_above_hull_ev_atom?: number | null;
  max_elements?: number | null;
}

export interface Step {
  tool: string;
  label: string;
  status: "running" | "done" | "failed" | "held";
  detail?: string | null;
  args: Record<string, unknown>;
  ms?: number | null;
}

export interface Pending {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  questions: string[];
  resolved?: "confirmed" | "dismissed" | null;
}

export interface Turn {
  id: string;
  role: "user" | "assistant";
  text: string;
  created_at: string;
  focus?: Focus | null;
  scope?: Scope | null;
  steps: Step[];
  result_id?: string | null;
  explain?: string | null;
  suggestions: string[];
  unverified?: string[];
  pending?: Pending | null;
  error?: string | null;
}

export interface Conversation {
  id: string;
  title: string;
  profile: string;
  created_at: string;
  updated_at: string;
  turns: Turn[];
  result_ids: string[];
  driver: "rules" | "model" | "claude";
}

export interface ConversationSummary {
  id: string;
  title: string;
  profile: string;
  created_at: string;
  updated_at: string;
  n_turns: number;
  driver: string;
}

export interface Family {
  id: string;
  name: string;
  rationale: string;
  cations: string[];
  n_any: number;
  n_only: number;
}

export interface ProfileSummary {
  name: string;
  description: string;
  top_k: number;
  gates: { max_energy_above_hull_ev_atom: number; min_band_gap_ev: number; max_elements: number; on_missing_stability: string; on_missing_band_gap: string };
  weights: Record<string, number>;
  default_families: string[];
}

export interface Status {
  cache: { path: string; sources: Record<string, { n: number; newest: string }>; fixture_data: boolean; empty: boolean; offline: boolean };
  selfcheck: { passed: boolean; inconclusive?: boolean; checked_at: string; details: string[] } | null;
  profiles: ProfileSummary[];
  families: Family[];
  n_universe: number;
  llm: { provider: string; model: string | null; driver: "rules" | "model"; agent_model: string | null };
  admin_editable: boolean;
  greetings: string[];
  suggested_requests: { label: string; text: string; profile?: string }[];
  scope_limitation: string;
}

// Server-sent events during a turn.
export type TurnEvent =
  | { type: "step"; step: Step; index: number }
  | { type: "progress"; stage: string; message: string; done: number | null; total: number | null }
  | { type: "text"; delta: string }
  | { type: "result"; result_id: string; previous_result_id?: string | null }
  | { type: "explain"; markdown: string }
  | { type: "focus"; result_id: string; candidate: string }
  | { type: "clarify"; pending: Pending }
  | { type: "notice"; message: string }
  | { type: "turn"; turn: Turn }
  | { type: "error"; message: string }
  | { type: "end" };

export interface JobState {
  id: string;
  kind: string;
  args: Record<string, unknown>;
  status: "queued" | "running" | "done" | "failed";
  started_at: string;
  finished_at?: string | null;
  log: string[];
  outcome?: unknown;
  error?: string | null;
}

// ---- platform-feedback aggregates (read-only reporting; nothing here changes a default) ----

export interface DeviationSummary {
  window: { since: string | null; until: string | null };
  n_runs: number;
  n_deviations: number;
  by_code: Record<string, number>;
  by_origin: Record<string, number>;
  by_profile: Record<string, number>;
  by_code_and_origin: { code: string; origin: string; count: number; profiles: string[]; last_ts: string | null; example: string | null }[];
}

export interface GapTally {
  candidates: number; // candidate-runs: the same material missing in ten runs counts ten
  runs: number; // runs in which at least one candidate was missing it
}

export interface RetrievalSummary {
  window: { since: string | null; until: string | null };
  n_runs: number;
  n_incomparable: number;
  mean_completeness: number | null;
  absent_by_criterion: Record<string, GapTally>;
  not_retrieved_by_criterion: Record<string, GapTally>;
}
