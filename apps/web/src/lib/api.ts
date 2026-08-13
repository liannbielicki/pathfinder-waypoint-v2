import type { components } from "./api-types";

export type RunView = components["schemas"]["RunView"];
export type RunCreate = components["schemas"]["RunCreate"];
export type HandoffResponse = components["schemas"]["HandoffResponse"];

// The evidence payloads are JSONB on the wire; these shapes mirror what the
// pipeline persists (see services/api/src/waypoint/pipeline.py).
export interface PanelItem {
  persona_id: string;
  label: string;
  family: string;
  role: string;
  fit_score: number;
  rationale: string;
}

export interface PanelEvidence {
  panel?: { items?: PanelItem[]; snapshot_version?: string };
  reactions?: number[];
}

export interface Candidate {
  id: string;
  pro_id: string;
  recommendation: Record<string, unknown>;
  critics: Record<string, unknown>;
  persona_evidence: Record<string, PanelEvidence>;
  score: Record<string, Record<string, unknown>>;
  status: string;
  round?: number;
}

export interface Winner {
  id: string;
  pro_id: string;
  kind: "winner" | "no_action" | "abstained";
  candidate_id: string | null;
  rationale: string;
  evidence: Record<string, unknown>;
}

export interface Indicator {
  key: string;
  label: string;
  direction: string;
  source: string;
  window_days: number;
  rationale: string;
}

export interface Measurement {
  id: string;
  winner_id: string | null;
  indicators: Indicator[];
}

export interface Handoff {
  id: string;
  idempotency_key: string;
  status: string;
  response: Record<string, unknown> | null;
}

export type RunDetail = Omit<
  components["schemas"]["RunDetail"],
  "candidates" | "winners" | "measurements" | "handoffs"
> & {
  candidates: Candidate[];
  winners: Winner[];
  measurements: Measurement[];
  handoffs: Handoff[];
  // Per-Pro jobs a worker is actively leasing right now. Not in the generated
  // base type; regenerate api-types.ts from the live OpenAPI schema to sync.
  agents_in_flight: number;
};

export const TERMINAL_STATES = new Set([
  "complete", "no_action", "abstained", "stopped", "failed",
]);

// Placeholder audience_query sent at run creation; the pipeline replaces it
// with the n8n flow's self-reported version. Mirrors PENDING_AUDIENCE_QUERY
// in services/api/src/waypoint/models.py — keep the literals in sync.
export const PENDING_AUDIENCE_QUERY = "pending_n8n";

export class ApiError extends Error {
  constructor(public status: number, detail: string) {
    super(detail);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    credentials: "include",
    headers: { "content-type": "application/json", ...init?.headers },
  });
  if (!response.ok) throw new ApiError(response.status, await response.text());
  return response.json() as Promise<T>;
}

export const login = (password: string) =>
  api<{ status: string }>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ password }),
  });

export const createRun = (body: RunCreate) =>
  api<RunView>("/runs", { method: "POST", body: JSON.stringify(body) });

export interface FleetSettings {
  loop_defaults: Record<string, number>;
  max_in_flight_llm_calls: number;
}

export const getFleetSettings = () => api<FleetSettings>("/fleet/settings");

export const getRun = (id: string) => api<RunDetail>(`/runs/${id}`);

export const killRun = (id: string) =>
  api<RunView>(`/runs/${id}/kill`, { method: "POST" });

export const createHandoff = (id: string) =>
  api<HandoffResponse>(`/runs/${id}/handoff`, { method: "POST" });
