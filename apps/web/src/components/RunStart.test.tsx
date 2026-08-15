import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RunStart } from "./RunStart";

const FLEET_SETTINGS = {
  loop_defaults: {
    MAX_ROUNDS: 10, MAX_NO_IMPROVE: 3, PATIENCE: 1,
    KEEP_DELTA_PP: 0.5, WIN_THRESHOLD_PP: 15,
    CANDIDATE_COUNT: 3, TIE_MARGIN: 0.05, WARM_START_THRESHOLD: 0.75,
  },
  max_in_flight_llm_calls: 4,
};

function stubFetch(overrides?: {
  settings?: object | Error;
  createRun?: (body: Record<string, unknown>) => Response | Promise<Response>;
}) {
  const createCalls: Record<string, unknown>[] = [];
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.includes("/fleet/settings")) {
      if (overrides?.settings instanceof Error) throw overrides.settings;
      return new Response(JSON.stringify(overrides?.settings ?? FLEET_SETTINGS));
    }
    if (path.includes("/runs")) {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      createCalls.push(body);
      if (overrides?.createRun) return overrides.createRun(body);
      return new Response(JSON.stringify({ id: "run-1", status: "queued" }), {
        status: 202,
      });
    }
    throw new Error(`unexpected fetch ${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, createCalls };
}

async function fillRequiredInputs() {
  await userEvent.type(screen.getByLabelText(/pro ids/i), "pro_1");
  // The run timestamp is autofilled; clear it to pin a deterministic value.
  await userEvent.clear(screen.getByLabelText(/audience run/i));
  await userEvent.type(
    screen.getByLabelText(/audience run/i), "2026-08-06T18:00:00Z",
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("RunStart", () => {
  it("autofills the audience run timestamp with now (UTC, editable)", () => {
    stubFetch();
    render(<RunStart onStarted={vi.fn()} />);
    const field = screen.getByLabelText(/audience run/i) as HTMLInputElement;
    expect(field.value).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/);
  });

  it("renders the three groups in order with the fleet cap visible and read-only", async () => {
    stubFetch();
    render(<RunStart onStarted={vi.fn()} />);
    const groups = screen.getAllByRole("group");
    const legends = groups.map((g) => g.querySelector("legend")?.textContent);
    expect(legends).toEqual(["Run inputs", "Loop behavior", "Fleet safety"]);
    const fleet = groups[2];
    expect(within(fleet).getByText(/max simultaneous model calls \(fleet\)/i))
      .toBeVisible();
    expect(within(fleet).getByText("4")).toBeVisible();
    expect(within(fleet).getByText(/MAX_IN_FLIGHT_LLM_CALLS/)).toBeVisible();
    expect(within(fleet).getByText(
      /shared across all workers; limits api pressure, not total run cost/i,
    )).toBeVisible();
    // Read-only: the fleet-safety group holds no editable control.
    expect(within(fleet).queryAllByRole("textbox")).toEqual([]);
    expect(within(fleet).queryAllByRole("spinbutton")).toEqual([]);
    await waitFor(() =>
      expect(screen.getByLabelText(/max rounds per pro/i)).toHaveValue(10),
    );
  });

  it("pre-fills the loop controls from persisted fleet defaults", async () => {
    stubFetch({ settings: { ...FLEET_SETTINGS, loop_defaults: {
      ...FLEET_SETTINGS.loop_defaults, MAX_ROUNDS: 7,
    } } });
    render(<RunStart onStarted={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByLabelText(/max rounds per pro/i)).toHaveValue(7),
    );
    expect(screen.getByLabelText(/refine attempts per mechanism/i)).toHaveValue(1);
  });

  it("every visible control keeps a persistent label", async () => {
    stubFetch();
    render(<RunStart onStarted={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByLabelText(/max rounds per pro/i)).toBeInTheDocument(),
    );
    for (const label of [
      /pro ids/i, /audience run/i, /channel/i,
      /max rounds per pro/i, /dry mechanisms before stopping/i,
      /refine attempts per mechanism/i, /min improvement to keep/i,
      /stop-early reduction/i, /ideas per round/i, /ranker tie margin/i,
    ]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    }
  });

  it("blocks submission when a changed field is not confirmed, keeping values", async () => {
    const { createCalls } = stubFetch();
    render(<RunStart onStarted={vi.fn()} />);
    await fillRequiredInputs();
    const maxRounds = await screen.findByLabelText(/max rounds per pro/i);
    await userEvent.clear(maxRounds);
    await userEvent.type(maxRounds, "5");
    await userEvent.click(screen.getByRole("button", { name: /start run/i }));
    const error = await screen.findByRole("alert");
    expect(error).toHaveTextContent(/confirm/i);
    expect(createCalls).toEqual([]);
    expect(maxRounds).toHaveValue(5);
    expect(screen.getByLabelText(/pro ids/i)).toHaveValue("pro_1");
    // Focus recovery: the confirmation input for the invalid field is focused.
    expect(document.activeElement).toBe(
      screen.getByLabelText(/type "confirm" to apply the new max rounds per pro/i),
    );
  });

  it("sends only confirmed changed fields in loop_config", async () => {
    const { createCalls } = stubFetch();
    const onStarted = vi.fn();
    render(<RunStart onStarted={onStarted} />);
    await fillRequiredInputs();
    const maxRounds = await screen.findByLabelText(/max rounds per pro/i);
    await userEvent.clear(maxRounds);
    await userEvent.type(maxRounds, "5");
    await userEvent.type(
      screen.getByLabelText(/type "confirm" to apply the new max rounds per pro/i),
      "confirm",
    );
    await userEvent.click(screen.getByRole("button", { name: /start run/i }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    expect(createCalls).toHaveLength(1);
    expect(createCalls[0].loop_config).toEqual({ MAX_ROUNDS: 5 });
  });

  it("sends the pending_n8n sentinel instead of an operator-typed query version", async () => {
    const { createCalls } = stubFetch();
    const onStarted = vi.fn();
    render(<RunStart onStarted={onStarted} />);
    await fillRequiredInputs();
    await screen.findByLabelText(/max rounds per pro/i);
    await userEvent.click(screen.getByRole("button", { name: /start run/i }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    expect(createCalls[0].audience_query).toBe("pending_n8n");
    expect(screen.queryByLabelText(/audience query/i)).not.toBeInTheDocument();
  });

  it("omits loop_config entirely when nothing changed", async () => {
    const { createCalls } = stubFetch();
    const onStarted = vi.fn();
    render(<RunStart onStarted={onStarted} />);
    await fillRequiredInputs();
    await screen.findByLabelText(/max rounds per pro/i);
    await userEvent.click(screen.getByRole("button", { name: /start run/i }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    expect(createCalls[0].loop_config).toBeUndefined();
  });

  it("identifies the field and bound on an invalid value", async () => {
    const { createCalls } = stubFetch();
    render(<RunStart onStarted={vi.fn()} />);
    await fillRequiredInputs();
    const patience = await screen.findByLabelText(/refine attempts per mechanism/i);
    await userEvent.clear(patience);
    await userEvent.type(patience, "0");
    await userEvent.type(
      screen.getByLabelText(/type "confirm" to apply the new refine attempts/i),
      "confirm",
    );
    await userEvent.click(screen.getByRole("button", { name: /start run/i }));
    const error = await screen.findByRole("alert");
    expect(error).toHaveTextContent(/refine attempts per mechanism/i);
    expect(error).toHaveTextContent(/at least 1/i);
    expect(createCalls).toEqual([]);
  });

  it("prevents duplicate submits while the request is in flight", async () => {
    let resolve: (response: Response) => void = () => {};
    const pending = new Promise<Response>((r) => { resolve = r; });
    const { createCalls } = stubFetch({ createRun: () => pending });
    render(<RunStart onStarted={vi.fn()} />);
    await fillRequiredInputs();
    await screen.findByLabelText(/max rounds per pro/i);
    const button = screen.getByRole("button", { name: /start run/i });
    await userEvent.click(button);
    expect(screen.getByRole("button", { name: /starting run/i })).toBeDisabled();
    expect(createCalls).toHaveLength(1);
    resolve(new Response(JSON.stringify({ id: "run-1" }), { status: 202 }));
  });

  it("keeps values and re-enables submit on a server error", async () => {
    stubFetch({
      createRun: () => new Response("audience rejected", { status: 422 }),
    });
    render(<RunStart onStarted={vi.fn()} />);
    await fillRequiredInputs();
    await screen.findByLabelText(/max rounds per pro/i);
    await userEvent.click(screen.getByRole("button", { name: /start run/i }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/audience rejected/);
    expect(screen.getByLabelText(/pro ids/i)).toHaveValue("pro_1");
    expect(screen.getByRole("button", { name: /start run/i })).toBeEnabled();
  });

  it("sends the selected journey window", async () => {
    const { createCalls } = stubFetch();
    const onStarted = vi.fn();
    render(<RunStart onStarted={onStarted} />);
    await fillRequiredInputs();
    await screen.findByLabelText(/max rounds per pro/i);
    await userEvent.selectOptions(
      screen.getByLabelText(/journey window/i), "onboarding",
    );
    await userEvent.click(screen.getByRole("button", { name: /start run/i }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    expect(createCalls[0]).toEqual(
      expect.objectContaining({ journey_window: "onboarding" }),
    );
  });

  it("disables loop controls and still submits when the settings fetch fails", async () => {
    const { createCalls } = stubFetch({ settings: new Error("network down") });
    const onStarted = vi.fn();
    render(<RunStart onStarted={onStarted} />);
    await screen.findByText(/defaults could not be loaded/i);
    expect(screen.getByLabelText(/max rounds per pro/i)).toBeDisabled();
    await fillRequiredInputs();
    await userEvent.click(screen.getByRole("button", { name: /start run/i }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    expect(createCalls[0].loop_config).toBeUndefined(); // server defaults apply
  });
});
