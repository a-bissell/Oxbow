import { useCallback, useEffect } from "react";
import { useApp } from "../store";
import type { Scope } from "../types";
import ScopeStrip from "./ScopeStrip";

function cleanScope(scope: Scope): Scope | null {
  const out: Scope = {};
  if (scope.profile) out.profile = scope.profile;
  if (scope.families && scope.families.length) out.families = scope.families;
  if (scope.top_k != null) out.top_k = scope.top_k;
  if (scope.min_band_gap_ev != null) out.min_band_gap_ev = scope.min_band_gap_ev;
  if (scope.max_energy_above_hull_ev_atom != null) out.max_energy_above_hull_ev_atom = scope.max_energy_above_hull_ev_atom;
  if (scope.max_elements != null) out.max_elements = scope.max_elements;
  return Object.keys(out).length ? out : null;
}

export default function Composer({ hero, placeholder }: { hero?: boolean; placeholder?: string }) {
  const app = useApp();
  const ref = app.composerRef;

  const resize = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 240)}px`;
  }, [ref]);

  useEffect(resize, [app.draft, resize]);

  const submit = () => {
    const text = app.draft.trim();
    if (!text || app.busy) return;
    void app.send({ text, focus: app.focus, scope: cleanScope(app.scope) });
  };

  return (
    <div className={`composer ${hero ? "composer--hero" : ""}`}>
      {app.focus && !hero && (
        <div className="row">
          <span className="chip chip--ctx chip--sm">
            {app.focus.candidate}
            <button className="chip__x" onClick={() => app.setFocus(null)} aria-label="Clear focus">
              ×
            </button>
          </span>
          <span className="small faint">focus follows the canvas</span>
        </div>
      )}
      <textarea
        ref={ref}
        value={app.draft}
        placeholder={placeholder ?? (app.focus ? `Ask about ${app.focus.candidate}…` : "Ask a follow-up or start a new request…")}
        rows={1}
        disabled={app.busy}
        onChange={(e) => app.setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
      />
      <div className="composer__foot">
        <ScopeStrip compact={!hero} />
        <div className="row">
          <span className="composer__hint">{hero ? "Enter to run" : ""}</span>
          <button className="btn btn--primary btn--icon" onClick={submit} disabled={app.busy || !app.draft.trim()} aria-label="Send" title="Send (Enter)">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M8 13V3M4 7l4-4 4 4" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
}
