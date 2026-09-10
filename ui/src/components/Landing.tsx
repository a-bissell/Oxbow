import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useApp } from "../store";
import type { ConversationSummary, JobState } from "../types";
import Composer from "./Composer";
import { timeAgo } from "../format";

export default function Landing() {
  const app = useApp();
  const s = app.status;
  const [recent, setRecent] = useState<ConversationSummary[]>([]);
  const [job, setJob] = useState<JobState | null>(null);
  const greeting = useMemo(() => {
    const pool = s?.greetings ?? ["Which oxides should go on the bench first?"];
    return pool[Math.floor(Math.random() * pool.length)];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [s?.greetings?.length]);

  useEffect(() => {
    api.conversations().then(setRecent).catch(() => setRecent([]));
  }, [app.conv]);

  // Poll a demo-data job started from the empty state.
  useEffect(() => {
    if (!job || job.status === "done" || job.status === "failed") return;
    const t = setInterval(async () => {
      const j = await api.admin.currentJob();
      setJob(j);
      if (j && (j.status === "done" || j.status === "failed")) {
        clearInterval(t);
        void app.refreshStatus();
      }
    }, 800);
    return () => clearInterval(t);
  }, [job, app]);

  const loadDemo = async () => {
    try {
      setJob(await api.admin.startJob("fixtures"));
    } catch (e) {
      setJob({ id: "x", kind: "fixtures", args: {}, status: "failed", started_at: "", log: [], error: (e as Error).message });
    }
  };

  const empty = s?.cache.empty;

  return (
    <div className="landing">
      <div className="landing__hero">
        <div className="greeting">
          <h1>{greeting}</h1>
          <p className="muted">Ask in plain English. Every number in the answer traces to a public source.</p>
        </div>

        {empty ? (
          <div className="card empty">
            <div>
              <strong>No data yet.</strong> The cache is empty, so there is nothing to rank. Load the synthetic demo set to try the assistant in a
              minute, or warm the cache from Materials Project for real data.
            </div>
            <div className="row">
              <button className="btn btn--primary" onClick={loadDemo} disabled={!!job && job.status === "running"}>
                {job?.status === "running" ? "Loading demo data…" : "Load demo data"}
              </button>
              <button className="btn" onClick={() => app.navigate("/admin/data")}>
                Warm from Materials Project
              </button>
              {job?.status === "failed" && <span className="small" style={{ color: "var(--crit)" }}>{job.error}</span>}
            </div>
            <div className="small muted">Demo data is a hand-written approximation of about 35 well-known oxides. Every output made from it says so.</div>
          </div>
        ) : (
          <Composer hero placeholder={s?.suggested_requests[0]?.text} />
        )}

        {s?.cache.fixture_data && !empty && (
          <div className="banner banner--warn small">
            This cache holds synthetic demo data, so every number is illustrative. Warm it from Materials Project in Admin → Data for real results.
          </div>
        )}
      </div>

      {!empty && (
        <div className="landing__section">
          <span className="label">Try one of these</span>
          <div className="wrap">
            {(s?.suggested_requests ?? []).map((q) => (
              <button
                key={q.label}
                className="chip chip--btn"
                title={q.text}
                onClick={() => {
                  app.setDraft(q.text);
                  app.focusComposer();
                }}
              >
                {q.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {recent.length > 0 && (
        <div className="landing__section">
          <span className="label">Recent</span>
          <div className="card recent">
            {recent.slice(0, 8).map((c) => (
              <button key={c.id} className="recent__row" onClick={() => void app.openConversation(c.id)}>
                <span className="recent__title">{c.title || "Untitled"}</span>
                <span className="small muted" style={{ whiteSpace: "nowrap" }}>
                  {c.profile} · {c.n_turns} turns · {timeAgo(c.updated_at)}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
