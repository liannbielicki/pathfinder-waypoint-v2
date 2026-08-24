import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { RetryPanel } from "./RetryPanel";
import { PENDING_AUDIENCE_QUERY, createRun, type Winner } from "@/lib/api";
import { RUN_FIXTURE } from "@/test/fixtures";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  createRun: vi.fn(),
}));

const winner = (pro_id: string, kind: Winner["kind"], rationale = ""): Winner => ({
  id: `w-${pro_id}`,
  pro_id,
  kind,
  candidate_id: null,
  rationale,
  evidence: {},
});

describe("RetryPanel", () => {
  it("renders nothing while the run is still in flight", () => {
    const { container } = render(
      <RetryPanel run={{ ...RUN_FIXTURE, status: "running" }} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when every pro won", () => {
    const { container } = render(
      <RetryPanel
        run={{
          ...RUN_FIXTURE,
          status: "complete",
          winners: [winner("pro_1", "winner")],
        }}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("logs failed, abstained, and no-action pros with reasons", () => {
    render(
      <RetryPanel
        run={{
          ...RUN_FIXTURE,
          status: "degraded",
          pro_ids: ["pro_1", "pro_2", "pro_3", "pro_4"],
          winners: [
            winner("pro_1", "winner"),
            winner("pro_2", "abstained", "org deprecated upstream"),
            winner("pro_3", "no_action", "evidence does not support a touch"),
          ],
        }}
      />,
    );
    expect(screen.queryByText(/pro_1/)).not.toBeInTheDocument();
    expect(screen.getByText(/pro_2/).closest("li")).toHaveTextContent(/org deprecated upstream/);
    expect(screen.getByText(/pro_3/).closest("li")).toHaveTextContent(/no action/);
    expect(screen.getByText(/pro_4/).closest("li")).toHaveTextContent(/no result recorded/);
  });

  it("reruns the logged pros with the same audience lineage", async () => {
    vi.mocked(createRun).mockResolvedValue({ ...RUN_FIXTURE, id: "run-2" });
    render(
      <RetryPanel
        run={{
          ...RUN_FIXTURE,
          status: "failed",
          pro_ids: ["pro_1", "pro_2"],
          winners: [winner("pro_1", "winner")],
        }}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /rerun 1 pro/i }));
    expect(createRun).toHaveBeenCalledWith({
      pro_ids: ["pro_2"],
      audience_query: PENDING_AUDIENCE_QUERY,
      audience_run: RUN_FIXTURE.audience_run,
      channels: RUN_FIXTURE.channels,
      loop_config: RUN_FIXTURE.loop_config,
    });
    const link = await screen.findByRole("link", { name: /run-2/ });
    expect(link).toHaveAttribute("href", "/runs/run-2");
    expect(screen.getByRole("button", { name: /rerun/i })).toBeDisabled();
  });

  it("surfaces a rerun failure instead of pretending it started", async () => {
    vi.mocked(createRun).mockRejectedValue(new Error("Failed to fetch"));
    render(<RetryPanel run={{ ...RUN_FIXTURE, status: "failed" }} />);
    await userEvent.click(screen.getByRole("button", { name: /rerun/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Failed to fetch/);
    expect(screen.getByRole("button", { name: /rerun/i })).toBeEnabled();
  });

  it("refuses to rerun while the fleet kill switch is active", () => {
    // createRun would 202 even during a kill (the API only enforces the switch
    // at claim time), so the panel must not offer a rerun that cannot start.
    render(<RetryPanel run={{ ...RUN_FIXTURE, status: "failed", killed: true }} />);
    expect(screen.getByRole("button", { name: /rerun/i })).toBeDisabled();
    expect(screen.getByText(/kill switch is active/i)).toBeVisible();
  });
});
