"use client";

import { TERMINAL_STATES, type RunDetail } from "@/lib/api";

const PIPELINE_STAGES = [
  "context", "generate", "critics", "screen", "search", "final",
  "score", "measure", "ready",
];

const NEXT_ACTION: Record<string, string> = {
  queued: "Waiting for a worker to claim the job.",
  running: "Workers are processing stages. You can kill the run.",
  waiting: "A dependency was unavailable; the run will retry automatically.",
  degraded: "Some pros failed; the rest continued. Inspect the evidence below.",
  resumed: "A worker resumed from the last durable checkpoint.",
  failed: "The run failed honestly — no canned fallback was used. Start a new run.",
  stopped: "The run was stopped. Start a new run when ready.",
  complete: "Review the winner and create the LCM handoff below.",
  abstained: "The system abstained rather than fabricate evidence. No handoff.",
  no_action: "No action is the recommendation. No handoff will be sent.",
};

export function RunStatus({
  run,
  onKill,
}: {
  run: RunDetail;
  onKill: () => void;
}) {
  const terminal = TERMINAL_STATES.has(run.status);
  const stages = run.stages ?? {};
  return (
    <section className="panel" aria-label="Run status">
      <h2>
        Run <code>{run.id}</code>
      </h2>
      <p role="status" className={`state state-${run.status}`}>
        {run.status.replace("_", " ")}
      </p>
      {run.stop_reason && <p className="stop-reason">Stop reason: {run.stop_reason}</p>}
      <p>{NEXT_ACTION[run.status] ?? "Unknown state — treat as degraded."}</p>

      <h3>Stages</h3>
      <ol className="stages">
        {PIPELINE_STAGES.map((stage) => (
          <li key={stage} data-done={stage in stages}>
            <span>{stage}</span> {stage in stages ? "✓" : "·"}
          </li>
        ))}
      </ol>

      <h3>Audience lineage</h3>
      <p>
        {run.pro_ids.length} pros · query <code>{run.audience_query}</code> · run{" "}
        <code>{run.audience_run}</code>
      </p>

      <h3>Cost</h3>
      <p>
        spent ${run.cost_spent_usd} · reserved ${run.cost_reserved_usd} · limit $
        {run.cost_limit_usd}
      </p>
      {run.killed && <p className="error">Fleet kill switch is active.</p>}

      <button type="button" onClick={onKill} disabled={terminal}>
        Kill run
      </button>
    </section>
  );
}
