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
  stages: WorkbenchStage[];
  warnings: string[];
  outputs: Record<string, unknown>;
};

export async function runWorkbench(input: Record<string, unknown>): Promise<WorkbenchTrace> {
  const response = await fetch(
    `${process.env.NEXT_PUBLIC_WORKBENCH_API_URL ?? "http://localhost:8766"}/api/context-workbench/run`,
    { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(input) },
  );
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail ?? "Workbench run failed");
  return payload as WorkbenchTrace;
}
