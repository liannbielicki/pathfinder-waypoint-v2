import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { WinnerReview } from "./WinnerReview";
import { RUN_FIXTURE } from "@/test/fixtures";
import type { RunDetail } from "@/lib/api";

const WINNER_RUN: RunDetail = {
  ...RUN_FIXTURE,
  status: "complete",
  candidates: [
    {
      id: "cand-1",
      pro_id: "pro_1",
      recommendation: {
        title: "Send open invoices reminder",
        mechanism: "invoice_delivery",
        pro_facing_concept: "Get paid faster.",
        manager_rationale: "Open AR is the signal.",
        channel: "sms",
      },
      critics: { block_kind: "none", reason: "grounded" },
      persona_evidence: {
        final: {
          panel: {
            items: [
              { persona_id: "solo_hustler", label: "Solo hustler", family: "solo_operators", role: "closest", fit_score: 1, rationale: "matches" },
            ],
            snapshot_version: "personas_2026_07",
          },
          reactions: [5.3],
        },
      },
      score: {
        final: {
          reduction_pp: 4.2, ci_lower_pp: 3.1, ci_upper_pp: 5.3,
          in_calibrated_range: true, calibration_version: "22cc4a1c89354327",
          baseline_confidence: "high", abstained: false,
        },
      },
      status: "generated",
    },
  ],
  winners: [
    { id: "win-1", pro_id: "pro_1", kind: "winner", candidate_id: "cand-1", rationale: "Open AR is the signal.", evidence: { org_id: "org_1" } },
  ],
  measurements: [
    { id: "m-1", winner_id: "win-1", indicators: [{ key: "invoices_sent", label: "Invoices sent", direction: "increase", source: "billing", window_days: 30, rationale: "r" }] },
  ],
};

describe("WinnerReview", () => {
  it("keeps handoff disabled until a persisted winner is ready", () => {
    render(
      <WinnerReview
        run={{ ...RUN_FIXTURE, status: "running", winners: [] }}
        onHandoff={vi.fn()}
        handingOff={false}
      />,
    );
    expect(screen.getByRole("button", { name: /create lcm handoff/i })).toBeDisabled();
  });

  it("enables handoff when winner and measurement plan are persisted", () => {
    render(<WinnerReview run={WINNER_RUN} onHandoff={vi.fn()} handingOff={false} />);
    expect(screen.getByRole("button", { name: /create lcm handoff/i })).toBeEnabled();
  });

  it("tags each winner with its channel and filters by it", async () => {
    const two: RunDetail = {
      ...WINNER_RUN,
      candidates: [
        WINNER_RUN.candidates[0],
        { ...WINNER_RUN.candidates[0], id: "cand-2", pro_id: "pro_2",
          recommendation: { ...WINNER_RUN.candidates[0].recommendation, title: "Email the recap", channel: "email" } },
      ],
      winners: [
        WINNER_RUN.winners[0],
        { ...WINNER_RUN.winners[0], id: "win-2", pro_id: "pro_2", candidate_id: "cand-2" },
      ],
    };
    render(<WinnerReview run={two} onHandoff={vi.fn()} handingOff={false} />);
    expect(screen.getByRole("heading", { name: /open invoices reminder.*SMS/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Email the recap.*EMAIL/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "email (1)" }));
    expect(screen.queryByRole("heading", { name: /open invoices reminder/ })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Email the recap/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "all (2)" }));
    expect(screen.getAllByRole("heading", { level: 4 })).toHaveLength(2);
  });

  it("shows real persona labels, never fabricated identities", () => {
    render(<WinnerReview run={WINNER_RUN} onHandoff={vi.fn()} handingOff={false} />);
    expect(screen.getByText("Solo hustler")).toBeInTheDocument();
    expect(screen.getByText(/counterweight|closest/)).toBeInTheDocument();
  });

  it("labels the score with confidence and calibration provenance", () => {
    render(<WinnerReview run={WINNER_RUN} onHandoff={vi.fn()} handingOff={false} />);
    expect(screen.getByText(/4\.2/)).toBeInTheDocument();
    expect(screen.getByText(/22cc4a1c89354327/)).toBeInTheDocument();
    expect(screen.getAllByText(/invoices_sent|Invoices sent/).length).toBeGreaterThan(0);
  });

  it("shows the org uuid the message is attached to", () => {
    render(<WinnerReview run={WINNER_RUN} onHandoff={vi.fn()} handingOff={false} />);
    expect(screen.getByText(/org uuid/i)).toBeInTheDocument();
    expect(screen.getByText("org_1")).toBeInTheDocument();
  });

  it("shows how many evolve rounds the result took", () => {
    render(<WinnerReview run={WINNER_RUN} onHandoff={vi.fn()} handingOff={false} />);
    // One candidate row per round: the fixture's single candidate → 1 round.
    expect(screen.getByText(/evolve loop: 1 round/i)).toBeInTheDocument();
  });

  it("shows a compact channel, selection, warm-start, and model explanation", () => {
    const run: RunDetail = {
      ...WINNER_RUN,
      candidates: [{
        ...WINNER_RUN.candidates[0],
        round: 1,
        recommendation: {
          ...WINNER_RUN.candidates[0].recommendation,
          channel_override_reason: "email engagement was low",
          cta: { label: "Online Booking", url: "https://example.test/booking" },
        },
        persona_evidence: {
          ...WINNER_RUN.candidates[0].persona_evidence,
          final: {
            ...WINNER_RUN.candidates[0].persona_evidence.final,
            tier: "deep",
            model: "claude-sonnet-5",
          },
        },
      }],
      winners: [{
        ...WINNER_RUN.winners[0],
        evidence: { ...WINNER_RUN.winners[0].evidence, suggested_channel: "email" },
      }],
      rounds: [{
        pro_id: "pro_1", round: 1, mechanism: "invoice_delivery",
        outcome: "win", score_pp: 4.2,
        ranking: {
          order: [
            { token: "c1", candidate_id: "cand-1", mechanism: "invoice_delivery", rank: 1, score: 0.82 },
            { token: "c2", candidate_id: "cand-2", mechanism: "review_request", rank: 2, score: 0.65 },
          ],
          screen_scores_pp: { c1: 4.2, c2: 1.1 },
          selection_reason: "all_rankable_candidates_screened",
          ranker_model: "claude-sonnet-5",
          screen_model: "claude-sonnet-5",
          warm_start: { outcome: "cold" },
        },
      }],
    };
    render(<WinnerReview run={run} onHandoff={vi.fn()} handingOff={false} />);
    expect(screen.getByText(/selected SMS · RECO email/i)).toBeVisible();
    expect(screen.getByText(/override: email engagement was low/i)).toBeVisible();
    expect(screen.getByText(/2 candidates compared across 1 round/i)).toBeVisible();
    expect(screen.getByText(/warm start: no/i)).toBeVisible();
    expect(screen.getByText(/claude-sonnet-5/i)).toBeVisible();
    expect(screen.getByText(/technical selection evidence/i)).toBeVisible();
    expect(screen.getByRole("link", { name: "Online Booking" })).toHaveAttribute(
      "href", "https://example.test/booking",
    );
  });

  it("does not call a full fallback panel undersized", () => {
    const run: RunDetail = {
      ...WINNER_RUN,
      winners: [{
        ...WINNER_RUN.winners[0],
        evidence: {
          ...WINNER_RUN.winners[0].evidence,
          panel_disclaimer: { final: "fallback panel: insufficient strong matches" },
        },
      }],
    };
    render(<WinnerReview run={run} onHandoff={vi.fn()} handingOff={false} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/fallback panel/i);
    expect(screen.getByRole("alert")).not.toHaveTextContent(/fewer personas/i);
  });

  it("counts every round for a Pro, including discarded losers", () => {
    const loser = { ...WINNER_RUN.candidates[0], id: "cand-0", status: "discarded" };
    render(
      <WinnerReview
        run={{ ...WINNER_RUN, candidates: [loser, ...WINNER_RUN.candidates] }}
        onHandoff={vi.fn()}
        handingOff={false}
      />,
    );
    // Two candidate rows for pro_1 (one discarded loser + the winner) → 2 rounds.
    expect(screen.getByText(/evolve loop: 2 rounds/i)).toBeInTheDocument();
  });

  it("counts distinct rounds, not candidate rows, when CANDIDATE_COUNT rows share a round", () => {
    // 3 candidates persisted per round (CANDIDATE_COUNT=3) across 2 rounds
    // must show 2 rounds, not 6.
    const rowsForRound = (round: number) =>
      [0, 1, 2].map((i) => ({
        ...WINNER_RUN.candidates[0],
        id: `cand-r${round}-${i}`,
        round,
        status: i === 0 ? "generated" : "discarded",
      }));
    render(
      <WinnerReview
        run={{
          ...WINNER_RUN,
          candidates: [...rowsForRound(1), ...rowsForRound(2)],
        }}
        onHandoff={vi.fn()}
        handingOff={false}
      />,
    );
    expect(screen.getByText(/evolve loop: 2 rounds/i)).toBeInTheDocument();
  });

  it("renders an unsupported historical no-action as inconclusive", () => {
    render(
      <WinnerReview
        run={{
          ...RUN_FIXTURE,
          status: "no_action",
          winners: [{ id: "w", pro_id: "pro_1", kind: "no_action", candidate_id: null, rationale: "no_candidate_cleared_floor", evidence: {} }],
        }}
        onHandoff={vi.fn()}
        handingOff={false}
      />,
    );
    expect(screen.getByRole("heading", { name: /inconclusive/i })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  const noActionRun = (candidates: RunDetail["candidates"]): RunDetail => ({
    ...RUN_FIXTURE,
    status: "no_action",
    candidates,
    winners: [{ id: "w", pro_id: "pro_1", kind: "no_action", candidate_id: null, rationale: "no_candidate_cleared_floor", evidence: {} }],
  });
  const loser = (id: string, round: number) => ({
    ...WINNER_RUN.candidates[0],
    id,
    round,
    status: "discarded",
    score: { screen: { reduction_pp: 0.2 } },
  });

  it("does not claim a completed search from historical candidate rows alone", () => {
    render(
      <WinnerReview
        run={noActionRun([loser("c1", 1), loser("c2", 2)])}
        onHandoff={vi.fn()}
        handingOff={false}
      />,
    );
    expect(screen.getByText(/search ended without a supported decision/i)).toBeInTheDocument();
  });

  it("explains no-action as a final-check rejection when a champion existed", () => {
    const champion = {
      ...WINNER_RUN.candidates[0],
      id: "champ",
      round: 20,
      status: "champion",
      score: {
        screen: { reduction_pp: 3.0 },
        final: {
          reduction_pp: 0.4, ci_lower_pp: -0.2, ci_upper_pp: 1.0,
          in_calibrated_range: true, calibration_version: "v", baseline_confidence: "high",
          abstained: false,
        },
      },
    };
    render(
      <WinnerReview
        run={noActionRun([loser("c1", 1), champion])}
        onHandoff={vi.fn()}
        handingOff={false}
      />,
    );
    expect(screen.getByText(/rejected at the final check/i)).toBeInTheDocument();
    expect(screen.getByText(/round-20 champion/i)).toBeInTheDocument();
  });

  it("flags no-action as incomplete when the champion never got a final check", () => {
    const champion = {
      ...WINNER_RUN.candidates[0],
      id: "champ",
      round: 5,
      status: "champion",
      score: { screen: { reduction_pp: 3.0 } },
    };
    render(
      <WinnerReview
        run={noActionRun([champion])}
        onHandoff={vi.fn()}
        handingOff={false}
      />,
    );
    expect(screen.getByText(/incomplete: champion never confirmed/i)).toBeInTheDocument();
  });

  it("flags degraded no-action when the final panel abstained", () => {
    const champion = {
      ...WINNER_RUN.candidates[0],
      id: "champ",
      round: 3,
      status: "champion",
      score: {
        screen: { reduction_pp: 3.0 },
        final: { reduction_pp: null, ci_lower_pp: null, ci_upper_pp: null, abstained: true, abstain_reason: "no_reactions" },
      },
    };
    render(
      <WinnerReview
        run={noActionRun([champion])}
        onHandoff={vi.fn()}
        handingOff={false}
      />,
    );
    expect(screen.getByText(/degraded: final evidence unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/no_reactions/)).toBeInTheDocument();
  });

  it("renders all-suppressed runs as inconclusive, never a clean no-action", () => {
    // Critic-suppressed rounds never reached the panel; the card must not
    // claim they failed the screen floor.
    const suppressed = (id: string, round: number) => ({
      ...WINNER_RUN.candidates[0],
      id,
      round,
      status: "suppressed",
      score: {},
    });
    render(
      <WinnerReview
        run={noActionRun([suppressed("c1", 1), suppressed("c2", 2)])}
        onHandoff={vi.fn()}
        handingOff={false}
      />,
    );
    expect(screen.getByText(/inconclusive: no round was ever panel-evaluated/i)).toBeInTheDocument();
    expect(screen.getByText(/2 blocked before evaluation/)).toBeInTheDocument();
    expect(screen.queryByText(/successful decision/i)).not.toBeInTheDocument();
  });

  it("keys the verdict on the backend rationale, avoiding contradiction", () => {
    // champion_final_missing beats any candidate-derived guess.
    const champion = {
      ...WINNER_RUN.candidates[0],
      id: "champ",
      round: 4,
      status: "champion",
      score: { screen: { reduction_pp: 3.0 } },
    };
    render(
      <WinnerReview
        run={{
          ...noActionRun([champion]),
          winners: [{ id: "w", pro_id: "pro_1", kind: "no_action", candidate_id: null, rationale: "champion_final_missing", evidence: {} }],
        }}
        onHandoff={vi.fn()}
        handingOff={false}
      />,
    );
    expect(screen.getByText(/incomplete: champion never confirmed/i)).toBeInTheDocument();
    expect(screen.getByText("champion_final_missing")).toBeInTheDocument();
  });

  it("does not call a generation failure a no-action verdict", () => {
    // No candidates at all: the loop never produced a round. This must read as
    // inconclusive, never as "a successful decision".
    render(
      <WinnerReview
        run={{
          ...noActionRun([]),
          winners: [{ id: "w", pro_id: "pro_1", kind: "no_action", candidate_id: null, rationale: "no_round_was_ever_generated", evidence: {} }],
        }}
        onHandoff={vi.fn()}
        handingOff={false}
      />,
    );
    expect(screen.getByText(/inconclusive: no idea was ever generated/i)).toBeInTheDocument();
    expect(screen.queryByText(/successful decision/i)).not.toBeInTheDocument();
  });

  it("surfaces unevaluated rounds as partial evidence", () => {
    const unavailable = { ...WINNER_RUN.candidates[0], id: "c9", round: 2, status: "discarded", score: {} };
    render(
      <WinnerReview
        run={noActionRun([loser("c1", 1), unavailable])}
        onHandoff={vi.fn()}
        handingOff={false}
      />,
    );
    expect(screen.getByText(/1 evaluation unavailable/i)).toBeInTheDocument();
  });

  it("does not treat a scored loss mixed with a blocked attempt as exhausted search", () => {
    render(<WinnerReview run={{
      ...noActionRun([loser("c1", 1), loser("c2", 2)]),
      winners: [{ id: "w", pro_id: "pro_1", kind: "no_action", candidate_id: null, rationale: "no_round_cleared_screen", evidence: {} }],
      rounds: [
        { pro_id: "pro_1", round: 1, mechanism: "m1", outcome: "lose", score_pp: 0.2 },
        { pro_id: "pro_1", round: 2, mechanism: "m2", outcome: "suppressed", score_pp: null },
      ],
    }} onHandoff={vi.fn()} handingOff={false} />);
    expect(screen.getByRole("heading", { name: /inconclusive/i })).toBeVisible();
    expect(screen.getByText(/1 scored round; 1 blocked before evaluation; 0 evaluation unavailable/)).toBeInTheDocument();
  });
});
