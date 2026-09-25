import type { Candidate, EvolveRound, Winner } from "@/lib/api";

export function decisionKind(winner: Winner, rounds: EvolveRound[], candidates: Candidate[], scoredLossBudget: number): "winner" | "no_action" | "final_rejected" | "inconclusive" | "other" {
  if (winner.kind === "winner") return "winner";
  if (winner.kind === "abstained") return winner.rationale.startsWith("inconclusive_") ? "inconclusive" : "other";
  if (["champion_final_missing", "all_candidates_abstained"].includes(winner.rationale)) return "inconclusive";
  if (candidates.some((c) => c.pro_id === winner.pro_id && c.status === "champion")) return "final_rejected";
  if (winner.rationale === "no_round_cleared_screen") {
    const scoredLosses = rounds.filter((r) => r.outcome === "lose" && r.score_pp != null).length;
    return scoredLosses >= Math.max(scoredLossBudget, 1) && rounds.at(-1)?.outcome === "lose"
      ? "no_action" : "inconclusive";
  }
  if (["no_candidate_cleared_floor", "no_round_was_ever_generated", "champion_final_missing"].includes(winner.rationale)) return "inconclusive";
  return "other";
}

export function roundOutcomes(rounds: EvolveRound[]) {
  return {
    scored: rounds.filter((r) => r.score_pp != null).length,
    blocked: rounds.filter((r) => r.outcome === "suppressed").length,
    unavailable: rounds.filter((r) => r.outcome === "unavailable" || (r.outcome === "lose" && r.score_pp == null)).length,
  };
}
