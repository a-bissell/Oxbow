// Application state: the loaded status, the open conversation, cached result objects, the
// assistant turn in progress, and what the canvas is showing. One context, no reducer
// ceremony; every mutation is a named function so components read like prose.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, streamTurn, type TurnBody } from "./api";
import type { Conversation, Focus, Pending, Scope, Status, Step, TriageResult, Turn } from "./types";

export type View =
  | { kind: "list"; tab: "shortlist" | "beyond" | "excluded" | "gaps" | "scoring" }
  | { kind: "focus"; candidate: string }
  | { kind: "compare"; keys: string[] };

export interface LiveTurn {
  steps: Step[];
  text: string;
  progress: { stage: string; message: string; done: number | null; total: number | null } | null;
  pending: Pending | null;
  notice: string | null;
}

export interface AppStore {
  status: Status | null;
  refreshStatus: () => Promise<void>;
  conv: Conversation | null;
  results: Record<string, TriageResult>;
  prevOf: Record<string, string>;
  live: LiveTurn | null;
  busy: boolean;
  error: string | null;
  clearError: () => void;
  scope: Scope;
  setScope: (s: Scope) => void;
  focus: Focus | null;
  setFocus: (f: Focus | null) => void;
  view: View;
  setView: (v: View) => void;
  currentResultId: string | null;
  showResult: (id: string) => Promise<void>;
  draft: string;
  setDraft: (d: string) => void;
  composerRef: React.RefObject<HTMLTextAreaElement>;
  focusComposer: () => void;
  openConversation: (id: string) => Promise<void>;
  send: (body: TurnBody) => Promise<void>;
  reset: () => void;
  route: string;
  navigate: (path: string) => void;
}

const Ctx = createContext<AppStore | null>(null);

export function useApp(): AppStore {
  const s = useContext(Ctx);
  if (!s) throw new Error("useApp outside AppProvider");
  return s;
}

function readRoute(): string {
  return window.location.pathname + window.location.search;
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [conv, setConv] = useState<Conversation | null>(null);
  const [results, setResults] = useState<Record<string, TriageResult>>({});
  const [prevOf, setPrevOf] = useState<Record<string, string>>({});
  const [live, setLive] = useState<LiveTurn | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [scope, setScope] = useState<Scope>({});
  const [focus, setFocus] = useState<Focus | null>(null);
  const [view, setView] = useState<View>({ kind: "list", tab: "shortlist" });
  const [currentResultId, setCurrentResultId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [route, setRoute] = useState(readRoute());
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const resultsRef = useRef(results);
  resultsRef.current = results;

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await api.status());
    } catch (e) {
      setError(`Could not reach the server: ${(e as Error).message}`);
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  useEffect(() => {
    const onPop = () => setRoute(readRoute());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback((path: string) => {
    if (path !== readRoute()) window.history.pushState(null, "", path);
    setRoute(path);
  }, []);

  const loadResult = useCallback(async (id: string): Promise<TriageResult | null> => {
    if (resultsRef.current[id]) return resultsRef.current[id];
    try {
      const r = await api.result(id);
      setResults((prev) => ({ ...prev, [id]: r }));
      return r;
    } catch (e) {
      setError(`Could not load result ${id}: ${(e as Error).message}`);
      return null;
    }
  }, []);

  const showResult = useCallback(
    async (id: string) => {
      await loadResult(id);
      setCurrentResultId(id);
      setView({ kind: "list", tab: "shortlist" });
    },
    [loadResult],
  );

  const openConversation = useCallback(
    async (id: string) => {
      try {
        const c = await api.conversation(id);
        setConv(c);
        setFocus(null);
        setLive(null);
        const latest = c.result_ids[c.result_ids.length - 1];
        // Reconstruct which result each rerun replaced from the recorded tool arguments.
        const prev: Record<string, string> = {};
        let last: string | null = null;
        for (const t of c.turns) {
          if (t.role !== "assistant" || !t.result_id) continue;
          if (last && t.steps.some((s) => s.tool === "rerun") && t.result_id !== last) prev[t.result_id] = last;
          last = t.result_id;
        }
        setPrevOf((p) => ({ ...p, ...prev }));
        if (latest) await showResult(latest);
        else setCurrentResultId(null);
        const lastScope = [...c.turns].reverse().find((t) => t.role === "user" && t.scope)?.scope;
        setScope(lastScope ? { ...lastScope } : { profile: c.profile });
        navigate(`/?c=${id}`);
      } catch (e) {
        setError(`Could not open the conversation: ${(e as Error).message}`);
      }
    },
    [navigate, showResult],
  );

  const reset = useCallback(() => {
    setConv(null);
    setLive(null);
    setFocus(null);
    setCurrentResultId(null);
    setView({ kind: "list", tab: "shortlist" });
    setDraft("");
    navigate("/");
  }, [navigate]);

  const focusComposer = useCallback(() => {
    composerRef.current?.focus();
  }, []);

  const send = useCallback(
    async (body: TurnBody) => {
      if (busy) return;
      setBusy(true);
      setError(null);
      let c = conv;
      try {
        if (!c) {
          c = await api.newConversation(body.scope?.profile || scope.profile || "default");
          setConv(c);
          navigate(`/?c=${c.id}`);
        }
        const cid = c.id;
        const userTurn: Turn = {
          id: `local-${Date.now()}`,
          role: "user",
          text: body.text ?? (body.confirm ? "Yes, run it." : body.dismiss ? "No, leave it." : ""),
          created_at: new Date().toISOString(),
          focus: body.focus ?? null,
          scope: body.scope ?? null,
          steps: [],
          suggestions: [],
        };
        setConv((prev) => (prev ? { ...prev, turns: [...prev.turns, userTurn] } : prev));
        setLive({ steps: [], text: "", progress: null, pending: null, notice: null });
        setDraft("");
        let sawFocus = false;
        await streamTurn(cid, body, (ev) => {
          switch (ev.type) {
            case "step":
              setLive((l) => {
                if (!l) return l;
                const steps = [...l.steps];
                steps[ev.index] = ev.step;
                return { ...l, steps, progress: ev.step.status === "running" ? l.progress : null };
              });
              if (ev.step.status === "done" && ev.step.tool === "compare" && Array.isArray(ev.step.args.candidates)) {
                setView({ kind: "compare", keys: ev.step.args.candidates as string[] });
              }
              if (ev.step.status === "done" && ev.step.tool === "list_candidates" && ev.step.args.section === "excluded") {
                setView({ kind: "list", tab: "excluded" });
              }
              break;
            case "progress":
              setLive((l) => (l ? { ...l, progress: { stage: ev.stage, message: ev.message, done: ev.done, total: ev.total } } : l));
              break;
            case "text":
              setLive((l) => (l ? { ...l, text: l.text + ev.delta } : l));
              break;
            case "result":
              if (ev.previous_result_id) setPrevOf((p) => ({ ...p, [ev.result_id]: ev.previous_result_id as string }));
              void loadResult(ev.result_id).then(() => {
                setCurrentResultId(ev.result_id);
                if (!sawFocus) setView({ kind: "list", tab: "shortlist" });
              });
              break;
            case "focus":
              sawFocus = true;
              void loadResult(ev.result_id).then(() => {
                setCurrentResultId(ev.result_id);
                setView({ kind: "focus", candidate: ev.candidate });
                setFocus({ result_id: ev.result_id, candidate: ev.candidate });
              });
              break;
            case "clarify":
              setLive((l) => (l ? { ...l, pending: ev.pending } : l));
              break;
            case "notice":
              setLive((l) => (l ? { ...l, notice: ev.message } : l));
              break;
            case "error":
              setError(ev.message);
              break;
            default:
              break;
          }
        });
        const fresh = await api.conversation(cid);
        setConv(fresh);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setLive(null);
        setBusy(false);
      }
    },
    [busy, conv, scope.profile, navigate, loadResult],
  );

  // Open a conversation named in the URL on first load.
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("c");
    if (id && !conv) void openConversation(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const value = useMemo<AppStore>(
    () => ({
      status,
      refreshStatus,
      conv,
      results,
      prevOf,
      live,
      busy,
      error,
      clearError: () => setError(null),
      scope,
      setScope,
      focus,
      setFocus,
      view,
      setView,
      currentResultId,
      showResult,
      draft,
      setDraft,
      composerRef,
      focusComposer,
      openConversation,
      send,
      reset,
      route,
      navigate,
    }),
    [status, refreshStatus, conv, results, prevOf, live, busy, error, scope, focus, view, currentResultId, showResult, draft, focusComposer, openConversation, send, reset, route, navigate],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
