// The scope strip: what a request is judged against. Every chip is a popover; a set value
// travels with the next message and shows up as a deviation on the result.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "../api";
import { useApp } from "../store";
import type { Scope } from "../types";

function Popover({ open, onClose, children, right }: { open: boolean; onClose: () => void; children: ReactNode; right?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div ref={ref} className={`popover ${right ? "popover--right" : ""}`}>
      {children}
    </div>
  );
}

export default function ScopeStrip({ compact }: { compact?: boolean }) {
  const app = useApp();
  const s = app.status;
  const [open, setOpen] = useState<null | "profile" | "families" | "more">(null);
  const [count, setCount] = useState<{ n_in_scope: number; n_universe: number } | null>(null);
  const scope = app.scope;
  const profiles = s?.profiles ?? [];
  const profile = profiles.find((p) => p.name === (scope.profile || "default")) ?? profiles[0];
  const families = s?.families ?? [];
  const selected = scope.families ?? [];
  const allSelected = selected.length === 0 || selected.length === families.length;

  useEffect(() => {
    let alive = true;
    api
      .scopeCount(allSelected ? [] : selected)
      .then((c) => alive && setCount(c))
      .catch(() => alive && setCount(null));
    return () => {
      alive = false;
    };
  }, [selected.join(","), allSelected, s?.n_universe]);

  const update = (patch: Partial<Scope>) => app.setScope({ ...scope, ...patch });

  const topK = scope.top_k ?? profile?.top_k ?? 5;
  const gap = scope.min_band_gap_ev ?? profile?.gates.min_band_gap_ev;
  const hull = scope.max_energy_above_hull_ev_atom ?? profile?.gates.max_energy_above_hull_ev_atom;
  const maxEl = scope.max_elements ?? profile?.gates.max_elements;
  const gapSet = scope.min_band_gap_ev != null && scope.min_band_gap_ev !== profile?.gates.min_band_gap_ev;
  const hullSet = scope.max_energy_above_hull_ev_atom != null && scope.max_energy_above_hull_ev_atom !== profile?.gates.max_energy_above_hull_ev_atom;
  const elSet = scope.max_elements != null && scope.max_elements !== profile?.gates.max_elements;
  const topSet = scope.top_k != null && scope.top_k !== profile?.top_k;

  const famLabel = allSelected
    ? `all ${families.length} families`
    : `${selected.length} of ${families.length} families${count && !compact ? ` · ${count.n_in_scope.toLocaleString()}` : ""}`;

  return (
    <div className="scope">
      <div className="scope__item">
        <button className="chip chip--btn" onClick={() => setOpen(open === "profile" ? null : "profile")} title={profile?.description}>
          {profile?.name ?? "default"}
        </button>
        <Popover open={open === "profile"} onClose={() => setOpen(null)}>
          <div className="col">
            <span className="label">Profile</span>
            {profiles.map((p) => (
              <label key={p.name} className="check" onClick={() => update({ profile: p.name })}>
                <input type="radio" checked={p.name === (profile?.name ?? "default")} readOnly />
                <span>
                  <div>{p.name}</div>
                  <div className="small muted">{p.description}</div>
                  <div className="small faint">
                    hull ≤ {p.gates.max_energy_above_hull_ev_atom} · gap ≥ {p.gates.min_band_gap_ev} eV · ≤ {p.gates.max_elements} elements · top {p.top_k}
                  </div>
                </span>
              </label>
            ))}
          </div>
        </Popover>
      </div>

      <div className="scope__item">
        <button className={`chip chip--btn ${!allSelected ? "chip--ctx" : ""}`} onClick={() => setOpen(open === "families" ? null : "families")}>
          {famLabel}
        </button>
        <Popover open={open === "families"} onClose={() => setOpen(null)}>
          <div className="col" style={{ minWidth: 360 }}>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="label">Material families in scope</span>
              <span className="small muted">
                {count ? `${count.n_in_scope.toLocaleString()} of ${count.n_universe.toLocaleString()} materials` : ""}
              </span>
            </div>
            {families.map((f) => {
              const on = allSelected || selected.includes(f.id);
              return (
                <label key={f.id} className="check">
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={() => {
                      const base = allSelected ? families.map((x) => x.id) : selected;
                      const next = on ? base.filter((x) => x !== f.id) : [...base, f.id];
                      update({ families: next.length === families.length ? [] : next });
                    }}
                  />
                  <span>
                    <div>
                      {f.name} <span className="faint small">{f.cations.join(" ")}</span>
                    </div>
                    <div className="small muted">{f.rationale}</div>
                  </span>
                </label>
              );
            })}
            <div className="small faint">A material is in scope when every cation in it belongs to a selected family.</div>
            <div className="row">
              <button className="btn btn--sm" onClick={() => update({ families: [] })}>
                Select all
              </button>
            </div>
          </div>
        </Popover>
      </div>

      <div className="scope__item">
        <button className={`chip chip--btn ${topSet ? "chip--warn" : ""}`} onClick={() => setOpen(open === "more" ? null : "more")}>
          top {topK}
        </button>
      </div>
      {gapSet && (
        <span className="chip chip--warn">
          gap ≥ {gap} eV
          <button className="chip__x" onClick={() => update({ min_band_gap_ev: null })} aria-label="Reset gap">
            ×
          </button>
        </span>
      )}
      {hullSet && (
        <span className="chip chip--warn">
          hull ≤ {hull}
          <button className="chip__x" onClick={() => update({ max_energy_above_hull_ev_atom: null })} aria-label="Reset hull">
            ×
          </button>
        </span>
      )}
      {elSet && (
        <span className="chip chip--warn">
          ≤ {maxEl} elements
          <button className="chip__x" onClick={() => update({ max_elements: null })} aria-label="Reset elements">
            ×
          </button>
        </span>
      )}
      <div className="scope__item">
        <button className="chip chip--btn chip--soft" onClick={() => setOpen(open === "more" ? null : "more")} title="Shortlist length and gates">
          +
        </button>
        <Popover open={open === "more"} onClose={() => setOpen(null)}>
          <div className="col" style={{ minWidth: 280 }}>
            <span className="label">Shortlist and gates for the next request</span>
            <div className="field">
              <label>Shortlist length</label>
              <input className="input input--sm" type="number" min={1} max={50} value={topK} onChange={(e) => update({ top_k: Number(e.target.value) || null })} />
            </div>
            <div className="field">
              <label>Minimum effective band gap (eV)</label>
              <input className="input input--sm" type="number" step={0.5} min={0} value={gap ?? ""} onChange={(e) => update({ min_band_gap_ev: e.target.value === "" ? null : Number(e.target.value) })} />
            </div>
            <div className="field">
              <label>Max energy above hull (eV/atom)</label>
              <input className="input input--sm" type="number" step={0.01} min={0} value={hull ?? ""} onChange={(e) => update({ max_energy_above_hull_ev_atom: e.target.value === "" ? null : Number(e.target.value) })} />
            </div>
            <div className="field">
              <label>Max distinct elements</label>
              <input className="input input--sm" type="number" min={2} max={6} value={maxEl ?? ""} onChange={(e) => update({ max_elements: e.target.value === "" ? null : Number(e.target.value) })} />
            </div>
            <div className="small faint">A changed gate is a deviation from the profile and is printed on the result.</div>
            <div className="row">
              <button className="btn btn--sm" onClick={() => update({ top_k: null, min_band_gap_ev: null, max_energy_above_hull_ev_atom: null, max_elements: null })}>
                Reset to profile
              </button>
            </div>
          </div>
        </Popover>
      </div>
    </div>
  );
}
