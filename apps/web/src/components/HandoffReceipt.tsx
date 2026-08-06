"use client";

import type { RunDetail } from "@/lib/api";

export function HandoffReceipt({ handoffs }: { handoffs: RunDetail["handoffs"] }) {
  if (handoffs.length === 0) return null;
  return (
    <section className="panel" aria-label="Handoff receipts">
      <h2>LCM handoff receipts</h2>
      <ul>
        {handoffs.map((handoff) => (
          <li key={handoff.id}>
            <code>{handoff.idempotency_key}</code> — {handoff.status}
            {handoff.response ? (
              <small> · response: {JSON.stringify(handoff.response)}</small>
            ) : (
              <small> · awaiting LCM response</small>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
