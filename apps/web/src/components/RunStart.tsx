"use client";

import { useState } from "react";
import { createRun, type RunView } from "@/lib/api";

export function RunStart({ onStarted }: { onStarted: (run: RunView) => void }) {
  const [proIds, setProIds] = useState("");
  const [audienceQuery, setAudienceQuery] = useState("");
  const [audienceRun, setAudienceRun] = useState("");
  const [channel, setChannel] = useState("sms");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const ids = proIds.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      // Async by default: the API returns 202 immediately; we navigate and poll.
      const run = await createRun({
        pro_ids: ids,
        audience_query: audienceQuery,
        audience_run: audienceRun,
        channels: [channel],
      });
      onStarted(run);
    } catch (e) {
      setError(`Run could not be started: ${e instanceof Error ? e.message : e}`);
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="panel">
      <h2>Start a run</h2>
      <p>
        The supplied audience is already SQL-suppressed (DNC applied upstream).
        Waypoint validates identifiers and preserves lineage; it never sends.
      </p>
      <label htmlFor="pro-ids">Pro IDs (one per line)</label>
      <textarea
        id="pro-ids"
        value={proIds}
        onChange={(e) => setProIds(e.target.value)}
        rows={5}
        required
      />
      <label htmlFor="audience-query">Audience query version</label>
      <input
        id="audience-query"
        value={audienceQuery}
        onChange={(e) => setAudienceQuery(e.target.value)}
        placeholder="audience_v7"
        required
      />
      <label htmlFor="audience-run">Audience run timestamp</label>
      <input
        id="audience-run"
        value={audienceRun}
        onChange={(e) => setAudienceRun(e.target.value)}
        placeholder="2026-08-06T18:00:00Z"
        required
      />
      <label htmlFor="channel">Channel</label>
      <select id="channel" value={channel} onChange={(e) => setChannel(e.target.value)}>
        <option value="sms">sms</option>
        <option value="email">email</option>
      </select>
      <button type="submit" disabled={busy || ids.length === 0}>
        {busy ? "Starting…" : `Start run (${ids.length} pros)`}
      </button>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </form>
  );
}
