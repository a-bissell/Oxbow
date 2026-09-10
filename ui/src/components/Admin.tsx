// The admin panel. Edits are written to the site overlay (config/site.yaml); the shipped YAML
// is never rewritten, and every field shows the shipped value when it differs.

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api";
import { CRITERIA_LABELS } from "../format";
import { useApp } from "../store";
import type { JobState } from "../types";

type Json = Record<string, any>;

const PAGES: { key: string; label: string }[] = [
  { key: "profiles", label: "Profiles" },
  { key: "universe", label: "Universe" },
  { key: "model", label: "Language model" },
  { key: "data", label: "Data & cache" },
  { key: "sources", label: "Sources & limits" },
  { key: "deviations", label: "Deviations log" },
];

function get(obj: Json | null | undefined, path: string): any {
  return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj as any);
}

function set(obj: Json, path: string, value: unknown): Json {
  const keys = path.split(".");
  const out: Json = { ...obj };
  let cur: Json = out;
  keys.forEach((k, i) => {
    if (i === keys.length - 1) cur[k] = value;
    else {
      cur[k] = { ...(cur[k] ?? {}) };
      cur = cur[k];
    }
  });
  return out;
}

/** Nested diff of `edited` against `base`: only the leaves that differ, as an overlay. */
function diff(edited: Json, base: Json): Json {
  const out: Json = {};
  for (const k of Object.keys(edited)) {
    const a = edited[k];
    const b = base?.[k];
    if (a && typeof a === "object" && !Array.isArray(a)) {
      const d = diff(a, b ?? {});
      if (Object.keys(d).length) out[k] = d;
    } else if (JSON.stringify(a) !== JSON.stringify(b)) out[k] = a;
  }
  return out;
}

function deepMerge(a: Json, b: Json): Json {
  const out: Json = { ...a };
  for (const k of Object.keys(b)) {
    out[k] = b[k] && typeof b[k] === "object" && !Array.isArray(b[k]) && out[k] && typeof out[k] === "object" ? deepMerge(out[k], b[k]) : b[k];
  }
  return out;
}

function FieldRow({ label, help, shipped, children }: { label: string; help?: string; shipped?: string; children: ReactNode }) {
  return (
    <div className="frow">
      <div className="frow__label">
        <span>{label}</span>
        {help && <span className="frow__help">{help}</span>}
        {shipped && <span className="frow__ship">shipped: {shipped}</span>}
      </div>
      {children}
    </div>
  );
}

function Num({ value, onChange, step, min, max }: { value: number | null | undefined; onChange: (v: number) => void; step?: number; min?: number; max?: number }) {
  return <input className="input input--sm" type="number" value={value ?? ""} step={step} min={min} max={max} onChange={(e) => onChange(Number(e.target.value))} />;
}

function Select({ value, options, onChange }: { value: string; options: string[]; onChange: (v: string) => void }) {
  return (
    <select className="input input--sm" value={value} onChange={(e) => onChange(e.target.value)}>
      {options.map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  );
}

function ListInput({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  const [text, setText] = useState(value.join(", "));
  useEffect(() => setText(value.join(", ")), [value.join(",")]);
  return (
    <input
      className="input input--sm"
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={() =>
        onChange(
          text
            .split(/[,\s]+/)
            .map((s) => s.trim())
            .filter(Boolean),
        )
      }
    />
  );
}

// ---- config editing shared by several pages -------------------------------------------------

function EditGate({ data }: { data: Awaited<ReturnType<typeof api.admin.config>> | null }) {
  if (!data || data.editable) return null;
  return (
    <div className="banner banner--warn small">
      Editing is off. Set <span className="kbd">OXIDE_TRIAGE_ADMIN=1</span> in the environment and restart the server to save changes here
      {data.overlay_path ? "" : " (the site file is also disabled: OXIDE_TRIAGE_SITE_CONFIG=off)"}. Values are shown as they are in force.
    </div>
  );
}

function useConfig(profile: string) {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.admin.config>> | null>(null);
  const [edited, setEdited] = useState<Json | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const load = useCallback(async () => {
    const d = await api.admin.config(profile);
    setData(d);
    setEdited(d.effective);
    setMessage(null);
  }, [profile]);
  useEffect(() => {
    void load();
  }, [load]);
  const update = (path: string, value: unknown) => setEdited((e) => (e ? set(e, path, value) : e));
  const dirty = !!(data && edited && Object.keys(diff(edited, data.effective)).length);
  const save = async (target: "profile" | "base") => {
    if (!data || !edited) return;
    setSaving(true);
    try {
      const overlay = { base: { ...(data.overlay.base ?? {}) }, profiles: { ...(data.overlay.profiles ?? {}) } } as { base: Json; profiles: Json };
      delete (overlay as Json).version;
      if (target === "profile") {
        // The overlay for this profile is everything that differs from the shipped profile.
        const shippedWithBase = deepMerge(data.shipped, overlay.base);
        const d = diff(edited, shippedWithBase);
        if (Object.keys(d).length) overlay.profiles[profile] = d;
        else delete overlay.profiles[profile];
      } else {
        overlay.base = deepMerge(overlay.base, diff(edited, data.effective));
      }
      await api.admin.saveOverlay(overlay);
      await load();
      setMessage("Saved to " + data.overlay_path);
    } catch (e) {
      setMessage(`Not saved: ${(e as Error).message}`);
    } finally {
      setSaving(false);
    }
  };
  const resetProfile = async () => {
    if (!data) return;
    const overlay = { base: { ...(data.overlay.base ?? {}) }, profiles: { ...(data.overlay.profiles ?? {}) } };
    delete overlay.profiles[profile];
    await api.admin.saveOverlay(overlay);
    await load();
  };
  return { data, edited, update, dirty, save, saving, message, resetProfile, reload: load };
}

function shippedLabel(data: Json | null, edited: Json | null, path: string): string | undefined {
  if (!data || !edited) return undefined;
  const s = get(data, path);
  const e = get(edited, path);
  return JSON.stringify(s) !== JSON.stringify(e) ? (Array.isArray(s) ? s.join(", ") || "none" : String(s)) : undefined;
}

function lockedLabel(data: { env_locked: Record<string, string> } | null, path: string): string | undefined {
  const v = data?.env_locked?.[path];
  return v ? `set by ${v} in the environment; the site file cannot change it` : undefined;
}

// ---- pages ---------------------------------------------------------------------------------------

function ProfilesPage() {
  const app = useApp();
  const [profile, setProfile] = useState("default");
  const cfg = useConfig(profile);
  const { data, edited, update } = cfg;
  const shipped = data?.shipped ?? null;
  const sh = (p: string) => shippedLabel(shipped, edited, p);
  const weights: Json = edited?.weights ?? {};
  const total = Object.values(weights).reduce((a: number, b: any) => a + Number(b || 0), 0) || 1;
  return (
    <>
      <div className="admin__title">
        <div className="col" style={{ gap: 2 }}>
          <h2>Profiles</h2>
          <span className="muted small">A profile is the policy a run is judged against. Anything a request changes at run time is reported as a deviation from it.</span>
        </div>
        <div className="row">
          {data?.overlay.profiles?.[profile] && (
            <button className="btn" onClick={() => void cfg.resetProfile().then(app.refreshStatus)}>
              Reset to shipped
            </button>
          )}
          <button className="btn" onClick={() => void cfg.reload()} disabled={!cfg.dirty}>
            Discard
          </button>
          <button className="btn btn--primary" onClick={() => void cfg.save("profile").then(app.refreshStatus)} disabled={!cfg.dirty || cfg.saving || !cfg.data?.editable}>
            {cfg.saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
      <div className="wrap">
        {(data?.profiles ?? ["default"]).map((p) => (
          <button key={p} className={`chip chip--btn ${p === profile ? "chip--ctx" : ""}`} onClick={() => setProfile(p)}>
            {p}
            {data?.overlay.profiles?.[p] ? " •" : ""}
          </button>
        ))}
      </div>
      <EditGate data={data} />
      {cfg.message && <div className={`banner small ${cfg.message.startsWith("Not") ? "banner--crit" : "banner--info"}`}>{cfg.message}</div>}
      {edited && (
        <div className="grid2">
          <div className="col" style={{ gap: 12 }}>
            <div className="card panel">
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="label">Weights · normalised at load</span>
                <span className="small muted">sum {Number(total).toFixed(2)}</span>
              </div>
              {Object.keys(CRITERIA_LABELS).map((k) => (
                <FieldRow key={k} label={CRITERIA_LABELS[k]} shipped={sh(`weights.${k}`)} help={`${((100 * Number(weights[k] || 0)) / Number(total)).toFixed(0)}% of the score`}>
                  <Num value={weights[k]} step={0.05} min={0} onChange={(v) => update(`weights.${k}`, v)} />
                </FieldRow>
              ))}
            </div>
            <div className="card panel">
              <span className="label">Gates</span>
              <FieldRow label="Max energy above hull (eV/atom)" shipped={sh("gates.max_energy_above_hull_ev_atom")}>
                <Num value={get(edited, "gates.max_energy_above_hull_ev_atom")} step={0.01} min={0} onChange={(v) => update("gates.max_energy_above_hull_ev_atom", v)} />
              </FieldRow>
              <FieldRow label="Min effective band gap (eV)" help="Applied to the corrected gap" shipped={sh("gates.min_band_gap_ev")}>
                <Num value={get(edited, "gates.min_band_gap_ev")} step={0.5} min={0} onChange={(v) => update("gates.min_band_gap_ev", v)} />
              </FieldRow>
              <FieldRow label="Max distinct elements" shipped={sh("gates.max_elements")}>
                <Num value={get(edited, "gates.max_elements")} min={2} max={6} onChange={(v) => update("gates.max_elements", v)} />
              </FieldRow>
              <FieldRow label="When stability is missing" shipped={sh("gates.on_missing_stability")}>
                <Select value={get(edited, "gates.on_missing_stability")} options={["exclude", "flag"]} onChange={(v) => update("gates.on_missing_stability", v)} />
              </FieldRow>
              <FieldRow label="When band gap is missing" shipped={sh("gates.on_missing_band_gap")}>
                <Select value={get(edited, "gates.on_missing_band_gap")} options={["exclude", "flag"]} onChange={(v) => update("gates.on_missing_band_gap", v)} />
              </FieldRow>
            </div>
            <div className="card panel">
              <span className="label">Output</span>
              <FieldRow label="Shortlist length" shipped={sh("output.top_k")}>
                <Num value={get(edited, "output.top_k")} min={1} max={50} onChange={(v) => update("output.top_k", v)} />
              </FieldRow>
              <FieldRow label="Default template" shipped={sh("output.default_template")}>
                <Select value={get(edited, "output.default_template")} options={["pi_summary", "audit", "json", "html"]} onChange={(v) => update("output.default_template", v)} />
              </FieldRow>
            </div>
          </div>
          <div className="col" style={{ gap: 12 }}>
            <div className="card panel">
              <span className="label">Band gap correction</span>
              <FieldRow label="Strategy" help="DFT gaps underestimate experiment by 30 to 50%" shipped={sh("band_gap.correction.strategy")}>
                <Select value={get(edited, "band_gap.correction.strategy")} options={["none", "scalar_factor", "hse_preferred"]} onChange={(v) => update("band_gap.correction.strategy", v)} />
              </FieldRow>
              <FieldRow label="Scalar factor for GGA / r2SCAN" shipped={sh("band_gap.correction.scalar_factor")}>
                <Num value={get(edited, "band_gap.correction.scalar_factor")} step={0.1} min={0.1} onChange={(v) => update("band_gap.correction.scalar_factor", v)} />
              </FieldRow>
              <FieldRow label="Fallback when no HSE value" shipped={sh("band_gap.correction.fallback")}>
                <Select value={get(edited, "band_gap.correction.fallback")} options={["scalar_factor", "none"]} onChange={(v) => update("band_gap.correction.fallback", v)} />
              </FieldRow>
              <FieldRow label="Preference saturates at (eV)" help="Score reaches 1.0 at this effective gap" shipped={sh("band_gap.preference.ideal_ev")}>
                <Num value={get(edited, "band_gap.preference.ideal_ev")} step={0.5} min={0.5} onChange={(v) => update("band_gap.preference.ideal_ev", v)} />
              </FieldRow>
            </div>
            <div className="card panel">
              <span className="label">Dielectric and stability curves</span>
              <FieldRow label="Dielectric scores 0 at ε ≤" shipped={sh("dielectric.low")}>
                <Num value={get(edited, "dielectric.low")} step={1} min={0} onChange={(v) => update("dielectric.low", v)} />
              </FieldRow>
              <FieldRow label="Dielectric scores 1 at ε ≥" help="Saturation is deliberate: k of 20 to 30 is the useful gate-stack range" shipped={sh("dielectric.high")}>
                <Num value={get(edited, "dielectric.high")} step={1} min={1} onChange={(v) => update("dielectric.high", v)} />
              </FieldRow>
              <FieldRow label="Stability score reaches 0 at E_hull (eV/atom)" shipped={sh("stability.zero_score_at_ev_atom")}>
                <Num value={get(edited, "stability.zero_score_at_ev_atom")} step={0.01} min={0.001} onChange={(v) => update("stability.zero_score_at_ev_atom", v)} />
              </FieldRow>
              <FieldRow label="Cross-check tolerance (eV/atom)" shipped={sh("stability.cross_check_tolerance_ev_atom")}>
                <Num value={get(edited, "stability.cross_check_tolerance_ev_atom")} step={0.01} min={0.001} onChange={(v) => update("stability.cross_check_tolerance_ev_atom", v)} />
              </FieldRow>
            </div>
            <div className="card panel">
              <span className="label">Hazards</span>
              <FieldRow label="Blocked hazard tiers" help="Elements in these tiers fail the hazard gate" shipped={sh("toxicity.blocklist_tiers")}>
                <ListInput value={(get(edited, "toxicity.blocklist_tiers") ?? []).map(String)} onChange={(v) => update("toxicity.blocklist_tiers", v.map(Number).filter((n) => !Number.isNaN(n)))} />
              </FieldRow>
              <FieldRow label="Extra blocked elements" shipped={sh("toxicity.element_blocklist")}>
                <ListInput value={get(edited, "toxicity.element_blocklist") ?? []} onChange={(v) => update("toxicity.element_blocklist", v)} />
              </FieldRow>
              <FieldRow label="Permitted despite tier" help="Shown as a deviation on every run" shipped={sh("toxicity.element_allowlist")}>
                <ListInput value={get(edited, "toxicity.element_allowlist") ?? []} onChange={(v) => update("toxicity.element_allowlist", v)} />
              </FieldRow>
            </div>
            <div className="card panel">
              <span className="label">Missing data</span>
              <FieldRow label="Policy" help="no_credit: an unknown criterion earns nothing. renormalize can help a candidate by having less data." shipped={sh("missing_data.policy")}>
                <Select value={get(edited, "missing_data.policy")} options={["no_credit", "renormalize"]} onChange={(v) => update("missing_data.policy", v)} />
              </FieldRow>
              <FieldRow label="Penalty per unit of missing weight" shipped={sh("missing_data.penalty")}>
                <Num value={get(edited, "missing_data.penalty")} step={0.01} min={0} max={1} onChange={(v) => update("missing_data.penalty", v)} />
              </FieldRow>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function UniversePage() {
  const app = useApp();
  const cfg = useConfig("default");
  const { data, edited, update } = cfg;
  const s = app.status;
  const families = s?.families ?? [];
  const selected: string[] = get(edited, "candidates.default_families") ?? [];
  const all = selected.length === 0;
  const [formula, setFormula] = useState("");
  const [job, setJob] = useState<JobState | null>(null);
  useJobPolling(job, setJob, () => void app.refreshStatus());
  const toggle = (id: string) => {
    const base = all ? families.map((f) => f.id) : selected;
    const next = base.includes(id) ? base.filter((x) => x !== id) : [...base, id];
    update("candidates.default_families", next.length === families.length ? [] : next);
  };
  const inScope = useMemo(() => (all ? s?.n_universe : undefined), [all, s?.n_universe]);
  return (
    <>
      <div className="admin__title">
        <div className="col" style={{ gap: 2 }}>
          <h2>Universe</h2>
          <span className="muted small">Which materials the assistant can rank. Families narrow the cached universe at query time; the allowlist decides what gets fetched.</span>
        </div>
        <div className="row">
          <button className="btn" onClick={() => void cfg.reload()} disabled={!cfg.dirty}>
            Discard
          </button>
          <button className="btn btn--primary" onClick={() => void cfg.save("base").then(app.refreshStatus)} disabled={!cfg.dirty || cfg.saving || !cfg.data?.editable}>
            {cfg.saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
      <EditGate data={data} />
      {cfg.message && <div className={`banner small ${cfg.message.startsWith("Not") ? "banner--crit" : "banner--info"}`}>{cfg.message}</div>}
      <div className="grid2" style={{ gridTemplateColumns: "minmax(0, 1fr) 320px" }}>
        <div className="col" style={{ gap: 10 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className="label">
              Families in scope by default · {all ? `all ${families.length}` : `${selected.length} of ${families.length}`}
              {inScope != null ? ` · ${inScope.toLocaleString()} materials` : ""}
            </span>
            <button className="btn btn--ghost btn--sm" onClick={() => update("candidates.default_families", [])}>
              Select all
            </button>
          </div>
          {families.map((f) => {
            const on = all || selected.includes(f.id);
            return (
              <label key={f.id} className={`card famrow ${on ? "" : "famrow--off"}`} style={{ cursor: "pointer" }}>
                <input type="checkbox" checked={on} onChange={() => toggle(f.id)} />
                <div>
                  <div style={{ fontWeight: 500 }}>{f.name}</div>
                  <div className="small muted">{f.cations.join(" ")}</div>
                </div>
                <div className="small muted">{f.rationale}</div>
                <div className="small num" title="materials containing a cation of this family / materials made only of this family's cations">
                  {f.n_any.toLocaleString()} <span className="faint">/ {f.n_only.toLocaleString()}</span>
                </div>
              </label>
            );
          })}
          <div className="small faint">
            Counts: materials with any cation from the family, and materials made only of that family's cations. A material is in scope when every cation in it belongs to a selected family. Users can widen or narrow this per request from the scope strip; this is only the default.
          </div>
          <div className="card famrow famrow--off" style={{ borderStyle: "dashed" }}>
            <input type="checkbox" disabled />
            <div>
              <div style={{ fontWeight: 500 }}>Wide list (alkalis, late transition metals, B, P, …)</div>
              <div className="small muted">cation_allowlist_wide.yaml</div>
            </div>
            <div className="small muted">Not in the cache. Switching the allowlist file needs a re-warm from Materials Project, a few minutes with a key. Set <code>candidates.cation_allowlist_file</code> in the site overlay and warm the cache.</div>
            <div className="small num muted">~4,700</div>
          </div>
        </div>
        <div className="col" style={{ gap: 12 }}>
          <div className="card panel">
            <span className="label">Fetch bounds · need a re-warm to change</span>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="small">Observed structures only</span>
              <span className="kbd">{String(get(data?.effective, "candidates.observed_only"))}</span>
            </div>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="small">Hull ceiling</span>
              <span className="kbd">{get(data?.effective, "candidates.energy_above_hull_ceiling_ev_atom")} eV/atom</span>
            </div>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="small">Min reported gap</span>
              <span className="kbd">{get(data?.effective, "candidates.min_reported_gap_ev")} eV</span>
            </div>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="small">Max elements fetched</span>
              <span className="kbd">{get(data?.effective, "candidates.max_elements_query")}</span>
            </div>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="small">Allowlist file</span>
              <span className="kbd">{get(data?.effective, "candidates.cation_allowlist_file")}</span>
            </div>
          </div>
          <div className="card panel">
            <span className="label">Add a compound</span>
            <div className="row">
              <input className="input input--sm" placeholder="e.g. SrHfO3" value={formula} onChange={(e) => setFormula(e.target.value)} />
              <button
                className="btn btn--sm"
                disabled={!formula.trim() || job?.status === "running" || s?.cache.offline}
                onClick={() => void api.admin.startJob("add_material", { formula: formula.trim() }).then(setJob).catch((e) => setJob(failedJob("add_material", e)))}
              >
                Add
              </button>
            </div>
            <div className="small muted">Pulls every entry for that formula into the cache and re-runs the self-check. Online only.</div>
            <JobLine job={job} />
          </div>
          <CacheCard />
        </div>
      </div>
    </>
  );
}

function failedJob(kind: string, e: unknown): JobState {
  return { id: "x", kind, args: {}, status: "failed", started_at: "", log: [], error: (e as Error).message };
}

function useJobPolling(job: JobState | null, setJob: (j: JobState | null) => void, onDone: () => void) {
  useEffect(() => {
    if (!job || job.status === "done" || job.status === "failed" || job.id === "x") return;
    const t = setInterval(async () => {
      try {
        const j = await api.admin.currentJob();
        if (j) setJob(j);
        if (j && (j.status === "done" || j.status === "failed")) {
          clearInterval(t);
          onDone();
        }
      } catch {
        clearInterval(t);
      }
    }, 1000);
    return () => clearInterval(t);
  }, [job?.id, job?.status]);
}

function JobLine({ job }: { job: JobState | null }) {
  if (!job) return null;
  return (
    <div className="small jobstate">
      <span className={`dot ${job.status === "failed" ? "dot--crit" : job.status === "done" ? "" : "dot--warn"}`} />
      <span>
        {job.kind} · {job.status}
        {job.error ? ` · ${job.error}` : ""}
      </span>
    </div>
  );
}

function CacheCard() {
  const app = useApp();
  const s = app.status;
  const [job, setJob] = useState<JobState | null>(null);
  useJobPolling(job, setJob, () => void app.refreshStatus());
  useEffect(() => {
    api.admin.currentJob().then((j) => j && setJob(j)).catch(() => undefined);
  }, []);
  const start = (kind: string) => void api.admin.startJob(kind).then(setJob).catch((e) => setJob(failedJob(kind, e)));
  const running = job?.status === "running" || job?.status === "queued";
  return (
    <div className="card panel">
      <span className="label">Cache</span>
      {s && (
        <>
          <div className="small mono muted">{s.cache.path}</div>
          {Object.entries(s.cache.sources).map(([src, info]) => (
            <div key={src} className="small">
              {src}: {info.n.toLocaleString()} rows · newest {String(info.newest).slice(0, 10)}
            </div>
          ))}
          {s.cache.empty && <div className="small muted">Empty.</div>}
          {s.cache.fixture_data && <div className="small" style={{ color: "var(--warn-ink)" }}>Holds synthetic fixture data.</div>}
          <div className="small" style={{ color: !s.selfcheck ? "var(--muted)" : s.selfcheck.passed ? "var(--ok)" : s.selfcheck.inconclusive ? "var(--warn-ink)" : "var(--crit)" }}>
            {!s.selfcheck ? "Self-check not run" : s.selfcheck.passed ? `Self-check passed ${s.selfcheck.checked_at}` : s.selfcheck.inconclusive ? `Self-check inconclusive: ${s.selfcheck.details[0] ?? ""}` : `Self-check FAILED: ${s.selfcheck.details.join("; ")}`}
          </div>
        </>
      )}
      <div className="wrap">
        <button className="btn btn--sm" disabled={running || s?.cache.offline} onClick={() => start("warm")} title="Fetch the candidate universe from Materials Project; needs MP_API_KEY">
          Warm from Materials Project
        </button>
        <button className="btn btn--sm" disabled={running} onClick={() => start("fixtures")}>
          Load demo fixture
        </button>
        <button className="btn btn--sm" disabled={running} onClick={() => start("selfcheck")}>
          Run self-check
        </button>
        <button className="btn btn--sm" disabled={running || s?.cache.offline} onClick={() => start("fill_gaps")}>
          Fill gaps
        </button>
      </div>
      <JobLine job={job} />
      {job && job.log.length > 0 && <div className="log">{job.log.slice(-200).join("\n")}</div>}
    </div>
  );
}

function ModelPage() {
  const app = useApp();
  const cfg = useConfig("default");
  const { data, edited, update } = cfg;
  const [env, setEnv] = useState<Json | null>(null);
  useEffect(() => {
    api.admin.environment().then(setEnv).catch(() => setEnv(null));
  }, []);
  return (
    <>
      <div className="admin__title">
        <div className="col" style={{ gap: 2 }}>
          <h2>Language model</h2>
          <span className="muted small">The model lives only at the edges: it reads requests, phrases caveats, and drives the assistant's tools. It never changes a number, a rank or a citation.</span>
        </div>
        <div className="row">
          <button className="btn" onClick={() => void cfg.reload()} disabled={!cfg.dirty}>
            Discard
          </button>
          <button className="btn btn--primary" onClick={() => void cfg.save("base").then(app.refreshStatus)} disabled={!cfg.dirty || cfg.saving || !cfg.data?.editable}>
            Save
          </button>
        </div>
      </div>
      <EditGate data={data} />
      {cfg.message && <div className={`banner small ${cfg.message.startsWith("Not") ? "banner--crit" : "banner--info"}`}>{cfg.message}</div>}
      <div className="grid2">
        <div className="card panel">
          <span className="label">Active provider · from the environment</span>
          {env ? (
            <>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="small">Provider</span>
                <span className="kbd">{env.llm.provider}</span>
              </div>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="small">Edge model</span>
                <span className="kbd">{env.llm.model ?? "provider default"}</span>
              </div>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="small">Assistant driver</span>
                <span className="kbd">{env.llm.driver === "model" ? env.llm.agent_model : "rule-based (no model)"}</span>
              </div>
              {data && Object.keys(data.env_locked).length > 0 && (
                <div className="small muted">{Object.keys(data.env_locked).map((p) => lockedLabel(data, p)).join(". ")}.</div>
              )}
              {Object.entries(env.keys as Record<string, string | null>).map(([k, v]) => (
                <div key={k} className="row" style={{ justifyContent: "space-between" }}>
                  <span className="small">{k}</span>
                  <span className="kbd">{v ?? "not set"}</span>
                </div>
              ))}
              <div className="small muted">
                Set <code>LLM_PROVIDER</code>, <code>ANTHROPIC_API_KEY</code>, <code>LLM_MODEL</code> or <code>AGENT_MODEL</code> in <code>.env</code> and restart the server. With no key the assistant still works, driven by rules. With <code>openai_compatible</code> the local model drives it.
              </div>
            </>
          ) : (
            <span className="small muted">Loading…</span>
          )}
        </div>
        {edited && (
          <div className="card panel">
            <span className="label">What the edge model is used for</span>
            <FieldRow label="Parse requests" help="Fill in fields the rules left at default; never override an element decision" shipped={shippedLabel(data?.shipped ?? null, edited, "llm.use_for.parse")}>
              <Select value={String(get(edited, "llm.use_for.parse"))} options={["true", "false"]} onChange={(v) => update("llm.use_for.parse", v === "true")} />
            </FieldRow>
            <FieldRow label="Elaborate caveats" help="Up to three observations per candidate over delimited facts, numerically guarded" shipped={shippedLabel(data?.shipped ?? null, edited, "llm.use_for.refute")}>
              <Select value={String(get(edited, "llm.use_for.refute"))} options={["true", "false"]} onChange={(v) => update("llm.use_for.refute", v === "true")} />
            </FieldRow>
            <FieldRow label="One-line rationale" help="Plain-language rationale per candidate, numerically guarded" shipped={shippedLabel(data?.shipped ?? null, edited, "llm.use_for.rationale")}>
              <Select value={String(get(edited, "llm.use_for.rationale"))} options={["true", "false"]} onChange={(v) => update("llm.use_for.rationale", v === "true")} />
            </FieldRow>
            <FieldRow label="Timeout (s)" shipped={shippedLabel(data?.shipped ?? null, edited, "llm.timeout_s")}>
              <Num value={get(edited, "llm.timeout_s")} min={5} onChange={(v) => update("llm.timeout_s", v)} />
            </FieldRow>
            <FieldRow label="Assistant model" help="Blank = claude-sonnet-5 for anthropic; AGENT_MODEL in the environment wins" shipped={shippedLabel(data?.shipped ?? null, edited, "agent.model")}>
              <input className="input input--sm" value={get(edited, "agent.model") ?? ""} onChange={(e) => update("agent.model", e.target.value || null)} />
            </FieldRow>
            <FieldRow label="Tool rounds per message" shipped={shippedLabel(data?.shipped ?? null, edited, "agent.max_tool_rounds")}>
              <Num value={get(edited, "agent.max_tool_rounds")} min={1} max={32} onChange={(v) => update("agent.max_tool_rounds", v)} />
            </FieldRow>
            <FieldRow label="Number guard" help="flag: mark numbers in a reply that no tool printed" shipped={shippedLabel(data?.shipped ?? null, edited, "agent.number_guard")}>
              <Select value={get(edited, "agent.number_guard")} options={["flag", "off"]} onChange={(v) => update("agent.number_guard", v)} />
            </FieldRow>
            <FieldRow label="Acquisition planner" help="ladder: deterministic route order. llm: the model may reorder or skip allowed routes" shipped={shippedLabel(data?.shipped ?? null, edited, "acquisition.planner")}>
              <Select value={get(edited, "acquisition.planner")} options={["ladder", "llm"]} onChange={(v) => update("acquisition.planner", v)} />
            </FieldRow>
          </div>
        )}
      </div>
      <div className="card panel">
        <span className="label">What leaves the site</span>
        <table>
          <thead>
            <tr>
              <th>Provider</th>
              <th>Data sent off-site</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>none</td>
              <td>nothing</td>
            </tr>
            <tr>
              <td>openai_compatible (local)</td>
              <td>nothing</td>
            </tr>
            <tr>
              <td>anthropic</td>
              <td>the request text, the conversation, and the public structured facts for shortlisted candidates</td>
            </tr>
          </tbody>
        </table>
      </div>
    </>
  );
}

function DataPage() {
  return (
    <>
      <div className="admin__title">
        <div className="col" style={{ gap: 2 }}>
          <h2>Data &amp; cache</h2>
          <span className="muted small">Every number traces to a cached public record with a retrieval timestamp. Jobs run one at a time; their log shows here.</span>
        </div>
      </div>
      <CacheCard />
      <div className="card panel small muted">
        Warming fetches the candidate universe from Materials Project (minutes). OQMD, PubChem and OpenAlex are fetched per query for the top-ranked pool, so the first live query on a candidate takes longer than a repeat. The self-check runs after every warm, fixture load or added material; a failed check blocks serving by default.
      </div>
    </>
  );
}

function SourcesPage() {
  const app = useApp();
  const cfg = useConfig("default");
  const { data, edited, update } = cfg;
  const sh = (p: string) => shippedLabel(data?.shipped ?? null, edited, p);
  return (
    <>
      <div className="admin__title">
        <div className="col" style={{ gap: 2 }}>
          <h2>Sources &amp; limits</h2>
          <span className="muted small">Fetch pressure on the public sources, what is fetched when, and how complete the data must be before a ranking is served.</span>
        </div>
        <div className="row">
          <button className="btn" onClick={() => void cfg.reload()} disabled={!cfg.dirty}>
            Discard
          </button>
          <button className="btn btn--primary" onClick={() => void cfg.save("base").then(app.refreshStatus)} disabled={!cfg.dirty || cfg.saving || !cfg.data?.editable}>
            Save
          </button>
        </div>
      </div>
      <EditGate data={data} />
      {cfg.message && <div className={`banner small ${cfg.message.startsWith("Not") ? "banner--crit" : "banner--info"}`}>{cfg.message}</div>}
      {edited && (
        <div className="grid2">
          <div className="card panel">
            <span className="label">Per-query fetching</span>
            <FieldRow label="Formula sources" help="on_demand: OQMD, PubChem, OpenAlex per query for the ranked pool. warm: for every formula during warm-cache" shipped={sh("candidates.formula_sources")}>
              <Select value={get(edited, "candidates.formula_sources")} options={["on_demand", "warm"]} onChange={(v) => update("candidates.formula_sources", v)} />
            </FieldRow>
            <FieldRow label="On-demand pool" help="Ranked candidates filled per query, never fewer than the shortlist" shipped={sh("candidates.on_demand_pool")}>
              <Num value={get(edited, "candidates.on_demand_pool")} min={1} onChange={(v) => update("candidates.on_demand_pool", v)} />
            </FieldRow>
            <FieldRow label="Literature sample size" shipped={sh("candidates.literature_sample_size")}>
              <Num value={get(edited, "candidates.literature_sample_size")} min={0} onChange={(v) => update("candidates.literature_sample_size", v)} />
            </FieldRow>
            <FieldRow label="Cache TTL (days)" shipped={sh("cache.ttl_days")}>
              <Num value={get(edited, "cache.ttl_days")} min={0} onChange={(v) => update("cache.ttl_days", v)} />
            </FieldRow>
            <span className="label" style={{ marginTop: 6 }}>
              Retrieval completeness
            </span>
            <FieldRow label="Warn below" help="Fraction of retrieved scoring weight over the ranked pool" shipped={sh("retrieval.min_completeness_warn")}>
              <Num value={get(edited, "retrieval.min_completeness_warn")} step={0.05} min={0} max={1} onChange={(v) => update("retrieval.min_completeness_warn", v)} />
            </FieldRow>
            <FieldRow label="Refuse to rank below" help="0 disables the block" shipped={sh("retrieval.min_completeness_serve")}>
              <Num value={get(edited, "retrieval.min_completeness_serve")} step={0.05} min={0} max={1} onChange={(v) => update("retrieval.min_completeness_serve", v)} />
            </FieldRow>
            <FieldRow label="Self-check on failure" shipped={sh("selfcheck.on_failure")}>
              <Select value={get(edited, "selfcheck.on_failure")} options={["block", "warn"]} onChange={(v) => update("selfcheck.on_failure", v)} />
            </FieldRow>
          </div>
          <div className="card panel">
            <span className="label">Per-source workers and rate caps</span>
            {["oqmd", "pubchem", "openalex"].map((src) => (
              <div key={src} className="col" style={{ gap: 0 }}>
                <FieldRow label={`${src} · workers`} shipped={sh(`candidates.fetch.${src}.workers`)}>
                  <Num value={get(edited, `candidates.fetch.${src}.workers`)} min={0} onChange={(v) => update(`candidates.fetch.${src}.workers`, v)} />
                </FieldRow>
                <FieldRow label={`${src} · max requests / s`} shipped={sh(`candidates.fetch.${src}.max_rps`)}>
                  <Num value={get(edited, `candidates.fetch.${src}.max_rps`)} step={0.5} min={0} onChange={(v) => update(`candidates.fetch.${src}.max_rps`, v)} />
                </FieldRow>
              </div>
            ))}
            <FieldRow label="Default workers per source" shipped={sh("candidates.fetch_workers")}>
              <Num value={get(edited, "candidates.fetch_workers")} min={0} onChange={(v) => update("candidates.fetch_workers", v)} />
            </FieldRow>
            <div className="small muted">OQMD returned 429 above about 2.4 requests per second and answers an uncached composition in 10 to 50 s. PubChem documents 5 per second. OpenAlex is metered by a daily budget, not by rate.</div>
          </div>
        </div>
      )}
    </>
  );
}

function DeviationsPage() {
  const [rows, setRows] = useState<any[]>([]);
  useEffect(() => {
    api.admin.deviations().then(setRows).catch(() => setRows([]));
  }, []);
  return (
    <>
      <div className="admin__title">
        <div className="col" style={{ gap: 2 }}>
          <h2>Deviations log</h2>
          <span className="muted small">Every run that departed from the shipped policy: a lifted hazard block, a moved gate, changed weights. Appended next to the cache, newest first.</span>
        </div>
      </div>
      <div className="card panel">
        {rows.length === 0 && <span className="small muted">No deviations recorded yet.</span>}
        {rows.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Profile</th>
                <th>Deviations</th>
                <th>Request</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  <td className="small mono" style={{ whiteSpace: "nowrap" }}>
                    {String(r.ts).replace("T", " ").slice(0, 16)}
                  </td>
                  <td className="small">{r.profile}</td>
                  <td className="small">
                    {(r.deviations ?? []).map((d: any, j: number) => (
                      <div key={j}>
                        <span className="chip chip--sm chip--warn" style={{ marginRight: 6 }}>
                          {d.origin}
                        </span>
                        {d.description}
                      </div>
                    ))}
                  </td>
                  <td className="small muted" style={{ maxWidth: 360 }}>
                    {String(r.request).slice(0, 160)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

export default function Admin({ page }: { page: string }) {
  const app = useApp();
  const [overlayPath, setOverlayPath] = useState<string>("site.yaml");
  useEffect(() => {
    api.admin.config("default").then((d) => setOverlayPath(d.overlay_path ?? "site.yaml (disabled)")).catch(() => undefined);
  }, []);
  return (
    <div className="admin">
      <nav className="admin__nav">
        {PAGES.map((p) => (
          <button key={p.key} className={page === p.key ? "active" : ""} onClick={() => app.navigate(`/admin/${p.key}`)}>
            {p.label}
          </button>
        ))}
        <div className="foot">
          Changes are written to <span className="kbd">{overlayPath.split("/").slice(-2).join("/")}</span>. The shipped YAML is never edited.
        </div>
      </nav>
      <main className="admin__main">
        {page === "profiles" && <ProfilesPage />}
        {page === "universe" && <UniversePage />}
        {page === "model" && <ModelPage />}
        {page === "data" && <DataPage />}
        {page === "sources" && <SourcesPage />}
        {page === "deviations" && <DeviationsPage />}
      </main>
    </div>
  );
}
