import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RunStatus } from "./RunStatus";
import type { RunDetail } from "@/lib/api";

export const RUN_FIXTURE: RunDetail = {
  id: "run-1",
  status: "queued",
  pro_ids: ["pro_1"],
  audience_query: "audience_v7",
  audience_run: "2026-08-06T18:00:00Z",
  channels: ["sms"],
  config_version: "waypoint_v1",
  cost_limit_usd: "25.00",
  cost_reserved_usd: "0.40",
  cost_spent_usd: "0.12",
  stop_reason: null,
  created_at: "2026-08-06T18:00:00Z",
  stages: {},
  candidates: [],
  winners: [],
  measurements: [],
  handoffs: [],
  killed: false,
};

describe("RunStatus", () => {
  it.each([
    "queued", "running", "waiting", "degraded", "failed",
    "resumed", "stopped", "complete", "abstained", "no_action",
  ])("renders %s as an explicit state", (status) => {
    render(<RunStatus run={{ ...RUN_FIXTURE, status }} onKill={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent(status.replace("_", " "));
  });

  it("shows the stop reason when a run stops", () => {
    render(
      <RunStatus
        run={{ ...RUN_FIXTURE, status: "stopped", stop_reason: "budget_exhausted" }}
        onKill={vi.fn()}
      />,
    );
    expect(screen.getByText(/budget_exhausted/)).toBeVisible();
  });

  it("shows completed stages so the operator knows what happens next", () => {
    render(
      <RunStatus
        run={{
          ...RUN_FIXTURE,
          status: "running",
          stages: { context: { orgs: 1 }, generate: { pros_generated: 1 } },
        }}
        onKill={vi.fn()}
      />,
    );
    expect(screen.getByText("context")).toBeInTheDocument();
    expect(screen.getByText("generate")).toBeInTheDocument();
  });

  it("exposes cost and kill state", () => {
    render(<RunStatus run={{ ...RUN_FIXTURE, killed: true }} onKill={vi.fn()} />);
    expect(screen.getByText(/\$0\.12/)).toBeInTheDocument();
    expect(screen.getByText(/fleet kill/i)).toBeVisible();
  });

  it("disables kill once the run is terminal", () => {
    render(<RunStatus run={{ ...RUN_FIXTURE, status: "complete" }} onKill={vi.fn()} />);
    expect(screen.getByRole("button", { name: /kill run/i })).toBeDisabled();
  });
});
