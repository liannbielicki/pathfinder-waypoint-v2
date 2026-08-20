"use client";

import { Fragment, useState } from "react";
import { PENDING_AUDIENCE_QUERY, TERMINAL_STATES, type RunDetail } from "@/lib/api";

const PIPELINE_STAGES = ["context", "evolve", "final", "score", "measure", "ready"];

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

// Plain-language labels for the immutable per-run loop snapshot (audit view).
const SETTING_LABELS: [string, string][] = [
  ["MAX_ROUNDS", "Max rounds per Pro"],
  ["MAX_NO_IMPROVE", "Dry mechanisms before stopping"],
  ["PATIENCE", "Refine attempts per mechanism"],
  ["KEEP_DELTA_PP", "Min improvement to keep (pp)"],
  ["WIN_THRESHOLD_PP", "Stop-early reduction (pp)"],
  ["CANDIDATE_COUNT", "Ideas per round"],
  ["TIE_MARGIN", "Ranker tie margin (0-1)"],
  ["WARM_START_THRESHOLD", "Warm-start similarity (0-1)"],
];

export function RunStatus({
  run,
  onKill,
}: {
  run: RunDetail;
  onKill: () => void;
}) {
  const [killArmed, setKillArmed] = useState(false);
  const [killConfirm, setKillConfirm] = useState("");
  const terminal = TERMINAL_STATES.has(run.status);
  const stages = run.stages ?? {};
  const loopConfig = (run.loop_config ?? {}) as Record<string, number>;
  const decided = run.winners.length;
  const counts = {
    winner: run.winners.filter((w) => w.kind === "winner").length,
    no_action: run.winners.filter((w) => w.kind === "no_action").length,
    abstained: run.winners.filter((w) => w.kind === "abstained").length,
  };
  const spentSomething = Number(run.cost_spent_usd) > 0;

  return (
    <section className="panel" aria-label="Run status">
      <h2>
        Run <code>{run.id}</code>
      </h2>
      <p role="status" aria-live="polite" className={`state state-${run.status}`}>
        {run.status.replace("_", " ")}
      </p>
      {run.agents_in_flight > 0 && (
        <span className="pill" title="Per-Pro jobs a worker is processing right now">
          {run.agents_in_flight} agent{run.agents_in_flight === 1 ? "" : "s"} in parallel
        </span>
      )}
      {run.stop_reason && <p className="stop-reason">Stop reason: {run.stop_reason}</p>}
      {["stopped", "failed", "degraded"].includes(run.status) && spentSomething && (
        <p className="stop-reason">Paid work may have occurred before the stop.</p>
      )}
      <p>{NEXT_ACTION[run.status] ?? "Unknown state — treat as degraded."}</p>
      <p>
        {decided} of {run.pro_ids.length} Pros decided · {counts.winner} winner /{" "}
        {counts.no_action} no-action / {counts.abstained} abstained
      </p>

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
        {run.pro_ids.length} pros · query{" "}
        {run.audience_query !== PENDING_AUDIENCE_QUERY ? (
          <code>{run.audience_query}</code>
        ) : terminal ? (
          <em>n8n never reported a query version — lineage unresolved</em>
        ) : (
          <em>awaiting n8n report (stamped when the flow first responds)</em>
        )}{" "}
        · run <code>{run.audience_run}</code>
      </p>

      {SETTING_LABELS.some(([key]) => key in loopConfig) && (
        <section aria-label="Run settings">
          <h3>Run settings</h3>
          <p className="helper">
            The immutable loop snapshot this run used. Edit defaults on the
            start form; a running run never changes.
          </p>
          <dl className="run-settings">
            {SETTING_LABELS.filter(([key]) => key in loopConfig).map(
              ([key, label]) => (
                <Fragment key={key}>
                  <dt>
                    {label} <small className="technical">{key}</small>
                  </dt>
                  <dd>{loopConfig[key]}</dd>
                </Fragment>
              ),
            )}
          </dl>
        </section>
      )}

      <h3>Cost</h3>
      <p>
        spent ${run.cost_spent_usd} · reserved ${run.cost_reserved_usd} · limit $
        {run.cost_limit_usd}
      </p>
      {run.killed && <p className="error">Fleet kill switch is active.</p>}

      {!terminal && !killArmed && (
        <button type="button" onClick={() => setKillArmed(true)}>
          Kill run
        </button>
      )}
      {!terminal && killArmed && (
        <>
          <label htmlFor="kill-confirm">Type &quot;kill&quot; to confirm</label>
          <input
            id="kill-confirm"
            value={killConfirm}
            onChange={(e) => setKillConfirm(e.target.value)}
          />
          <button
            type="button"
            disabled={killConfirm !== "kill"}
            onClick={onKill}
          >
            Confirm kill
          </button>
        </>
      )}
    </section>
  );
}
