"use client";

import { useCallback, useEffect, useState } from "react";
import { WorkbenchRunForm } from "@/components/WorkbenchRunForm";
import { WorkbenchTimeline } from "@/components/WorkbenchTimeline";
import { getWorkbenchStatus, type WorkbenchTrace } from "@/lib/workbench";
import { AuthoringCatalog } from "@/components/AuthoringCatalog";
import { LoginForm } from "@/components/LoginForm";

export default function ContextWorkbenchPage() {
  const [authState, setAuthState] = useState<"checking" | "signed_out" | "signed_in">("checking");
  const [trace, setTrace] = useState<WorkbenchTrace | null>(null);
  const [busy, setBusy] = useState(false);
  const handleRun = useCallback((next: WorkbenchTrace) => { setTrace(next); setBusy(false); }, []);
  useEffect(() => {
    let cancelled = false;
    getWorkbenchStatus()
      .then(() => { if (!cancelled) setAuthState("signed_in"); })
      .catch(() => { if (!cancelled) setAuthState("signed_out"); });
    return () => { cancelled = true; };
  }, []);
  if (authState === "checking") return <main><section className="panel">Checking session…</section></main>;
  if (authState === "signed_out") return <main><LoginForm onSuccess={() => setAuthState("signed_in")} /></main>;
  return <main className="workbench">
    <WorkbenchRunForm busy={busy} onBusy={setBusy} onRun={handleRun} completed={Boolean(trace)} />
    {trace && <>
      <AuthoringCatalog key={JSON.stringify((trace.outputs.authoring as { draft?: unknown } | undefined)?.draft)} trace={trace} />
      <details className="diagnostics"><summary>Run diagnostics</summary><WorkbenchTimeline trace={trace} /></details>
    </>}
  </main>;
}
