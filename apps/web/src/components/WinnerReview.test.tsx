import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WinnerReview } from "./WinnerReview";
import { RUN_FIXTURE } from "./RunStatus.test";
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

  it("renders no-action as a legitimate outcome, not an error", () => {
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
    expect(screen.getByText(/no action/i)).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
