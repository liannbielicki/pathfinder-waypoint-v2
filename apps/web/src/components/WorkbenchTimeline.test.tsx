import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { WorkbenchTimeline } from "./WorkbenchTimeline";

describe("WorkbenchTimeline", () => {
  it("shows the PII gate and removal ledger without raw values", () => {
    render(
      <WorkbenchTimeline
        trace={{
          stages: [
            { name: "pii_gate", status: "succeeded", data: { removed_count: 2, removed: [{ path: "email", category: "field", reason: "PII" }] } },
          ],
          warnings: [],
          outputs: {},
        }}
      />,
    );
    expect(screen.getByText("pii_gate")).toBeInTheDocument();
    expect(screen.getByText(/removed 2 fields/i)).toBeInTheDocument();
    expect(screen.queryByText(/@/)).not.toBeInTheDocument();
  });

  it("shows the exact source failure instead of only a generic failure", () => {
    render(<WorkbenchTimeline trace={{
      stages: [{ name: "snowflake_context", status: "failed", error: "Snowflake/n8n returned HTTP 503" }],
      warnings: [],
      outputs: {},
    }} />);

    expect(screen.getByText("Snowflake/n8n returned HTTP 503")).toBeInTheDocument();
  });
});
