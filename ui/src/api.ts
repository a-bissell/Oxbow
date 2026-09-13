// Thin client over the FastAPI backend. Every call returns parsed JSON or throws with the
// server's message; `streamTurn` reads the event stream of one assistant turn.

import type { Conversation, ConversationSummary, DeviationSummary, Focus, JobState, RetrievalSummary, Scope, Status, TriageResult, TurnEvent } from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* keep statusText */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export const api = {
  status: () => request<Status>("/api/status"),
  scopeCount: (families: string[]) =>
    request<{ n_in_scope: number; n_universe: number }>(`/api/universe/scope?families=${encodeURIComponent(families.join(","))}`),
  conversations: () => request<ConversationSummary[]>("/api/conversations"),
  newConversation: (profile: string) => request<Conversation>("/api/conversations", { method: "POST", body: JSON.stringify({ profile }) }),
  conversation: (id: string) => request<Conversation>(`/api/conversations/${id}`),
  deleteConversation: (id: string) => request<{ deleted: boolean }>(`/api/conversations/${id}`, { method: "DELETE" }),
  result: (id: string) => request<TriageResult>(`/api/results/${id}`),
  renderUrl: (id: string, template: string) => `/api/results/${id}/render/${template}`,
  admin: {
    config: (profile: string) =>
      request<{
        profile: string;
        profiles: string[];
        effective: Record<string, any>;
        shipped: Record<string, any>;
        overlay: { base: Record<string, any>; profiles: Record<string, any> };
        overlay_path: string | null;
        editable: boolean;
        env_locked: Record<string, string>;
        site_overrides: { key: string; shipped: unknown; value: unknown }[];
      }>(
        `/api/admin/config?profile=${encodeURIComponent(profile)}`,
      ),
    saveOverlay: (overlay: { base: Record<string, any>; profiles: Record<string, any> }) =>
      request<{ saved: string; overlay: Record<string, any> }>("/api/admin/overlay", { method: "PUT", body: JSON.stringify(overlay) }),
    environment: () => request<Record<string, any>>("/api/admin/environment"),
    deviations: () => request<any[]>("/api/admin/deviations"),
    deviationsSummary: (days?: number) =>
      request<DeviationSummary>(`/api/admin/deviations/summary${days ? `?days=${days}` : ""}`),
    retrievalSummary: (days?: number) =>
      request<RetrievalSummary>(`/api/admin/retrieval/summary${days ? `?days=${days}` : ""}`),
    startJob: (kind: string, args: Record<string, unknown> = {}) =>
      request<JobState>("/api/admin/jobs", { method: "POST", body: JSON.stringify({ kind, args }) }),
    currentJob: () => request<JobState | null>("/api/admin/jobs/current"),
  },
};

export interface TurnBody {
  text?: string;
  focus?: Focus | null;
  scope?: Scope | null;
  confirm?: string | null;
  dismiss?: string | null;
}

/** POST a turn and deliver its events as they arrive. Resolves when the stream ends. */
export async function streamTurn(conversationId: string, body: TurnBody, onEvent: (ev: TurnEvent) => void): Promise<void> {
  const res = await fetch(`/api/conversations/${conversationId}/turns`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data: ")) {
          try {
            onEvent(JSON.parse(line.slice(6)) as TurnEvent);
          } catch {
            /* malformed line: skip */
          }
        }
      }
    }
  }
}
