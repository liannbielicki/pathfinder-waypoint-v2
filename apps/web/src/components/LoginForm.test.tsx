import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { LoginForm } from "./LoginForm";

describe("LoginForm", () => {
  it("has a labeled password field reachable by keyboard", () => {
    render(<LoginForm onSuccess={vi.fn()} />);
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign in/i })).toBeInTheDocument();
  });

  it("shows a visible error when login fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Invalid credentials", { status: 401 })));
    render(<LoginForm onSuccess={vi.fn()} />);
    await userEvent.type(screen.getByLabelText(/password/i), "wrong");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByRole("alert")).toBeVisible();
    vi.unstubAllGlobals();
  });

  it("shows the activity conflict returned by the shared backend", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ detail: "Context Workbench is running" }),
      { status: 409, headers: { "content-type": "application/json" } },
    )));
    render(<LoginForm onSuccess={vi.fn()} />);
    await userEvent.type(screen.getByLabelText(/password/i), "correct");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Context Workbench is running");
    vi.unstubAllGlobals();
  });
});
