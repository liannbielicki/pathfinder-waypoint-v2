import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AppNav } from "./AppNav";

let pathname = "/";
vi.mock("next/navigation", () => ({ usePathname: () => pathname }));

describe("AppNav", () => {
  beforeEach(() => { pathname = "/"; });

  it("keeps Waypoint first and marks it as the default active tab", () => {
    render(<AppNav />);

    const links = screen.getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual(["Waypoint", "Context Workbench"]);
    expect(links[0]).toHaveAttribute("href", "/");
    expect(links[0]).toHaveAttribute("aria-current", "page");
    expect(links[1]).toHaveAttribute("href", "/context-workbench");
    expect(links[1]).not.toHaveAttribute("aria-current");
  });

  it("marks the Workbench tab active on its route", () => {
    pathname = "/context-workbench";
    render(<AppNav />);

    expect(screen.getByRole("link", { name: "Context Workbench" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Waypoint" })).not.toHaveAttribute("aria-current");
  });
});
