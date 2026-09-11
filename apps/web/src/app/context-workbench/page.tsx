"use client";

import { useState } from "react";
import { WorkbenchRunForm } from "@/components/WorkbenchRunForm";
import { WorkbenchTimeline } from "@/components/WorkbenchTimeline";
import { WorkbenchFlow } from "@/components/WorkbenchFlow";
import type { WorkbenchTrace } from "@/lib/workbench";
import { AuthoringCatalog } from "@/components/AuthoringCatalog";

export default function ContextWorkbenchPage() {
  const [trace, setTrace] = useState<WorkbenchTrace | null>(null);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState("runtime");
  function handleModeChange(next: string) { setMode(next); setTrace(null); }
  async function handleRun(next: WorkbenchTrace) { setTrace(next); setBusy(false); }
  return <main className="workbench"><WorkbenchRunForm busy={busy} onBusy={setBusy} onModeChange={handleModeChange} onRun={handleRun} />{mode === "authoring" ? (trace && <AuthoringCatalog key={JSON.stringify((trace.outputs.authoring as { draft?: unknown } | undefined)?.draft)} trace={trace} />) : <><WorkbenchFlow />{trace && <WorkbenchTimeline trace={trace} />}</>}</main>;
}
