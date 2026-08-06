"use client";

import type { Candidate, RunDetail, Winner } from "@/lib/api";

function ScoreBlock({ score }: { score: Record<string, unknown> }) {
  const reduction = score.reduction_pp as number | null;
  const lo = score.ci_lower_pp as number | null;
  const hi = score.ci_upper_pp as number | null;
  if (reduction == null || lo == null || hi == null) {
    return (
      <p>
        No calibrated estimate — the panel abstained
        {score.abstain_reason ? ` (${String(score.abstain_reason)})` : ""}.
      </p>
    );
  }
  return (
    <p>
      Estimated churn reduction: <strong>{reduction?.toFixed(1)} pp</strong>{" "}
      (CI {lo?.toFixed(1)}–{hi?.toFixed(1)} pp)
      {!score.in_calibrated_range &&
        " — calibrated extrapolation, outside the fitted reaction range"}
      <br />
      <small>
        baseline confidence: {String(score.baseline_confidence)} · calibration{" "}
        {String(score.calibration_version)}
      </small>
    </p>
  );
}

function WinnerCard({ winner, candidate }: { winner: Winner; candidate?: Candidate }) {
  if (winner.kind === "no_action") {
    return (
      <div className="card">
        <h4>No action for {winner.pro_id}</h4>
        <p>
          The panel evidence does not support a touch for this pro ({winner.rationale}).
          This is a legitimate outcome; nothing will be handed off.
        </p>
      </div>
    );
  }
  if (winner.kind === "abstained") {
    return (
      <div className="card">
        <h4>Abstained for {winner.pro_id}</h4>
        <p>{winner.rationale}</p>
      </div>
    );
  }
  const rec = candidate?.recommendation;
  const finalEvidence = candidate?.persona_evidence?.final;
  const finalScore = candidate?.score?.final;
  return (
    <div className="card">
      <h4>{String(rec?.title ?? "Winner")}</h4>
      <p>{String(rec?.pro_facing_concept ?? "")}</p>
      <p>
        <small>
          mechanism: {String(rec?.mechanism)} · channel: {String(rec?.channel)}
        </small>
      </p>
      <h5>Why (manager rationale)</h5>
      <p>{winner.rationale}</p>
      {finalScore && <ScoreBlock score={finalScore} />}
      {finalEvidence?.panel?.items && (
        <>
          <h5>Persona panel (final check)</h5>
          <ul>
            {finalEvidence.panel.items.map((item, index) => (
              <li key={item.persona_id}>
                <strong>{item.label}</strong> — {item.role} · fit{" "}
                {item.fit_score.toFixed(2)}
                {finalEvidence.reactions
                  ? ` · reaction ${finalEvidence.reactions[index]}`
                  : ""}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export function WinnerReview({
  run,
  onHandoff,
  handingOff,
}: {
  run: RunDetail;
  onHandoff: () => void;
  handingOff: boolean;
}) {
  const winners = run.winners ?? [];
  const measuredWinnerIds = new Set(run.measurements.map((m) => m.winner_id));
  const ready = winners.filter(
    (w) => w.kind === "winner" && measuredWinnerIds.has(w.id),
  );
  const candidateById = new Map(run.candidates.map((c) => [c.id, c]));

  return (
    <section className="panel" aria-label="Winner review">
      <h2>Decision evidence</h2>
      {winners.length === 0 && <p>No decision yet — the run has not reached scoring.</p>}
      {winners.map((winner) => (
        <WinnerCard
          key={winner.id}
          winner={winner}
          candidate={winner.candidate_id ? candidateById.get(winner.candidate_id) : undefined}
        />
      ))}

      {run.measurements.length > 0 && (
        <>
          <h3>Measurement plan</h3>
          {run.measurements.map((m) => (
            <ul key={m.id}>
              {m.indicators.map((indicator) => (
                <li key={indicator.key}>
                  {indicator.label} (<code>{indicator.key}</code>) —{" "}
                  {indicator.direction} over {indicator.window_days} days, source{" "}
                  {indicator.source}
                </li>
              ))}
            </ul>
          ))}
        </>
      )}

      <p>
        Handoff is the boundary: Allison&apos;s LCM tool owns copy, personalization,
        the Iterable DNC failsafe, and sending. Waypoint never sends.
      </p>
      <button
        type="button"
        onClick={onHandoff}
        disabled={ready.length === 0 || handingOff}
      >
        {handingOff ? "Creating handoff…" : "Create LCM handoff"}
      </button>
    </section>
  );
}
