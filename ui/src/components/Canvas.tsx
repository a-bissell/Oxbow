// The results canvas: the state the conversation is about. Everything here renders from the
// stored result object; nothing is recomputed beyond display rounding.

import { useMemo, useState, type ReactNode } from "react";
import { api } from "../api";
import { CRITERIA_LABELS, allCandidates, diffResults, findCandidate, fmt, fmtInt, formulaParts, gateLabel, pct, rationaleFacts, registerFigureOfMerit, visibleCaveats } from "../format";
import { useApp, type View } from "../store";
import type { ScoredCandidate, TriageResult } from "../types";

export function Formula({ f }: { f: string }) {
  return (
    <span>
      {formulaParts(f).map((p, i) => (p.sub ? <sub key={i}>{p.text}</sub> : <span key={i}>{p.text}</span>))}
    </span>
  );
}

function Confidence({ sc }: { sc: ScoredCandidate }) {
  const cls = sc.confidence === "high" ? "chip--ok" : sc.confidence === "medium" ? "" : "chip--warn";
  return <span className={`chip chip--sm ${cls}`}>{sc.confidence} confidence</span>;
}

function MissingChips({ sc }: { sc: ScoredCandidate }) {
  return (
    <>
      {sc.absent_criteria.map((c) => (
        <span key={c} className="chip chip--sm chip--warn" title="The source holds no record: a fact about the data">
          no {CRITERIA_LABELS[c] ?? c} data
        </span>
      ))}
      {sc.not_retrieved_criteria.map((c) => (
        <span key={c} className="chip chip--sm chip--crit" title="Never fetched into this cache: fixable by warming">
          {CRITERIA_LABELS[c] ?? c} not retrieved
        </span>
      ))}
    </>
  );
}

// ---- header ----------------------------------------------------------------------------------

// A run-wide banner that shows only its headline until clicked, so the results start higher.
function CollapsibleBanner({ tone, title, noun, items }: { tone: "info" | "warn"; title: string; noun: string; items: ReactNode[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`banner banner--${tone} small`}>
      <button className="banner__summary" onClick={() => setOpen(!open)} aria-expanded={open}>
        <strong>{title}</strong>
        <span className="banner__count">
          {items.length} {items.length === 1 ? noun : `${noun}s`} {open ? "▴" : "▾"}
        </span>
      </button>
      {open && (
        <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
          {items.map((it, i) => (
            <li key={i}>{it}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Header({ result, rid, view, setView }: { result: TriageResult; rid: string; view: View; setView: (v: View) => void }) {
  // The profile's figure of merit renders under its label everywhere a criterion name appears.
  registerFigureOfMerit(result.scoring.figure_of_merit);
  const [showWarnings, setShowWarnings] = useState(false);
  const nPass = result.shortlist.length + result.ranked_beyond_shortlist.length;
  const scope = result.scope;
  const notices = result.warnings.filter((w) => !w.startsWith("Self-check has not"));
  const tabs: { key: Extract<View, { kind: "list" }>["tab"]; label: string }[] = [
    { key: "shortlist", label: `Shortlist · ${result.shortlist.length}` },
    { key: "beyond", label: `Ranked below · ${result.ranked_beyond_shortlist.length}` },
    { key: "excluded", label: `Excluded · ${result.excluded.length}` },
    { key: "gaps", label: "Data gaps" },
    { key: "scoring", label: "Scoring rules" },
  ];
  return (
    <div className="canvas__head">
      <div className="canvas__title">
        <div className="col" style={{ gap: 2 }}>
          <h2>
            {result.shortlist.length ? `Shortlist · ${result.shortlist.length} of ${nPass} passing` : "No shortlist served"}
          </h2>
          <div className="small muted">
            {result.profile_name} profile · {fmtInt(result.n_candidates_considered)} in scope
            {scope && scope.n_in_scope < scope.n_universe ? ` of ${fmtInt(scope.n_universe)} in the cache` : ""}
            {result.retrieval ? ` · retrieval ${pct(result.retrieval.completeness)} over the ranked pool` : ""} · result <span className="mono">{rid}</span>
          </div>
        </div>
        <div className="row">
          {/* The advanced summary and JSON stay on the API (/api/results/{id}/render/...) for the
              assistant, the CLI and scripts; the row shows only what a person downloads. */}
          <a className="btn btn--sm" href={api.renderUrl(rid, "pi_summary")} download>
            Summary
          </a>
          <a className="btn btn--sm" href={api.renderUrl(rid, "audit")} download>
            Audit
          </a>
          <a className="btn btn--sm" href={api.renderUrl(rid, "html")} download>
            Report
          </a>
        </div>
      </div>
      {result.fixture_data && <div className="banner banner--crit small">Synthetic fixture data. Every number here is illustrative.</div>}
      {result.retrieval && !result.retrieval.comparable && <div className="banner banner--warn small">{result.retrieval.note}</div>}
      {(result.run_notes ?? []).length > 0 && (
        <CollapsibleBanner
          tone="info"
          title="Applies to every shortlisted candidate"
          noun="note"
          items={(result.run_notes ?? []).map((c, i) => (
            <span key={i}>
              <span className={`sev sev--${c.severity}`}>{c.severity}</span> {c.text}
            </span>
          ))}
        />
      )}
      {(result.not_acted_on ?? []).length > 0 && (
        <CollapsibleBanner
          tone="warn"
          title="Parts of the request not acted on"
          noun="part"
          items={(result.not_acted_on ?? []).map((line, i) => (
            <span key={i}>{line}</span>
          ))}
        />
      )}
      {(result.deviations.length > 0 || notices.length > 0) && (
        <div className="wrap">
          {result.deviations.map((d, i) => (
            <span key={i} className="chip chip--warn chip--sm" title={d.description}>
              deviation · {d.description.length > 70 ? d.description.slice(0, 68) + "…" : d.description}
            </span>
          ))}
          {notices.length > 0 && (
            <button className="chip chip--btn chip--soft chip--sm" onClick={() => setShowWarnings(!showWarnings)}>
              {notices.length} {notices.length === 1 ? "note" : "notes"} {showWarnings ? "▴" : "▾"}
            </button>
          )}
        </div>
      )}
      {showWarnings && (
        <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
          {notices.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}
      <div className="tabs">
        {tabs.map((t) => (
          <button key={t.key} className={`tab ${view.kind === "list" && view.tab === t.key ? "tab--active" : ""}`} onClick={() => setView({ kind: "list", tab: t.key })}>
            {t.label}
          </button>
        ))}
      </div>
    </div>
  );
}

// ---- list views ------------------------------------------------------------------------------

function DiffCard({ prev, next, onShowPrev }: { prev: TriageResult; next: TriageResult; onShowPrev: () => void }) {
  const d = diffResults(prev, next);
  return (
    <div className="card diffcard">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="label" style={{ color: "var(--accent-ink)" }}>
          What changed since the previous result
        </span>
        <button className="btn btn--ghost btn--sm" onClick={onShowPrev}>
          Show previous
        </button>
      </div>
      {d.unchanged ? (
        <span className="small">The shortlist is unchanged.</span>
      ) : (
        <div className="wrap small">
          {d.entered.map((x) => (
            <span key={x.id} className="chip chip--ok chip--sm">
              <Formula f={x.formula} /> entered{x.from ? ` (was ${x.from})` : ""}
            </span>
          ))}
          {d.left.map((x) => (
            <span key={x.id} className="chip chip--warn chip--sm">
              <Formula f={x.formula} /> left{x.to ? ` (now ${x.to})` : " (excluded)"}
            </span>
          ))}
          {d.moved.map((x) => (
            <span key={x.id} className="chip chip--sm">
              <Formula f={x.formula} /> {x.from} → {x.to}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function CandidateCard({ sc, onOpen, focused, shared }: { sc: ScoredCandidate; onOpen: () => void; focused: boolean; shared: string[] }) {
  const r = sc.record;
  const cavs = visibleCaveats(sc, shared);
  const cav = cavs[0];
  const facts = rationaleFacts(sc.rationale);
  return (
    <button className={`card ccard ${focused ? "ccard--focus" : ""}`} onClick={onOpen}>
      <div className="ccard__rank">{sc.rank}</div>
      <div className="col" style={{ gap: 4, minWidth: 0 }}>
        <div className="wrap" style={{ alignItems: "center" }}>
          <span className="ccard__name">
            <Formula f={r.formula} />
          </span>
          <span className="ccard__meta">
            {r.material_id}
            {r.crystal_system ? ` · ${r.crystal_system}` : ""}
            {r.spacegroup_symbol ? ` ${r.spacegroup_symbol}` : ""}
          </span>
          <Confidence sc={sc} />
          {sc.tier != null && (
            <span className="chip chip--sm" title="Candidates within the tie band of a tier's leader share the tier; the order inside a tier is arbitrary.">
              tier {sc.tier}
            </span>
          )}
          {sc.polymorphs && sc.polymorphs.length > 0 && (
            <span className="chip chip--sm" title={`Other phases of this compound that passed the gates: ${sc.polymorphs.map((p) => p.material_id).join(", ")}. The leading phase's numbers are shown.`}>
              +{sc.polymorphs.length} phase{sc.polymorphs.length > 1 ? "s" : ""}
            </span>
          )}
          <MissingChips sc={sc} />
        </div>
        {facts.length > 0 && (
          <div className="ccard__facts">
            {facts.map((f, i) => (
              <span key={i}>{f}</span>
            ))}
          </div>
        )}
        {cav && (
          <div className={`ccard__caveat ${cav.severity === "critical" ? "ccard__caveat--crit" : ""}`}>
            <span className="ccard__caveat-txt">Caveat · {cav.text}</span>
            {cavs.length > 1 && <span className="ccard__caveat-more">+{cavs.length - 1} more</span>}
          </div>
        )}
      </div>
      <div className="col" style={{ gap: 4 }}>
        <div className="bar">
          <i style={{ width: `${Math.round(100 * (sc.adjusted_score ?? 0))}%` }} />
        </div>
        <div className="small muted">score {fmt(sc.adjusted_score)}</div>
      </div>
      <span className="btn btn--sm">Explain</span>
    </button>
  );
}

function CompactTable({ rows, onOpen, excluded }: { rows: ScoredCandidate[]; onOpen: (sc: ScoredCandidate) => void; excluded?: boolean }) {
  const [q, setQ] = useState("");
  const filtered = q ? rows.filter((s) => s.record.formula.toLowerCase().includes(q.toLowerCase()) || s.record.material_id.includes(q)) : rows;
  return (
    <div className="card panel">
      <input className="input input--sm" placeholder="Filter by formula or id" value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 260 }} />
      <div style={{ overflowX: "auto" }}>
        <table>
          <thead>
            <tr>
              {!excluded && <th className="num">Rank</th>}
              <th>Material</th>
              {excluded ? <th>Why it was excluded</th> : <th className="num">Score</th>}
              {!excluded && <th>Confidence</th>}
              {!excluded && <th>Missing</th>}
            </tr>
          </thead>
          <tbody>
            {filtered.slice(0, 300).map((s) => (
              <tr key={s.record.material_id} className="tbl-row" onClick={() => onOpen(s)}>
                {!excluded && <td className="num">{s.rank}</td>}
                <td>
                  <Formula f={s.record.formula} /> <span className="faint small">{s.record.material_id}</span>
                </td>
                {excluded ? <td className="small">{s.exclusion_reasons.join("; ")}</td> : <td className="num">{fmt(s.adjusted_score)}</td>}
                {!excluded && <td className="small">{s.confidence}</td>}
                {!excluded && <td className="small muted">{s.missing_criteria.map((c) => CRITERIA_LABELS[c] ?? c).join(", ") || "—"}</td>}
              </tr>
            ))}
          </tbody>
        </table>
        {filtered.length > 300 && <div className="small faint">Showing the first 300 of {filtered.length}.</div>}
      </div>
    </div>
  );
}

function GapsView({ result }: { result: TriageResult }) {
  const ranked = [...result.shortlist, ...result.ranked_beyond_shortlist];
  const crits = Object.keys(result.scoring.weights);
  const counts = crits.map((c) => {
    let known = 0;
    let absent = 0;
    let notRetrieved = 0;
    for (const s of ranked) {
      const comp = s.components.find((x) => x.criterion === c);
      if (!comp) continue;
      if (comp.status === "known") known++;
      else if (comp.status === "not_retrieved") notRetrieved++;
      else absent++;
    }
    return { c, known, absent, notRetrieved };
  });
  return (
    <div className="card panel">
      <span className="label">Which criteria had data behind them, across the {ranked.length} passing candidates</span>
      <table>
        <thead>
          <tr>
            <th>Criterion</th>
            <th className="num">Weight</th>
            <th className="num">Known</th>
            <th className="num">Absent at the source</th>
            <th className="num">Not retrieved here</th>
          </tr>
        </thead>
        <tbody>
          {counts.map((x) => (
            <tr key={x.c}>
              <td>{CRITERIA_LABELS[x.c] ?? x.c}</td>
              <td className="num">{fmt(result.scoring.weights[x.c], 2)}</td>
              <td className="num">{x.known}</td>
              <td className="num">{x.absent}</td>
              <td className="num" style={{ color: x.notRetrieved ? "var(--crit)" : undefined }}>
                {x.notRetrieved}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="small muted">
        Absent means the source answered and holds no record: a fact about the data. Not retrieved means this cache never downloaded it: fixable by warming the cache. Both earn no credit and lower confidence; only the second makes candidates non-comparable.
      </div>
      {result.retrieval && <div className="small muted">{result.retrieval.note}</div>}
    </div>
  );
}

function ScoringView({ result }: { result: TriageResult }) {
  const gates = result.scoring.gates as Record<string, unknown>;
  return (
    <div className="focus__grid">
      <div className="card panel">
        <span className="label">Weights</span>
        {Object.entries(result.scoring.weights).map(([k, w]) => (
          <div key={k} className="comp">
            <span>{CRITERIA_LABELS[k] ?? k}</span>
            <div className="bar">
              <i style={{ width: `${Math.round(100 * w)}%` }} />
            </div>
            <span className="comp__val">{fmt(w, 2)}</span>
          </div>
        ))}
        <div className="small muted">
          Rule: <code>{result.scoring.formula}</code>
        </div>
        <div className="small muted">
          Missing data policy: {result.scoring.missing_data_policy}, penalty {fmt(result.scoring.missing_data_penalty, 2)} per unit of missing weight.
        </div>
      </div>
      <div className="card panel">
        <span className="label">Gates</span>
        <table>
          <tbody>
            {Object.entries(gates).map(([k, v]) => (
              <tr key={k}>
                <td className="muted">{gateLabel(k)}</td>
                <td className="mono">{Array.isArray(v) ? (v.length ? v.join(", ") : "none") : String(v)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="small muted">{result.scope_limitation}</div>
        <div className="small faint">
          config {result.config_hash} · cache {result.cache_fingerprint.slice(0, 12)} · {result.generated_at}
        </div>
      </div>
    </div>
  );
}

// ---- focus ----------------------------------------------------------------------------------

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="card panel">
      <span className="label">{title}</span>
      {children}
    </div>
  );
}

function ProvenanceLine({ label, p }: { label: string; p?: { source: string; source_id?: string | null; retrieved_at?: string | null; url?: string | null; note?: string | null } | null }) {
  if (!p) return null;
  return (
    <div className="small">
      <span className="muted">{label}:</span> {p.source}
      {p.source_id ? ` ${p.source_id}` : ""}
      {p.retrieved_at ? ` · retrieved ${p.retrieved_at.slice(0, 10)}` : ""}
      {p.url ? (
        <>
          {" · "}
          <a href={p.url} target="_blank" rel="noreferrer">
            open
          </a>
        </>
      ) : null}
      {p.note ? <span className="faint"> · {p.note}</span> : null}
    </div>
  );
}

function FocusView({ result, rid, candidate }: { result: TriageResult; rid: string; candidate: string }) {
  const app = useApp();
  const sc = findCandidate(result, candidate);
  if (!sc) {
    return (
      <div className="canvas__body">
        <div className="banner banner--warn">No candidate {candidate} in this result.</div>
      </div>
    );
  }
  const r = sc.record;
  const nPass = result.shortlist.length + result.ranked_beyond_shortlist.length;
  const top = result.shortlist[0];
  const bg = sc.band_gap_assessment;
  const askAbout = () => {
    app.setFocus({ result_id: rid, candidate: r.formula });
    app.focusComposer();
  };
  return (
    <div className="canvas__body">
      <div className="row small">
        <a
          href="#shortlist"
          onClick={(e) => {
            e.preventDefault();
            app.setView({ kind: "list", tab: sc.excluded ? "excluded" : "shortlist" });
          }}
        >
          ← {sc.excluded ? "Excluded" : "Shortlist"}
        </a>
        <span className="faint">/ {r.formula}</span>
      </div>
      <div className="focus__head">
        <div className="col" style={{ gap: 4 }}>
          <div className="wrap" style={{ alignItems: "center" }}>
            <span className="focus__name">
              <Formula f={r.formula} />
            </span>
            <span className="muted">
              {r.material_id}
              {r.crystal_system ? ` · ${r.crystal_system}` : ""}
              {r.spacegroup_symbol ? ` ${r.spacegroup_symbol}` : ""}
            </span>
            {!sc.excluded && <Confidence sc={sc} />}
            {sc.excluded && <span className="chip chip--sm chip--crit">excluded</span>}
            {sc.collapsed_under && <span className="chip chip--sm chip--warn">collapsed under {sc.collapsed_under}</span>}
            {r.theoretical && <span className="chip chip--sm chip--warn">no observed structure</span>}
            <MissingChips sc={sc} />
          </div>
          <div className="small muted">
            {sc.excluded
              ? `Excluded by a gate: ${sc.exclusion_reasons.join("; ")}`
              : sc.collapsed_under
                ? `Passed the gates; another phase of ${r.formula} leads the row (ranked ${sc.rank_by_material ?? "?"} over materials before grouping) · adjusted score ${fmt(sc.adjusted_score, 4)} · raw ${fmt(sc.raw_score, 4)} on ${pct(sc.data_coverage)} coverage`
                : `Rank ${sc.rank} of ${nPass} passing compounds · adjusted score ${fmt(sc.adjusted_score, 4)} · raw ${fmt(sc.raw_score, 4)} on ${pct(sc.data_coverage)} coverage · cross-check ${sc.cross_source_agreement}`}
          </div>
        </div>
        <div className="row">
          {top && top.record.material_id !== r.material_id && (
            <button className="btn btn--sm" onClick={() => app.setView({ kind: "compare", keys: [r.formula, top.record.formula] })}>
              Compare with <Formula f={top.record.formula} />
            </button>
          )}
          <button className="btn btn--primary btn--sm" onClick={askAbout}>
            Ask about this
          </button>
        </div>
      </div>

      <div className="focus__grid">
        <Panel title="Score components">
          {sc.components.map((c) => (
            <div key={c.criterion} className={`comp ${c.status !== "known" ? "comp--unknown" : ""}`} title={c.notes.join(" ")}>
              <span>{CRITERIA_LABELS[c.criterion] ?? c.criterion}</span>
              <div className="bar">
                <i style={{ width: `${Math.round(100 * (c.normalized ?? 0))}%` }} />
              </div>
              <span className="comp__val">
                {c.contribution == null ? c.status.replace("_", " ") : fmt(c.contribution, 3)} · {c.raw_label}
              </span>
            </div>
          ))}
          <div className="small faint">Contribution = weight × normalised score. An unknown criterion earns nothing and is shown as unknown, never as zero.</div>
        </Panel>

        <Panel title="Gates">
          <table>
            <thead>
              <tr>
                <th>Gate</th>
                <th>Threshold</th>
                <th>Observed</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {sc.gates.map((g) => (
                <tr key={g.gate}>
                  <td>{gateLabel(g.gate)}</td>
                  <td className="small">{g.threshold_label}</td>
                  <td className="small">{g.observed_label}</td>
                  <td className={g.passed === true ? "pass" : g.passed === false ? "fail" : "indet"}>{g.passed === true ? "pass" : g.passed === false ? "FAIL" : "indeterminate"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="small muted">{bg.correction_note}</div>
        </Panel>

        {sc.polymorphs && sc.polymorphs.length > 0 && (
          <Panel title="Other phases of this compound · collapsed under this row">
            <table className="tbl">
              <thead>
                <tr><th>Material</th><th>Phase</th><th>E_hull (eV/atom)</th><th>Effective gap (eV)</th><th>Score</th><th>Rank over materials</th></tr>
              </thead>
              <tbody>
                {sc.polymorphs.map((p) => (
                  <tr key={p.material_id}>
                    <td className="mono">{p.material_id}</td>
                    <td>{[p.crystal_system, p.spacegroup_symbol].filter(Boolean).join(" ") || "?"}</td>
                    <td className="mono">{p.energy_above_hull_ev_atom == null ? "?" : p.energy_above_hull_ev_atom.toFixed(3)}</td>
                    <td className="mono">{p.effective_band_gap_ev == null ? "?" : p.effective_band_gap_ev.toFixed(2)}</td>
                    <td className="mono">{fmt(p.adjusted_score)}</td>
                    <td className="mono">{p.rank_by_material ?? "?"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="small muted">Which phase a deposited film adopts is not modelled. The numbers above this panel are for the leading phase.</div>
          </Panel>
        )}
        <Panel title="Caveats · the case against">
          {sc.caveats.filter((c) => c.code !== "fixture_data").length === 0 && <span className="small muted">No caveats raised.</span>}
          {sc.caveats
            .filter((c) => c.code !== "fixture_data")
            .map((c, i) => (
              <div key={i} className="caveat">
                <span className={`sev sev--${c.severity}`}>{c.severity}</span>
                <span>
                  {c.text} <span className="faint">({c.code}, {c.origin})</span>
                </span>
              </div>
            ))}
        </Panel>

        <Panel title="Provenance">
          <ProvenanceLine label="Stability" p={r.stability.provenance} />
          <ProvenanceLine label="Band gap" p={r.band_gap.provenance} />
          <div className="small">
            <span className="muted">Band gap:</span> reported {bg.reported_ev == null ? "—" : `${bg.reported_ev.toFixed(2)} eV (${bg.reported_functional ?? "unknown functional"})`}
            {bg.effective_ev != null ? `, effective ${bg.effective_ev.toFixed(2)} eV${bg.corrected ? " (corrected)" : ""}` : ""}
          </div>
          <ProvenanceLine label={r.figure_of_merit.label} p={r.figure_of_merit.provenance} />
          {r.figure_of_merit.status === "known" && (
            <div className="small">
              <span className="muted">{r.figure_of_merit.label}:</span>{" "}
              {r.figure_of_merit.display ?? `${fmt(r.figure_of_merit.value, 2)} ${r.figure_of_merit.units}`.trim()}
            </div>
          )}
          {r.figure_of_merit.status !== "known" && r.figure_of_merit.absent_note && (
            <div className="small muted">{r.figure_of_merit.label}: {r.figure_of_merit.absent_note}</div>
          )}
          <ProvenanceLine label="Cross-check" p={r.cross_check.provenance} />
          {r.cross_check.stability_ev_atom != null && (
            <div className="small">
              <span className="muted">OQMD hull distance:</span> {r.cross_check.stability_ev_atom.toFixed(3)} eV/atom{r.cross_check.matched_formula ? ` (matched ${r.cross_check.matched_formula})` : ""}
            </div>
          )}
          <ProvenanceLine label="Literature" p={r.literature.provenance} />
          {r.literature.status === "known" && (
            <div className="small">
              <span className="muted">Works:</span> {fmtInt(r.literature.total_works)} total, {fmtInt(r.literature.thin_film_works)} thin-film
              {r.literature.query_terms.length ? <span className="faint"> · searched {r.literature.query_terms.join(", ")}</span> : null}
            </div>
          )}
          {r.literature.sample_works.length > 0 && (
            <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
              {r.literature.sample_works.slice(0, 5).map((w) => (
                <li key={w.work_id}>
                  {w.doi ? (
                    <a href={`https://doi.org/${w.doi}`} target="_blank" rel="noreferrer">
                      {w.title}
                    </a>
                  ) : (
                    w.title
                  )}
                  {w.year ? <span className="faint"> ({w.year})</span> : null}
                </li>
              ))}
            </ul>
          )}
          <div className="small">
            <span className="muted">Hazard:</span> {r.hazard.worst_tier == null ? "—" : `worst tier ${r.hazard.worst_tier}`}
            {r.hazard.worst_elements.length ? ` (${r.hazard.worst_elements.join(", ")})` : ""}
            {r.hazard.ghs_hazard_codes.length ? ` · GHS ${r.hazard.ghs_hazard_codes.join(", ")}` : r.hazard.pubchem_status === "absent" ? " · no PubChem GHS record" : ""}
          </div>
        </Panel>
      </div>
    </div>
  );
}

// ---- compare --------------------------------------------------------------------------------

function CompareView({ result, keys }: { result: TriageResult; keys: string[] }) {
  const app = useApp();
  const picked = keys.map((k) => findCandidate(result, k)).filter((x): x is ScoredCandidate => !!x);
  if (picked.length < 2) {
    return (
      <div className="canvas__body">
        <div className="banner banner--warn">Could not find both candidates in this result.</div>
      </div>
    );
  }
  const crits = Array.from(new Set(picked.flatMap((s) => s.components.map((c) => c.criterion))));
  const gates = Array.from(new Set(picked.flatMap((s) => s.gates.map((g) => g.gate))));
  const best = (vals: (number | null)[]) => {
    const known = vals.filter((v): v is number => v != null);
    return known.length ? Math.max(...known) : null;
  };
  return (
    <div className="canvas__body">
      <div className="row small">
        <a
          href="#shortlist"
          onClick={(e) => {
            e.preventDefault();
            app.setView({ kind: "list", tab: "shortlist" });
          }}
        >
          ← Shortlist
        </a>
        <span className="faint">/ compare</span>
      </div>
      <div className="card panel">
        <div style={{ overflowX: "auto" }}>
          <table className="cmp-table">
            <thead>
              <tr>
                <th></th>
                {picked.map((s) => (
                  <th key={s.record.material_id} style={{ textTransform: "none", fontSize: 14, color: "var(--text)" }}>
                    <button className="btn btn--ghost btn--sm" onClick={() => app.setView({ kind: "focus", candidate: s.record.material_id })}>
                      <Formula f={s.record.formula} />
                    </button>
                    <div className="faint small">{s.record.material_id}</div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>rank</td>
                {picked.map((s) => (
                  <td key={s.record.material_id}>{s.rank ?? "excluded"}</td>
                ))}
              </tr>
              <tr>
                <td>adjusted score</td>
                {picked.map((s) => (
                  <td key={s.record.material_id} className={s.adjusted_score != null && s.adjusted_score === best(picked.map((x) => x.adjusted_score)) ? "cmp-lead" : ""}>
                    {fmt(s.adjusted_score, 4)}
                  </td>
                ))}
              </tr>
              <tr>
                <td>confidence · coverage</td>
                {picked.map((s) => (
                  <td key={s.record.material_id}>
                    {s.confidence} · {pct(s.data_coverage)}
                  </td>
                ))}
              </tr>
              {crits.map((c) => {
                const vals = picked.map((s) => s.components.find((x) => x.criterion === c)?.contribution ?? null);
                const lead = best(vals);
                return (
                  <tr key={c}>
                    <td>{CRITERIA_LABELS[c] ?? c}</td>
                    {picked.map((s, i) => {
                      const comp = s.components.find((x) => x.criterion === c);
                      return (
                        <td key={s.record.material_id} className={vals[i] != null && vals[i] === lead && vals.filter((v) => v === lead).length === 1 ? "cmp-lead" : ""}>
                          {comp ? (comp.contribution == null ? `${comp.raw_label} (${comp.status.replace("_", " ")})` : `${comp.raw_label} → ${fmt(comp.contribution, 4)}`) : "—"}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
              {gates.map((g) => (
                <tr key={g}>
                  <td>gate · {gateLabel(g)}</td>
                  {picked.map((s) => {
                    const x = s.gates.find((y) => y.gate === g);
                    return (
                      <td key={s.record.material_id} className="small">
                        {x ? (
                          <>
                            {x.observed_label} · <span className={x.passed === true ? "pass" : x.passed === false ? "fail" : "indet"}>{x.passed === true ? "pass" : x.passed === false ? "FAIL" : "indeterminate"}</span>
                          </>
                        ) : (
                          "—"
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="small muted">Bold marks the higher contribution on each row. Contributions are weight × normalised score as stored on the result.</div>
      </div>
      {picked.map((s) => {
        const cav = s.caveats.filter((c) => c.code !== "fixture_data").slice(0, 3);
        return cav.length ? (
          <div key={s.record.material_id} className="card panel">
            <span className="label">
              <Formula f={s.record.formula} /> · caveats
            </span>
            {cav.map((c, i) => (
              <div key={i} className="caveat">
                <span className={`sev sev--${c.severity}`}>{c.severity}</span>
                <span>{c.text}</span>
              </div>
            ))}
          </div>
        ) : null;
      })}
    </div>
  );
}

// ---- canvas -----------------------------------------------------------------------------------

export default function Canvas() {
  const app = useApp();
  const rid = app.currentResultId;
  const result = rid ? app.results[rid] : null;
  const prevId = rid ? app.prevOf[rid] : undefined;
  const prev = prevId ? app.results[prevId] : undefined;
  const view = app.view;
  const focusedId = useMemo(() => (view.kind === "focus" && result ? findCandidate(result, view.candidate)?.record.material_id : undefined), [view, result]);

  if (!rid || !result) {
    return (
      <section className="canvas">
        <div className="canvas__empty">
          <div className="col" style={{ alignItems: "center", gap: 6 }}>
            <span>{app.busy ? "Ranking… the shortlist appears here when the run finishes." : "Results appear here. Click a candidate to focus on it."}</span>
          </div>
        </div>
      </section>
    );
  }

  const open = (sc: ScoredCandidate) => {
    app.setView({ kind: "focus", candidate: sc.record.material_id });
    app.setFocus({ result_id: rid, candidate: sc.record.formula });
  };

  if (!result.guard.proceed || (result.shortlist.length === 0 && result.ranked_beyond_shortlist.length === 0)) {
    return (
      <section className="canvas">
        <Header result={result} rid={rid} view={view} setView={app.setView} />
        <div className="canvas__body">
          <div className="banner banner--warn">{result.warnings[0] ?? "No ranking was served."}</div>
          {result.guard.findings.map((f, i) => (
            <div key={i} className="card panel small">
              <span className="label">{f.bin.replaceAll("_", " ")}</span>
              <span>
                “{f.matched_text}” — {f.explanation}
              </span>
            </div>
          ))}
          {view.kind === "list" && view.tab === "excluded" && <CompactTable rows={result.excluded} onOpen={open} excluded />}
        </div>
      </section>
    );
  }

  return (
    <section className="canvas">
      {view.kind === "list" && <Header result={result} rid={rid} view={view} setView={app.setView} />}
      {view.kind === "focus" && <FocusView result={result} rid={rid} candidate={view.candidate} />}
      {view.kind === "compare" && <CompareView result={result} keys={view.keys} />}
      {view.kind === "list" && (
        <div className="canvas__body">
          {view.tab === "shortlist" && (
            <>
              {prev && prevId && <DiffCard prev={prev} next={result} onShowPrev={() => void app.showResult(prevId)} />}
              {result.shortlist.map((sc) => (
                <CandidateCard key={sc.record.material_id} sc={sc} onOpen={() => open(sc)} focused={sc.record.material_id === focusedId} shared={(result.run_notes ?? []).map((c) => c.code)} />
              ))}
              {result.ranked_beyond_shortlist.length > 0 && (
                <div className="row small muted" style={{ justifyContent: "space-between", padding: "4px 4px" }}>
                  <span>
                    {allCandidates(result).length.toLocaleString()} candidates considered · {result.excluded.length.toLocaleString()} excluded by a gate
                  </span>
                  <a
                    href="#beyond"
                    onClick={(e) => {
                      e.preventDefault();
                      app.setView({ kind: "list", tab: "beyond" });
                    }}
                  >
                    Show ranks {result.shortlist.length + 1} to {result.shortlist.length + result.ranked_beyond_shortlist.length}
                  </a>
                </div>
              )}
            </>
          )}
          {view.tab === "beyond" && <CompactTable rows={result.ranked_beyond_shortlist} onOpen={open} />}
          {view.tab === "excluded" && <CompactTable rows={result.excluded} onOpen={open} excluded />}
          {view.tab === "gaps" && <GapsView result={result} />}
          {view.tab === "scoring" && <ScoringView result={result} />}
        </div>
      )}
    </section>
  );
}
