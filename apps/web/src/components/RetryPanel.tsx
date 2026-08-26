"use client";

import Link from "next/link";
import { useState } from "react";
import {
  PENDING_AUDIENCE_QUERY,
  createRun,
  isRunSettled,
  type RunCreateInput,
  type RunDetail,
} from "@/lib/api";

// Pros worth retrying: everything without a "winner" outcome once the run
// settles. Mid-run, a missing winner just means "not scored yet", so the
// panel stays hidden until the run reaches a settled state.
function retryList(run: RunDetail) {
  const byPro = new Map(run.winners.map((w) => [w.pro_id, w]));
  return run.pro_ids.flatMap((proId) => {
    const w = byPro.get(proId);
    if (w?.kind === "winner") return [];
    return [{
      proId,
      reason: w
        ? `${w.kind.replace("_", " ")} — ${w.rationale}`
        : "failed — no result recorded",
    }];
  });
}

export function RetryPanel({ run }: { run: RunDetail }) {
  const [retryRun, setRetryRun] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isRunSettled(run.status)) return null;
  const retries = retryList(run);
  if (retries.length === 0) return null;

  // ponytail: duplicate-rerun guard is client state only (a reload re-arms the
  // button, and runs carry no retry-of lineage); add a server-side retry route
  // with an idempotency key if duplicate paid runs ever bite.
  async function rerun() {
    setBusy(true);
    setError(null);
    try {
      // audience_query is always the sentinel at creation — the n8n flow
      // stamps the real version during the run.
      const view = await createRun({
        pro_ids: retries.map((r) => r.proId),
        audience_query: PENDING_AUDIENCE_QUERY,
        audience_run: run.audience_run,
        channels: run.channels,
        loop_config: run.loop_config,
        // RunDetail types the window as plain string; the server validates it.
        journey_window: run.journey_window as RunCreateInput["journey_window"],
      });
      setRetryRun(view.id);
    } catch (e) {
      setError(`Rerun could not be started: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel" aria-label="Retry log">
      <h2>Retry log</h2>
      <p>
        {retries.length} of {run.pro_ids.length} pros did not produce a winner.
        Rerunning starts a new run with the same audience lineage; this page and
        its results stay as they are.
      </p>
      <ul>
        {retries.map(({ proId, reason }) => (
          <li key={proId}>
            <code>{proId}</code> — {reason}
          </li>
        ))}
      </ul>
      <button
        type="button"
        onClick={rerun}
        disabled={busy || retryRun !== null || run.killed}
      >
        {busy ? "Starting rerun…" : `Rerun ${retries.length} pro${retries.length === 1 ? "" : "s"}`}
      </button>
      {run.killed && (
        <p className="error">
          Fleet kill switch is active — a rerun would queue but never start.
        </p>
      )}
      {retryRun && (
        <p role="status">
          Rerun started:{" "}
          <Link href={`/runs/${retryRun}`} target="_blank">
            {retryRun} (opens in a new tab)
          </Link>
        </p>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </section>
  );
}
