import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { WorkbenchRunForm } from "./WorkbenchRunForm";

describe("WorkbenchRunForm", () => {
  beforeEach(() => {
    const values = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
      clear: () => values.clear(),
    });
    const shared = { context: [] as Record<string, unknown>[], feature: [] as Record<string, unknown>[] };
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (url: string, init?: RequestInit) => ({
      ok: true,
      json: async () => {
        if (url.includes("/catalogs?kind=context")) return shared.context;
        if (url.includes("/catalogs?kind=feature")) return shared.feature;
        if (url.endsWith("/catalogs") && init?.method === "POST") {
          const version = JSON.parse(String(init.body));
          const saved = { ...version, created_at: "2026-09-18T16:04:05Z" };
          const versions = shared[version.kind as "context" | "feature"];
          if (!versions.some((item) => item.id === version.id)) versions.unshift(saved);
          return saved;
        }
        return {
          env_file: "/project/services/api/.env",
          env_file_exists: true,
          configured: { snowflake: true, context_layer: true, ai: true, model: true },
          activity: "idle",
        };
      },
    })));
  });

  it("does not collect credential fields in the browser", () => {
    render(<WorkbenchRunForm onRun={vi.fn()} busy={false} />);
    expect(screen.queryByLabelText(/AI API key/i)).not.toBeInTheDocument();
  });

  it("starts with the experimental Snowflake audit and keeps Context Layer optional", async () => {
    render(<WorkbenchRunForm onRun={vi.fn()} busy={false} />);

    expect(screen.getByRole("heading", { name: /build waypoint context/i })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /add context layer/i })).not.toBeChecked();
    expect(screen.getByRole("button", { name: /collect and curate all variables/i })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("/project/services/api/.env")).toBeInTheDocument());
    expect(screen.getByText(/snowflake.*configured/i)).toBeInTheDocument();
  });

  it("locks collection while a Waypoint run is active", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        env_file: "/project/services/api/.env",
        env_file_exists: true,
        configured: { snowflake: true, context_layer: true, ai: true, model: true },
        activity: "waypoint",
      }),
    }));

    render(<WorkbenchRunForm onRun={vi.fn()} busy={false} />);

    expect(await screen.findByText(/waypoint run is active/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /collect and curate/i })).toBeDisabled();
  });

  it("removes collection as the primary action after a run is complete", () => {
    render(<WorkbenchRunForm onRun={vi.fn()} busy={false} completed />);

    expect(screen.queryByRole("button", { name: /collect and curate all variables/i })).not.toBeInTheDocument();
    expect(screen.getByText(/collection is complete.*continue to curation/i)).toBeInTheDocument();
  });

  it("starts a fresh run without deleting saved catalog versions", () => {
    const savedVersions = JSON.stringify([{ id: "saved-1", entries: [] }]);
    window.localStorage.setItem("waypoint-context-workbench-active-job", "job-1");
    window.localStorage.setItem("waypoint-context-catalog-versions", savedVersions);
    const onStartFresh = vi.fn();

    render(<WorkbenchRunForm onRun={vi.fn()} busy={false} completed onStartFresh={onStartFresh} />);
    fireEvent.click(screen.getByRole("button", { name: /start a fresh run/i }));

    expect(onStartFresh).toHaveBeenCalledOnce();
    expect(window.localStorage.getItem("waypoint-context-workbench-active-job")).toBeNull();
    expect(window.localStorage.getItem("waypoint-context-catalog-versions")).toBe(savedVersions);
  });

  it("offers versioned feature catalog upload without exposing query editing", () => {
    render(<WorkbenchRunForm onRun={vi.fn()} busy={false} />);
    expect(screen.getByLabelText(/upload feature catalog csv/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/snowflake query/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/context policy/i)).not.toBeInTheDocument();
  });

  it("reconnects to the saved job after remount without starting another run", async () => {
    window.localStorage.setItem("waypoint-context-workbench-active-job", "job-1");
    const trace = { stages: [], warnings: [], outputs: { authoring: { draft: [] } } };
    const fetchMock = vi.fn().mockImplementation(async (url: string) => ({
      ok: true,
      json: async () => url.endsWith("/jobs/job-1")
        ? { id: "job-1", status: "completed", state: {}, result: trace }
        : {
            env_file: "/project/services/api/.env",
            env_file_exists: true,
            configured: { snowflake: true, context_layer: true, ai: true, model: true },
          },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const onRun = vi.fn();

    render(<WorkbenchRunForm onRun={onRun} busy={false} />);

    await waitFor(() => expect(onRun).toHaveBeenCalledWith({ ...trace, job_id: "job-1" }));
    expect(fetchMock).not.toHaveBeenCalledWith(
      expect.stringMatching(/\/jobs$/),
      expect.objectContaining({ method: "POST" }),
    );
    expect(screen.getByText(/job completed/i)).toBeInTheDocument();
  });

  it("loads a selected saved catalog into the completed authoring view", async () => {
    const savedEntries = [{
      key: "JOBS_CREATED",
      canonical_key: "jobs_created",
      disposition: "include",
      review_status: "reviewed",
    }];
    window.localStorage.setItem("waypoint-context-catalog-versions", JSON.stringify([{
      id: "context-480",
      name: "Yesterday 480",
      created_at: "2026-09-15T18:00:00Z",
      prompt: "saved prompt",
      entries: savedEntries,
      feature_catalog_version_id: "features-one",
      confidence_threshold: 0.8,
    }]));
    const onRun = vi.fn();
    render(<WorkbenchRunForm onRun={onRun} busy={false} />);

    await waitFor(() => expect(screen.getByRole("option", { name: /yesterday 480/i })).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText(/start from prior context catalog/i), { target: { value: "context-480" } });

    expect(onRun).toHaveBeenCalledWith(expect.objectContaining({
      outputs: expect.objectContaining({
        authoring: expect.objectContaining({
          draft: [{ ...savedEntries[0], approval_status: "auto_approved" }],
          completed_keys: 1,
          remaining_keys: 0,
        }),
      }),
    }));
    expect(fetch).not.toHaveBeenCalledWith(
      expect.stringMatching(/\/jobs$/),
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("loads shared Postgres catalog versions without browser-local data", async () => {
    const shared = {
      id: "context-shared",
      kind: "context",
      name: "Shared context",
      created_at: "2026-09-18T16:04:05Z",
      entries: [{
        key: "JOBS_CREATED",
        canonical_key: "jobs_created",
        disposition: "include",
      }],
      details: { prompt: "shared prompt", confidence_threshold: 0.9 },
    };
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (url: string) => ({
      ok: true,
      json: async () => url.includes("/catalogs?kind=context")
        ? [shared]
        : url.includes("/catalogs?kind=feature")
          ? []
          : {
              env_file: "Railway service environment",
              env_file_exists: false,
              configured: { snowflake: true, context_layer: true, ai: true, model: true },
              activity: "idle",
            },
    })));
    const onRun = vi.fn();

    render(<WorkbenchRunForm onRun={onRun} busy={false} />);

    const option = await screen.findByRole("option", { name: /shared context/i });
    fireEvent.change(screen.getByLabelText(/start from prior context catalog/i), {
      target: { value: "context-shared" },
    });
    expect(option).toBeVisible();
    expect(onRun).toHaveBeenCalledWith(expect.objectContaining({
      outputs: expect.objectContaining({
        authoring: expect.objectContaining({
          draft: [expect.objectContaining({ key: "JOBS_CREATED" })],
        }),
      }),
    }));
  });

  it("restores every legacy draft using its predetermined disposition", async () => {
    const legacyEntries = Array.from({ length: 30 }, (_, index) => ({
      key: `LEGACY_${index}`,
      canonical_key: `legacy_${index}`,
      disposition: index < 10 ? "include" : index < 20 ? "deprioritize" : "exclude",
      usefulness_rank: 5,
      review_status: "draft",
    }));
    window.localStorage.setItem("waypoint-context-catalog-versions", JSON.stringify([{
      id: "legacy-30",
      name: "Legacy run",
      created_at: "2026-09-15T18:00:00Z",
      prompt: "saved prompt",
      entries: legacyEntries,
    }]));
    const onRun = vi.fn();
    render(<WorkbenchRunForm onRun={onRun} busy={false} />);

    await waitFor(() => expect(screen.getByRole("option", { name: /legacy run/i })).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText(/start from prior context catalog/i), { target: { value: "legacy-30" } });

    const restored = onRun.mock.calls[0][0].outputs.authoring.draft;
    expect(restored).toHaveLength(30);
    expect(restored.filter((entry: { approval_status: string }) => entry.approval_status === "auto_approved")).toHaveLength(10);
    expect(restored.filter((entry: { approval_status: string }) => entry.approval_status === "review_required")).toHaveLength(10);
    expect(restored.filter((entry: { approval_status: string }) => entry.approval_status === "excluded")).toHaveLength(10);
  });

  it("selects a newly saved edited catalog for the next collection pass", async () => {
    const onRun = vi.fn();
    render(<WorkbenchRunForm onRun={onRun} busy={false} />);
    const edited = {
      id: "context-edited",
      name: "Context catalog 9/16/2026",
      tag: "Edited",
      created_at: "2026-09-16T17:52:00Z",
      prompt: "saved prompt",
      entries: [{ key: "JOBS_CREATED", disposition: "include" }],
    };

    window.localStorage.setItem("waypoint-context-catalog-versions", JSON.stringify([edited]));
    window.localStorage.setItem("waypoint-context-catalog-selected", edited.id);
    window.dispatchEvent(new Event("waypoint-catalog-updated"));

    await waitFor(() => expect(screen.getByLabelText(/start from prior context catalog/i)).toHaveValue(edited.id));
    expect(screen.getByRole("option", { name: /edited.*context catalog 9\/16\/2026/i })).toBeInTheDocument();
  });
});
