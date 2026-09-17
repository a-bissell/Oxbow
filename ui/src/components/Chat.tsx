import { useEffect, useRef, type ReactNode } from "react";
import { splitParagraphs } from "../format";
import { useApp, type LiveTurn } from "../store";
import type { Step, Turn } from "../types";
import Composer from "./Composer";

/** Inline **bold** and `code`, nothing else: assistant prose is short by design. */
function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let i = 0;
  for (const m of text.matchAll(re)) {
    if (m.index! > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith("**")) out.push(<strong key={i++}>{tok.slice(2, -2)}</strong>);
    else out.push(<code key={i++}>{tok.slice(1, -1)}</code>);
    last = m.index! + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function Prose({ text, streaming }: { text: string; streaming?: boolean }) {
  const paras = splitParagraphs(text);
  return (
    <div className="prose">
      {paras.map((p, i) => {
        const lines = p.split("\n");
        const isList = lines.every((l) => /^\s*[-*•]\s+/.test(l));
        if (isList) {
          return (
            <ul key={i}>
              {lines.map((l, j) => (
                <li key={j}>{inline(l.replace(/^\s*[-*•]\s+/, ""))}</li>
              ))}
            </ul>
          );
        }
        return (
          <p key={i}>
            {inline(p)}
            {streaming && i === paras.length - 1 && <span className="cursor" />}
          </p>
        );
      })}
      {streaming && paras.length === 0 && (
        <p>
          <span className="cursor" />
        </p>
      )}
    </div>
  );
}

function StepIcon({ status }: { status: Step["status"] }) {
  const common = { className: "step__icon", viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  if (status === "running")
    return (
      <svg {...common}>
        <path className="spin" d="M8 2a6 6 0 1 1-5.2 3" />
      </svg>
    );
  if (status === "failed")
    return (
      <svg {...common}>
        <path d="M4 4l8 8M12 4l-8 8" />
      </svg>
    );
  if (status === "held")
    return (
      <svg {...common}>
        <circle cx="8" cy="8" r="6" />
        <path d="M6 5.5v5M10 5.5v5" />
      </svg>
    );
  return (
    <svg {...common}>
      <path d="M3 8.5l3 3 7-7" />
    </svg>
  );
}

function Steps({ steps, progress }: { steps: Step[]; progress?: LiveTurn["progress"] }) {
  if (!steps.length) return null;
  return (
    <div className="steps">
      {steps.map((s, i) => (
        <div key={i}>
          <div className={`step step--${s.status}`} title={JSON.stringify(s.args)}>
            <StepIcon status={s.status} />
            <span>
              {s.label}
              {s.ms != null && s.status !== "running" && s.ms > 1500 ? ` · ${(s.ms / 1000).toFixed(0)} s` : ""}
            </span>
          </div>
          {s.status === "running" && progress && (
            <div className="progress">
              <span>{progress.message}</span>
              {progress.total != null && progress.done != null && progress.total > 0 && progress.stage === "fill" && (
                <div className="bar bar--thin" style={{ width: 220 }}>
                  <i style={{ width: `${Math.round((100 * progress.done) / progress.total)}%` }} />
                </div>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function UserTurn({ turn }: { turn: Turn }) {
  return (
    <div className="turn--user">
      {(turn.focus || (turn.scope && Object.values(turn.scope).some((v) => v != null))) && (
        <div className="wrap" style={{ justifyContent: "flex-end" }}>
          {turn.focus && <span className="chip chip--ctx chip--sm">{turn.focus.candidate}</span>}
          {turn.scope?.profile && <span className="chip chip--sm">{turn.scope.profile}</span>}
          {turn.scope?.families && turn.scope.families.length > 0 && <span className="chip chip--sm">{turn.scope.families.length} families</span>}
          {turn.scope?.top_k != null && <span className="chip chip--sm">top {turn.scope.top_k}</span>}
          {turn.scope?.min_band_gap_ev != null && <span className="chip chip--sm chip--warn">gap ≥ {turn.scope.min_band_gap_ev}</span>}
          {turn.scope?.max_energy_above_hull_ev_atom != null && <span className="chip chip--sm chip--warn">hull ≤ {turn.scope.max_energy_above_hull_ev_atom}</span>}
        </div>
      )}
      <div className="bubble">{turn.text}</div>
    </div>
  );
}

function AssistantTurn({ turn, isLast }: { turn: Turn; isLast: boolean }) {
  const app = useApp();
  // A held run stays answerable until someone answers it, however many turns have gone by since.
  // Gating this on "is the last turn" stranded the card: still on screen, no longer clickable.
  const pendingOpen = turn.pending && !turn.pending.resolved && !app.busy;
  return (
    <div className="turn--assistant">
      {turn.error && <div className="banner banner--warn small">{turn.error}</div>}
      <Steps steps={turn.steps} />
      {turn.text && <Prose text={turn.text} />}
      {turn.pending && (
        <div className={`card pendingcard${turn.pending.resolved ? " pendingcard--settled" : ""}`}>
          <span className="label">Before running</span>
          <ul className="small">
            {turn.pending.questions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ul>
          {turn.pending.resolved && (
            <div className="small" style={{ color: "var(--muted)" }}>
              {turn.pending.resolved === "confirmed" ? "You ran it with that." : "You chose not to run it."}
            </div>
          )}
          {pendingOpen && (
            <div className="row">
              <button className="btn btn--primary btn--sm" onClick={() => void app.send({ confirm: turn.pending!.id })}>
                Run with that
              </button>
              <button className="btn btn--sm" onClick={() => void app.send({ dismiss: turn.pending!.id })}>
                Don't run
              </button>
            </div>
          )}
        </div>
      )}
      {turn.unverified && turn.unverified.length > 0 && (
        <div className="small" style={{ color: "var(--warn-ink)" }} title="The number guard checks every number in the reply against the numbers the tools printed. It flags; it does not rewrite.">
          Not verified against tool output: {turn.unverified.join(", ")}
        </div>
      )}
      {turn.result_id && turn.result_id !== app.currentResultId && (
        <a
          className="resultlink"
          href={`#${turn.result_id}`}
          onClick={(e) => {
            e.preventDefault();
            void app.showResult(turn.result_id!);
          }}
        >
          Show this result on the canvas
        </a>
      )}
      {isLast && turn.suggestions.length > 0 && !app.busy && (
        <div className="wrap">
          {turn.suggestions.map((s, i) => (
            <button key={i} className="chip chip--btn" onClick={() => void app.send({ text: s, focus: app.focus })}>
              {s}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function LiveAssistantTurn({ live }: { live: LiveTurn }) {
  return (
    <div className="turn--assistant">
      {live.notice && <div className="banner banner--warn small">{live.notice}</div>}
      <Steps steps={live.steps} progress={live.progress} />
      {(live.text || !live.steps.length) && <Prose text={live.text} streaming />}
      {live.pending && (
        <div className="card pendingcard">
          <span className="label">Before running</span>
          <ul className="small">
            {live.pending.questions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export default function Chat() {
  const app = useApp();
  const scroll = useRef<HTMLDivElement>(null);
  const turns = app.conv?.turns ?? [];
  const lastAssistant = [...turns].reverse().find((t) => t.role === "assistant");

  useEffect(() => {
    const el = scroll.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns.length, app.live?.text, app.live?.steps.length, app.live?.progress?.message]);

  return (
    <section className="chat">
      <div className="chat__scroll" ref={scroll}>
        {turns.map((t) => (t.role === "user" ? <UserTurn key={t.id} turn={t} /> : <AssistantTurn key={t.id} turn={t} isLast={t === lastAssistant} />))}
        {app.live && <LiveAssistantTurn live={app.live} />}
      </div>
      <div className="chat__foot">
        <Composer />
      </div>
    </section>
  );
}
