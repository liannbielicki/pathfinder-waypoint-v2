"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, type CallItem, getCalls, updateCall } from "@/lib/api";

// Operator work list: every winner Waypoint recommended over the call channel.
// SMS and email winners go to LCM; these are placed by Pathfinder staff.
export default function CallsPage() {
  const [calls, setCalls] = useState<CallItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showDone, setShowDone] = useState(false);
  const [notes, setNotes] = useState<Record<string, string>>({});

  useEffect(() => {
    getCalls()
      .then(setCalls)
      .catch((e) =>
        setError(
          e instanceof ApiError && e.status === 401
            ? "Log in on the start page first."
            : `Calls could not be loaded: ${e instanceof Error ? e.message : e}`,
        ),
      );
  }, []);

  async function save(call: CallItem, status: CallItem["status"]) {
    try {
      const updated = await updateCall(call.winner_id, {
        status,
        note: notes[call.winner_id] ?? call.note,
      });
      setCalls((prev) => prev?.map((c) => (c.winner_id === updated.winner_id ? updated : c)) ?? null);
    } catch (e) {
      setError(`Could not save: ${e instanceof Error ? e.message : e}`);
    }
  }

  const visible = calls?.filter((c) => showDone || c.status === "todo") ?? [];
  const open = calls?.filter((c) => c.status === "todo").length ?? 0;

  return (
    <main>
      <p>
        <Link href="/">← Start page</Link>
      </p>
      <h1>Calls to place</h1>
      <p className="helper">
        {calls ? `${open} open` : "Loading…"} · Waypoint picked a phone call for these Pros.
        They are not sent to LCM; work them here.
      </p>
      <label>
        <input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} />
        {" "}Show done
      </label>
      {error && <p role="alert" className="error">{error}</p>}
      {calls && visible.length === 0 && <p role="status">No calls to place.</p>}
      {visible.map((call) => (
        <section key={call.winner_id} className="card" aria-label={call.title}>
          <h2>{call.title}</h2>
          <p>
            Pro <code>{call.pro_uuid ?? call.pro_id}</code> · org{" "}
            <code>{call.org_id || "?"}</code> ·{" "}
            {Array.isArray(call.flags) && call.flags.includes("dnc_call") && (
              <strong>DNC on file — not a marketing call · </strong>
            )}
            {Array.isArray(call.flags) && call.flags.includes("phone_shared") && (
              <strong>shared phone line · </strong>
            )}
            <Link href={`/runs/${call.run_id}`}>run</Link> ·{" "}
            {new Date(call.created_at).toLocaleDateString()}
          </p>
          <p><strong>Agenda:</strong> {call.pro_facing_concept}</p>
          {call.actions.length > 0 && (
            <ul>{call.actions.map((a, i) => <li key={i}>{a}</li>)}</ul>
          )}
          <p className="helper">{call.manager_rationale}</p>
          {call.alternatives.length > 0 && (
            <details>
              <summary>Other ideas for this Pro ({call.alternatives.length})</summary>
              <ol>
                {call.alternatives.map((alt, i) => (
                  <li key={i}>
                    <strong>{alt.title}</strong>
                    {alt.score_pp != null && <small> · {alt.score_pp.toFixed(1)} pp</small>}
                    {alt.channel && alt.channel !== "call" && <small> · {alt.channel}</small>}
                    <br />
                    {alt.pro_facing_concept}
                    {alt.actions.length > 0 && (
                      <ul>{alt.actions.map((a, j) => <li key={j}>{a}</li>)}</ul>
                    )}
                  </li>
                ))}
              </ol>
            </details>
          )}
          <label htmlFor={`note-${call.winner_id}`}>Note</label>
          <input
            id={`note-${call.winner_id}`}
            value={notes[call.winner_id] ?? call.note}
            onChange={(e) => setNotes({ ...notes, [call.winner_id]: e.target.value })}
            placeholder="Who you spoke to, outcome, follow-up"
          />
          {call.status === "todo" ? (
            <button type="button" onClick={() => save(call, "done")}>Mark done</button>
          ) : (
            <button type="button" onClick={() => save(call, "todo")}>Reopen</button>
          )}
        </section>
      ))}
    </main>
  );
}
