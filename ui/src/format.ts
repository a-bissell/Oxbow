// Small formatting helpers. No arithmetic on scores beyond display rounding.

import type { ScoredCandidate, TriageResult } from "./types";

export const fmt = (x: number | null | undefined, digits = 3): string => (x == null ? "—" : x.toFixed(digits));
export const pct = (x: number | null | undefined): string => (x == null ? "—" : `${Math.round(x * 100)}%`);
export const fmtInt = (x: number | null | undefined): string => (x == null ? "—" : x.toLocaleString());

/** Chemical formula with subscripts: HfO2 -> Hf O₂ (as <sub>). Returns segments. */
export function formulaParts(formula: string): { text: string; sub: boolean }[] {
  const out: { text: string; sub: boolean }[] = [];
  for (const m of formula.matchAll(/(\d+(?:\.\d+)?)|([^\d]+)/g)) {
    if (m[1]) out.push({ text: m[1], sub: true });
    else if (m[2]) out.push({ text: m[2], sub: false });
  }
  return out;
}

export const CRITERIA_LABELS: Record<string, string> = {
  stability: "stability",
  band_gap: "band gap",
  dielectric: "dielectric",
  interface: "interface (vs substrate)",
  toxicity: "toxicity",
  simplicity: "simplicity",
  literature: "literature",
};

export const GATE_LABELS: Record<string, string> = {
  energy_above_hull: "energy above hull",
  effective_band_gap: "effective band gap",
  max_elements: "distinct elements",
  hazard: "hazard tier",
  exclude_elements: "excluded elements",
  include_elements: "required elements",
  band_gap: "band gap",
  stability: "stability",
};

export function gateLabel(gate: string): string {
  return GATE_LABELS[gate] ?? gate.replaceAll("_", " ");
}

export function allCandidates(r: TriageResult): ScoredCandidate[] {
  return [...r.shortlist, ...r.ranked_beyond_shortlist, ...r.excluded];
}

export function findCandidate(r: TriageResult, key: string): ScoredCandidate | undefined {
  const k = key.trim().toLowerCase();
  const all = allCandidates(r);
  const byId = all.find((s) => s.record.material_id.toLowerCase() === k);
  if (byId) return byId;
  const matches = all.filter((s) => s.record.formula.toLowerCase() === k);
  const ranked = matches.filter((s) => s.rank != null).sort((a, b) => (a.rank ?? 0) - (b.rank ?? 0));
  return ranked[0] ?? matches[0];
}

/** The caveats a row shows, most severe first, minus the codes already said once for the whole
 *  run (result.run_notes) and the fixture-data note. The list view leads with the first and keeps
 *  the rest as a count; the full text lives in the focus view. */
export function visibleCaveats(sc: ScoredCandidate, exclude: string[] = []) {
  const skip = new Set([...exclude, "fixture_data"]);
  return sc.caveats.filter((c) => !skip.has(c.code));
}

/** The rationale split into its clauses, so the list can lay them out as scannable tokens
 *  instead of one semicolon-joined line. The wording stays exactly as the server wrote it. */
export function rationaleFacts(rationale: string | null | undefined): string[] {
  if (!rationale) return [];
  return rationale
    .split(";")
    .map((s) => s.trim().replace(/\.$/, ""))
    .filter(Boolean);
}

export function timeAgo(iso: string): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const d = new Date(t);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

/** Rank movements between two results, for the diff card. */
export function diffResults(prev: TriageResult, next: TriageResult) {
  const rank = (r: TriageResult) => new Map([...r.shortlist, ...r.ranked_beyond_shortlist].map((s) => [s.record.material_id, s.rank ?? 0]));
  const name = new Map(allCandidates(prev).concat(allCandidates(next)).map((s) => [s.record.material_id, s.record.formula]));
  const pr = rank(prev);
  const nr = rank(next);
  const prevShort = prev.shortlist.map((s) => s.record.material_id);
  const nextShort = next.shortlist.map((s) => s.record.material_id);
  const entered = nextShort.filter((m) => !prevShort.includes(m)).map((m) => ({ id: m, formula: name.get(m) ?? m, from: pr.get(m) ?? null, to: nr.get(m) ?? null }));
  const left = prevShort.filter((m) => !nextShort.includes(m)).map((m) => ({ id: m, formula: name.get(m) ?? m, from: pr.get(m) ?? null, to: nr.get(m) ?? null }));
  const moved = nextShort
    .filter((m) => prevShort.includes(m) && pr.get(m) !== nr.get(m))
    .map((m) => ({ id: m, formula: name.get(m) ?? m, from: pr.get(m) ?? null, to: nr.get(m) ?? null }));
  return { entered, left, moved, unchanged: entered.length === 0 && left.length === 0 && moved.length === 0 };
}

/** Inline formatting for assistant prose: **bold**, `code`, paragraphs and simple lists. */
export function splitParagraphs(text: string): string[] {
  return text
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter(Boolean);
}
