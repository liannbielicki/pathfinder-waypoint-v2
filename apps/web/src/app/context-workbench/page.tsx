"use client";

import { useCallback, useState } from "react";
import { WorkbenchRunForm } from "@/components/WorkbenchRunForm";
import { WorkbenchTimeline } from "@/components/WorkbenchTimeline";
import type { WorkbenchTrace } from "@/lib/workbench";
import { AuthoringCatalog } from "@/components/AuthoringCatalog";

export default function ContextWorkbenchPage() {
  const [trace, setTrace] = useState<WorkbenchTrace | null>(null);
  const [busy, setBusy] = useState(false);
  const handleRun = useCallback((next: WorkbenchTrace) => { setTrace(next); setBusy(false); }, []);
  return <main className="workbench">
    <WorkbenchRunForm busy={busy} onBusy={setBusy} onRun={handleRun} completed={Boolean(trace)} />
    {trace && <>
      <AuthoringCatalog key={JSON.stringify((trace.outputs.authoring as { draft?: unknown } | undefined)?.draft)} trace={trace} />
      <details className="diagnostics"><summary>Run diagnostics</summary><WorkbenchTimeline trace={trace} /></details>
    </>}
  </main>;
}
