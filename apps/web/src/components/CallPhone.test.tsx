import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CallPhone } from "./CallPhone";
import { getCallPhone } from "@/lib/api";

vi.mock("@/lib/api", () => ({ getCallPhone: vi.fn() }));

describe("CallPhone", () => {
  it("reveals the selected Pro's Snowflake phone on request", async () => {
    vi.mocked(getCallPhone).mockResolvedValue({ phone: "3035550123" });
    render(<CallPhone winnerId="win-1" />);
    await userEvent.click(screen.getByRole("button", { name: "Show phone number" }));
    expect(getCallPhone).toHaveBeenCalledWith("win-1");
    expect(await screen.findByRole("link", { name: "(303) 555-0123" })).toHaveAttribute("href", "tel:+13035550123");
  });
});
