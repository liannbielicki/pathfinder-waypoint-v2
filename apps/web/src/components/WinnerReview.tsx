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

function Rounds({ rounds, championRound }: { rounds: number; championRound?: number }) {
  if (rounds === 0) return null;
  return (
    <p>
      <small>
        evolve loop: {rounds} round{rounds === 1 ? "" : "s"}
        {championRound != null ? `, round ${championRound} is the champion` : ""}
      </small>
    </p>
  );
}

// The endings a no_action can hide, keyed on the backend's recorded rationale
// first (candidate rows only fill in detail and legacy rows), so the verdict
// can never contradict the rationale printed on the card. Returns [title, body].
function noActionEnding(
  rationale: string,
  champion: Candidate | undefined,
  screened: number,
  rounds: number,
): [string, string] {
  const finalScore = champion?.score?.final;
  const label =
    champion?.round != null ? `The round-${champion.round} champion` : "The champion";
  if (rationale === "champion_final_missing" || (champion && !finalScore)) {
    return ["Incomplete: champion never confirmed.",
      `${label} won the screen but the run stopped before its held-out final check. ` +
      "Treat this as an incomplete run, not a conclusion."];
  }
  if (rationale.startsWith("all_candidates_abstained") || finalScore?.abstained) {
    return ["Degraded: final evidence unavailable.",
      `${label} won the screen, but the held-out final panel could not produce a ` +
      "calibrated estimate. This is missing evidence, not a verdict on the idea."];
  }
  if (champion) {
    return ["Rejected at the final check.",
      `${label} cleared the screen but failed the held-out final — it needed ` +
      "≥ 1.0 pp with a positive CI lower bound. The stronger panel did not " +
      "confirm the screen result."];
  }
  if (rounds > 0 && screened === 0) {
    return ["Inconclusive: no round was ever panel-evaluated.",
      "The bottleneck was idea generation or evaluation, not the evidence — " +
      "treat this as inconclusive, not a clean no-action."];
  }
  const cleared = screened > 0
    ? `None of the ${screened} screened round${screened === 1 ? "" : "s"} cleared`
    : "No evolve round cleared";
  return ["Conclusion: not worth a touch.",
    `${cleared} the 1.0 pp support floor on the screen panel, so the loop ` +
    "stopped and recommended no action. This is a successful decision, not a failure."];
}

function NoActionCard({
  winner,
  proCandidates,
}: {
  winner: Winner;
  proCandidates: Candidate[];
}) {
  const rounds = proCandidates.length;
  const champion = proCandidates.find((c) => c.status === "champion");
  const finalScore = champion?.score?.final;
  const screened = proCandidates.filter((c) => c.score?.screen).length;
  const suppressed = proCandidates.filter((c) => c.status === "suppressed").length;
  const unevaluated = proCandidates.filter(
    (c) => c.status === "discarded" && !c.score?.screen,
  ).length;
  const ending = noActionEnding(winner.rationale, champion, screened, rounds);
  return (
    <div className="card">
      <h4>No action for {winner.pro_id}</h4>
      <p>
        <strong>{ending[0]}</strong> {ending[1]}
      </p>
      {finalScore != null && <ScoreBlock score={finalScore} />}
      {(suppressed > 0 || unevaluated > 0) && (
        <p>
          {suppressed > 0 && `${suppressed} round${suppressed === 1 ? "" : "s"} critic-suppressed (never panel-evaluated)`}
          {suppressed > 0 && unevaluated > 0 && "; "}
          {unevaluated > 0 && `${unevaluated} round${unevaluated === 1 ? "" : "s"} could not be evaluated (panel unavailable)`}
          {" — the evidence above is partial."}
        </p>
      )}
      <p>
        Nothing will be handed off.{" "}
        <small className="technical">{winner.rationale}</small>
      </p>
      <Rounds rounds={rounds} />
    </div>
  );
}

function WinnerCard({
  winner,
  candidate,
  rounds,
  proCandidates,
}: {
  winner: Winner;
  candidate?: Candidate;
  rounds: number;
  proCandidates: Candidate[];
}) {
  if (winner.kind === "no_action") {
    return <NoActionCard winner={winner} proCandidates={proCandidates} />;
  }
  if (winner.kind === "abstained") {
    return (
      <div className="card">
        <h4>Abstained for {winner.pro_id}</h4>
        <p>{winner.rationale}</p>
        <Rounds rounds={rounds} />
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
      <p>
        <small>
          org uuid: <code>{String(winner.evidence?.org_id ?? winner.pro_id)}</code>
        </small>
      </p>
      <Rounds rounds={rounds} championRound={candidate?.round} />
      {winner.evidence?.panel_disclaimer != null && (
        <p role="alert">
          <strong>⚠ Degraded panel:</strong>{" "}
          {Object.entries(
            winner.evidence.panel_disclaimer as Record<string, string>,
          )
            .map(([stage, detail]) => `${stage}: ${detail}`)
            .join("; ")}
          {" — evaluated with fewer personas than requested."}
        </p>
      )}
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
  // One candidate row is persisted per evolve round, so candidates-per-Pro is
  // both the loop count and the evidence trail for that result.
  const candidatesByPro = new Map<string, Candidate[]>();
  for (const c of run.candidates) {
    const list = candidatesByPro.get(c.pro_id) ?? [];
    list.push(c);
    candidatesByPro.set(c.pro_id, list);
  }

  return (
    <section className="panel" aria-label="Winner review">
      <h2>Decision evidence</h2>
      {winners.length === 0 && <p>No decision yet — the run has not reached scoring.</p>}
      {winners.map((winner) => (
        <WinnerCard
          key={winner.id}
          winner={winner}
          candidate={winner.candidate_id ? candidateById.get(winner.candidate_id) : undefined}
          rounds={(candidatesByPro.get(winner.pro_id) ?? []).length}
          proCandidates={candidatesByPro.get(winner.pro_id) ?? []}
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
