import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LoopProgress } from "./LoopProgress";
import type { EvolveRound, Winner } from "@/lib/api";
import { RUN_FIXTURE, WINNER_FIXTURE as WINNER } from "@/test/fixtures";

const round = (over: Partial<EvolveRound>): EvolveRound => ({
  pro_id: "pro_1", round: 1, mechanism: "discount",
  outcome: "lose", score_pp: null, ...over,
});

describe("LoopProgress", () => {
  it("renders nothing before any loop rounds exist", () => {
    const { container } = render(<LoopProgress run={RUN_FIXTURE} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("survives a payload with no rounds field (stale API deploy)", () => {
    const stale = { ...RUN_FIXTURE, rounds: undefined } as unknown as typeof RUN_FIXTURE;
    const { container } = render(<LoopProgress run={stale} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("labels an unknown decision kind with the raw kind, never a dangling separator", () => {
    render(
      <LoopProgress
        run={{
          ...RUN_FIXTURE,
          rounds: [round({ outcome: "win", score_pp: 1.0 })],
          winners: [{ ...WINNER, kind: "deferred" as Winner["kind"] }],
        }}
      />,
    );
    expect(screen.getByText(/· deferred/)).toBeInTheDocument();
  });

  it("summarizes pros still looping and the best reduction so far", () => {
    render(
      <LoopProgress
        run={{
          ...RUN_FIXTURE,
          pro_ids: ["pro_1", "pro_2", "pro_3"],
          rounds: [
            round({ pro_id: "pro_1", outcome: "win", score_pp: 1.2 }),
            round({ pro_id: "pro_2", outcome: "win", score_pp: 2.4 }),
          ],
          winners: [{ ...WINNER, pro_id: "pro_2" }],
        }}
      />,
    );
    // pro_2 is decided; pro_1 still looping; pro_3 has not started.
    expect(screen.getByText(/1 of 3 pros looping/i)).toBeInTheDocument();
    expect(screen.getByText(/best so far 2\.4 pp/i)).toBeInTheDocument();
  });

  it("shows one row per pro with loop count and current best from win rounds only", () => {
    render(
      <LoopProgress
        run={{
          ...RUN_FIXTURE,
          rounds: [
            round({ round: 1, outcome: "win", score_pp: 1.0 }),
            round({ round: 2, outcome: "lose", score_pp: 3.0 }),
            round({ round: 3, outcome: "win", score_pp: 1.2 }),
          ],
        }}
      />,
    );
    const row = screen.getByRole("group", { name: /pro_1/ });
    expect(within(row).getByText(/loop 3 of 10/i)).toBeInTheDocument();
    // A losing round's score never becomes "best" — only kept wins count.
    expect(within(row).getByText(/best 1\.2 pp/i)).toBeInTheDocument();
  });

  it("lists each round's score inside the pro row", () => {
    render(
      <LoopProgress
        run={{
          ...RUN_FIXTURE,
          rounds: [
            round({ round: 1, mechanism: "discount", outcome: "win", score_pp: 1.0 }),
            round({ round: 2, mechanism: "reminder", outcome: "suppressed" }),
          ],
        }}
      />,
    );
    const row = screen.getByRole("group", { name: /pro_1/ });
    expect(within(row).getByText(/discount/)).toBeInTheDocument();
    expect(within(row).getByText(/1\.0 pp · win/)).toBeInTheDocument();
    expect(within(row).getByText(/reminder/)).toBeInTheDocument();
    expect(within(row).getByText(/suppressed/)).toBeInTheDocument();
  });

  it("sorts looping pros above decided ones and marks the decision", () => {
    render(
      <LoopProgress
        run={{
          ...RUN_FIXTURE,
          pro_ids: ["pro_1", "pro_2"],
          rounds: [
            round({ pro_id: "pro_1", outcome: "win", score_pp: 5.0 }),
            round({ pro_id: "pro_2", outcome: "win", score_pp: 0.5 }),
          ],
          winners: [WINNER],
        }}
      />,
    );
    const rows = screen.getAllByRole("group", { name: /pro_/ });
    // pro_2 (still looping) outranks decided pro_1 despite the lower score.
    expect(rows[0]).toHaveAccessibleName(/pro_2/);
    expect(screen.getByText(/✓ winner/i)).toBeInTheDocument();
  });
});
