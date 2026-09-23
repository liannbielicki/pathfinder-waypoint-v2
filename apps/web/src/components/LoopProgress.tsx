"use client";

import type { EvolveRound, RunDetail } from "@/lib/api";
import { formatEstimate } from "./WinnerReview";

const DECISION_LABEL: Record<string, string> = {
  winner: "✓ winner",
  no_action: "no-action",
  abstained: "abstained",
};

// Every per-round number here comes from the cheap 3-persona SCREENING panel,
// never the held-out final: a "win" only means it beat the incumbent screen
// estimate on the panel-reaction bar (KEEP_DELTA_REACTION) and by KEEP_DELTA_PP.
// Label it as the screen result it is.
const OUTCOME_LABEL: Record<string, string> = {
  win: "screen win",
  lose: "screen lose",
};

// Best = last kept win. A win must still beat best + KEEP_DELTA_PP in pp (the
// reaction bar is the other half of the AND), so the max win score is the
// current best; losing rounds never count.
const bestOf = (rounds: EvolveRound[]) =>
  rounds.reduce<number | null>(
    (best, r) =>
      r.outcome === "win" && r.score_pp !== null && (best === null || r.score_pp > best)
        ? r.score_pp
        : best,
    null,
  );

export function LoopProgress({ run }: { run: RunDetail }) {
  // Optional-chained: a stale API deploy may serve payloads without rounds,
  // and this panel must never crash the rest of the run page.
  if (!run.rounds?.length) return null;

  const byPro = new Map<string, EvolveRound[]>();
  for (const r of run.rounds) {
    const list = byPro.get(r.pro_id);
    if (list) list.push(r);
    else byPro.set(r.pro_id, [r]);
  }
  const decided = new Map(run.winners.map((w) => [w.pro_id, w.kind]));
  const rows = [...byPro.entries()].map(([proId, rounds]) => ({
    proId,
    rounds,
    best: bestOf(rounds),
    decision: decided.get(proId),
    // A no-action with a formattable final estimate is exactly the no-action
    // card's "Rejected at the final check" ending: an abstained or missing
    // final has no numbers, so formatEstimate returns null for those.
    finalEstimate: formatEstimate(
      run.candidates.find((c) => c.pro_id === proId && c.status === "champion")?.score
        ?.final,
    ),
  }));
  // Pros still looping float to the top, then by current best descending.
  rows.sort(
    (a, b) =>
      (a.decision ? 1 : 0) - (b.decision ? 1 : 0) ||
      (b.best ?? -Infinity) - (a.best ?? -Infinity),
  );

  const looping = rows.filter((r) => !r.decision).length;
  const runBest = bestOf(run.rounds);
  const maxRounds = run.loop_config?.MAX_ROUNDS;

  return (
    <details className="panel loop-progress">
      <summary>
        Loop progress · {looping} of {run.pro_ids.length} pros looping
        {runBest !== null && (
          <> · best screen estimate so far {runBest.toFixed(1)} pp</>
        )}
      </summary>
      <div className="loop-rows">
        {rows.map(({ proId, rounds, best, decision, finalEstimate }) => (
          <details key={proId} aria-label={proId}>
            <summary>
              <code>{proId}</code> · loop {rounds.length}
              {maxRounds ? ` of ${maxRounds}` : ""}
              {best !== null && <> · best screen estimate {best.toFixed(1)} pp</>}
              {decision && <> · {DECISION_LABEL[decision] ?? decision}</>}
              {decision === "no_action" && finalEstimate !== null && (
                <> · Rejected at held-out final: {finalEstimate}</>
              )}
            </summary>
            <ol>
              {rounds.map((r) => (
                <li key={r.round}>
                  {r.mechanism} —{" "}
                  {r.score_pp !== null
                    ? `${r.score_pp.toFixed(1)} pp · ${OUTCOME_LABEL[r.outcome] ?? r.outcome}`
                    : (OUTCOME_LABEL[r.outcome] ?? r.outcome)}
                </li>
              ))}
            </ol>
          </details>
        ))}
      </div>
    </details>
  );
}
