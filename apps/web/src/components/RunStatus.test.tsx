import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { RunStatus } from "./RunStatus";
import { RUN_FIXTURE, WINNER_FIXTURE as WINNER } from "@/test/fixtures";

describe("RunStatus", () => {
  it.each([
    "queued", "running", "waiting", "degraded", "failed",
    "resumed", "stopped", "complete", "abstained", "no_action",
  ])("renders %s as an explicit state", (status) => {
    render(<RunStatus run={{ ...RUN_FIXTURE, status }} onKill={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent(status.replace("_", " "));
  });

  it("shows the n8n-reported query version once stamped, pending until then", () => {
    const { rerender } = render(
      <RunStatus run={{ ...RUN_FIXTURE, audience_query: "pending_n8n" }} onKill={vi.fn()} />,
    );
    expect(screen.getByText(/awaiting n8n report/i)).toBeVisible();
    rerender(
      <RunStatus run={{ ...RUN_FIXTURE, audience_query: "audience_v8" }} onKill={vi.fn()} />,
    );
    expect(screen.getByText("audience_v8")).toBeVisible();
    expect(screen.queryByText(/awaiting n8n report/i)).not.toBeInTheDocument();
  });

  it("marks lineage unresolved (not awaiting) on a terminal run n8n never stamped", () => {
    render(
      <RunStatus
        run={{ ...RUN_FIXTURE, status: "failed", audience_query: "pending_n8n" }}
        onKill={vi.fn()}
      />,
    );
    expect(screen.getByText(/never reported a query version/i)).toBeVisible();
    expect(screen.queryByText(/awaiting n8n report/i)).not.toBeInTheDocument();
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

  it("lists the evolve-loop stages so the operator knows what happens next", () => {
    render(
      <RunStatus
        run={{
          ...RUN_FIXTURE,
          status: "running",
          stages: { context: { orgs: 1 }, evolve: { rounds: 3 } },
        }}
        onKill={vi.fn()}
      />,
    );
    for (const stage of ["context", "evolve", "final", "score", "measure", "ready"]) {
      expect(screen.getByText(stage)).toBeInTheDocument();
    }
    expect(screen.queryByText("generate")).not.toBeInTheDocument();
    expect(screen.queryByText("search")).not.toBeInTheDocument();
  });

  it("shows the immutable run settings snapshot with plain labels first", () => {
    render(<RunStatus run={RUN_FIXTURE} onKill={vi.fn()} />);
    const settings = screen.getByRole("region", { name: /run settings/i });
    for (const label of [
      /max rounds per pro/i, /dry mechanisms before stopping/i,
      /refine attempts per mechanism/i, /min improvement to keep/i,
      /stop-early reduction/i, /ideas per round/i, /ranker tie margin/i,
    ]) {
      expect(within(settings).getByText(label)).toBeInTheDocument();
    }
    expect(within(settings).getByText("10")).toBeInTheDocument();
    expect(within(settings).getByText("0.5")).toBeInTheDocument();
    expect(within(settings).getByText("0.05")).toBeInTheDocument();
    // Audit view, never an editor: no inputs inside the snapshot.
    expect(within(settings).queryAllByRole("textbox")).toEqual([]);
    expect(within(settings).queryAllByRole("spinbutton")).toEqual([]);
  });

  it("shows per-Pro progress counts from winners", () => {
    render(
      <RunStatus
        run={{
          ...RUN_FIXTURE,
          status: "running",
          pro_ids: ["pro_1", "pro_2", "pro_3"],
          winners: [
            WINNER,
            { ...WINNER, id: "w2", pro_id: "pro_2", kind: "no_action" },
          ],
        }}
        onKill={vi.fn()}
      />,
    );
    expect(screen.getByText(/2 of 3 pros decided/i)).toBeVisible();
    expect(screen.getByText(/1 winner/i)).toBeVisible();
    expect(screen.getByText(/1 no-action/i)).toBeVisible();
  });

  it("shows an agents-in-parallel pill only when agents are in flight", () => {
    const { rerender } = render(
      <RunStatus run={{ ...RUN_FIXTURE, status: "running", agents_in_flight: 3 }} onKill={vi.fn()} />,
    );
    expect(screen.getByText(/3 agents in parallel/i)).toBeVisible();
    rerender(
      <RunStatus run={{ ...RUN_FIXTURE, status: "running", agents_in_flight: 0 }} onKill={vi.fn()} />,
    );
    expect(screen.queryByText(/in parallel/i)).not.toBeInTheDocument();
  });

  it("names possible paid work when a stop happened after spend", () => {
    render(
      <RunStatus
        run={{ ...RUN_FIXTURE, status: "stopped", stop_reason: "budget_exhausted",
               cost_spent_usd: "0.12" }}
        onKill={vi.fn()}
      />,
    );
    expect(screen.getByText(/paid work may have occurred/i)).toBeVisible();
  });

  it("requires a typed confirmation before killing a live run", async () => {
    const onKill = vi.fn();
    render(<RunStatus run={{ ...RUN_FIXTURE, status: "running" }} onKill={onKill} />);
    await userEvent.click(screen.getByRole("button", { name: /kill run/i }));
    expect(onKill).not.toHaveBeenCalled();
    const confirm = screen.getByLabelText(/type "kill" to confirm/i);
    await userEvent.type(confirm, "kill");
    await userEvent.click(screen.getByRole("button", { name: /confirm kill/i }));
    expect(onKill).toHaveBeenCalledTimes(1);
  });

  it("never offers the kill action on a terminal run", () => {
    render(<RunStatus run={{ ...RUN_FIXTURE, status: "complete" }} onKill={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /kill/i })).not.toBeInTheDocument();
  });

  it("exposes cost and kill state", () => {
    render(<RunStatus run={{ ...RUN_FIXTURE, killed: true }} onKill={vi.fn()} />);
    expect(screen.getByText(/\$0\.12/)).toBeInTheDocument();
    expect(screen.getByText(/fleet kill/i)).toBeVisible();
  });

  it("announces status changes politely for screen readers", () => {
    render(<RunStatus run={{ ...RUN_FIXTURE, status: "running" }} onKill={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
  });
});
