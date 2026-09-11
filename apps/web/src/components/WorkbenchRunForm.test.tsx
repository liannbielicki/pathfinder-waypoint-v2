import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WorkbenchRunForm } from "./WorkbenchRunForm";

describe("WorkbenchRunForm", () => {
  it("does not collect credential fields in the browser", () => {
    render(<WorkbenchRunForm onRun={vi.fn()} busy={false} />);
    expect(screen.queryByLabelText(/AI API key/i)).not.toBeInTheDocument();
  });
});
