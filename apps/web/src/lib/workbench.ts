import type { SharedCatalogVersion } from "@/lib/catalogVersions";

export type WorkbenchStage = {
  name: string;
  status: string;
  summary?: string | null;
  data?: unknown;
  error?: string | null;
  duration_ms?: number | null;
  metrics?: Record<string, unknown>;
};

export type WorkbenchTrace = {
  job_id?: string;
  stages: WorkbenchStage[];
  warnings: string[];
  outputs: Record<string, unknown>;
};

export type WorkbenchStatus = {
  env_file: string;
  env_file_exists: boolean;
  configured: { snowflake: boolean; context_layer: boolean; ai: boolean; model: boolean };
  activity: "idle" | "waypoint" | "workbench";
};

export type WorkbenchJob = {
  id: string;
  status: "queued" | "running" | "needs_review" | "completed" | "failed";
  state: Record<string, unknown>;
  result: WorkbenchTrace | null;
  error?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type PromotionResult = {
  id: string;
  included_variables: number;
  counts: {
    approved: number;
    pii_removed: number;
    duplicates_merged: number;
    retained: number;
  };
  csv: string;
};

export const WORKBENCH_ACTIVE_JOB_KEY = "waypoint-context-workbench-active-job";

async function responsePayload(response: Response) {
  let payload;
  try {
    payload = await response.json();
  } catch {
    const status = `${response.status} ${response.statusText}`.trim();
    throw new Error(`Workbench backend returned HTTP ${status} without a valid JSON error`);
  }
  if (response.ok) return payload;
  const detail = payload.detail;
  if (typeof detail === "string") throw new Error(detail);
  if (detail && typeof detail === "object") {
    const sources = detail.sources && typeof detail.sources === "object"
      ? Object.entries(detail.sources).map(([source, reason]) => `${source}: ${String(reason)}`).join(" · ")
      : "";
    throw new Error([detail.message, sources].filter(Boolean).join(" — ") || "Workbench request failed");
  }
  throw new Error("Workbench request failed");
}

async function request(path: string, init?: RequestInit) {
  try {
    return await fetch(path, { ...init, credentials: "include" });
  } catch (cause) {
    if (cause instanceof TypeError) {
      throw new Error(
        "Lost connection to the shared Waypoint backend. It may be unavailable or restarting.",
        { cause },
      );
    }
    throw cause;
  }
}

export async function getWorkbenchStatus(): Promise<WorkbenchStatus> {
  return responsePayload(await request("/api/context-workbench/status")) as Promise<WorkbenchStatus>;
}

export async function validateFeatureCatalog(input: { name: string; filename: string; csv_text: string }) {
  return responsePayload(await request("/api/context-workbench/catalog/validate", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  }));
}

export async function listWorkbenchCatalogs(
  kind: "context" | "feature",
): Promise<SharedCatalogVersion[]> {
  return responsePayload(
    await request(`/api/context-workbench/catalogs?kind=${kind}`),
  ) as Promise<SharedCatalogVersion[]>;
}

export async function saveWorkbenchCatalog(
  version: Omit<SharedCatalogVersion, "created_at">,
): Promise<SharedCatalogVersion> {
  return responsePayload(await request("/api/context-workbench/catalogs", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(version),
  })) as Promise<SharedCatalogVersion>;
}

export async function promoteContext(input: {
  evaluation_job_id: string;
}): Promise<PromotionResult> {
  return responsePayload(await request("/api/context-workbench/promotions", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  })) as Promise<PromotionResult>;
}

export async function previewContextPromotion(input: {
  evaluation_job_id: string;
}): Promise<PromotionResult> {
  return responsePayload(await request("/api/context-workbench/promotions/preview", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  })) as Promise<PromotionResult>;
}

export async function startWorkbenchJob(input: Record<string, unknown>): Promise<WorkbenchJob> {
  return responsePayload(await request("/api/context-workbench/jobs", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(input),
  })) as Promise<WorkbenchJob>;
}

export async function getWorkbenchJob(id: string): Promise<WorkbenchJob> {
  return responsePayload(await request(`/api/context-workbench/jobs/${id}`)) as Promise<WorkbenchJob>;
}

export async function resumeWorkbenchJob(id: string): Promise<WorkbenchJob> {
  return responsePayload(await request(`/api/context-workbench/jobs/${id}/resume`, {
    method: "POST",
  })) as Promise<WorkbenchJob>;
}
