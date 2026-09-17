import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { previewContextPromotion, promoteContext, startWorkbenchJob } from "@/lib/workbench";
import { AuthoringCatalog } from "./AuthoringCatalog";

vi.mock("@/lib/workbench", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/workbench")>()),
  startWorkbenchJob: vi.fn(),
  getWorkbenchJob: vi.fn(),
  promoteContext: vi.fn(),
  previewContextPromotion: vi.fn(),
}));

vi.mock("@/lib/catalogVersions", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/catalogVersions")>()),
  newCatalogVersionId: () => "context-test",
  saveCatalogVersion: vi.fn(),
}));

const trace = {
  stages: [{ name: "input", status: "succeeded", data: { identifier: "889901", source_mode: "snowflake" } }],
  warnings: [],
  outputs: {
    audit: {
      total_variables: 2,
      inventory: [
        { key: "JOBS_CREATED", observed_state: "present", observed_type: "number", source_query: "usage" },
        { key: "CALLS", observed_state: "null", observed_type: "null", source_query: "calls" },
      ],
    },
    authoring: {
      draft: [
        { key: "JOBS_CREATED", canonical_key: "jobs", value_category: "activity", related_features: ["jobs"], usefulness_rank: 5, disposition: "include", aggregate_prompt: null, confidence: 0.92, approval_status: "auto_approved" },
        { key: "CALLS", canonical_key: "calls", value_category: "communication", related_features: [], usefulness_rank: 2, disposition: "deprioritize", aggregate_prompt: null, confidence: 0.61, uncertainty_reason: "The variable name does not identify the call type.", approval_status: "review_required" },
      ],
      review_exception_count: 1,
      confidence_threshold: 0.8,
      total_keys: 2,
      completed_keys: 2,
      catalog_version: { prompt: "prompt" },
      feature_catalog_version_id: "features-one",
    },
  },
};

describe("AuthoringCatalog", () => {
  beforeEach(() => {
    vi.mocked(startWorkbenchJob).mockReset();
    vi.mocked(promoteContext).mockReset();
    vi.mocked(previewContextPromotion).mockReset();
    vi.mocked(previewContextPromotion).mockResolvedValue({
      id: "promotion-preview",
      included_variables: 1,
      counts: { approved: 1, pii_removed: 0, duplicates_merged: 0, retained: 1 },
      csv: "canonical_key,source_table,cohort_aggregate_prompt\n",
    });
    const values = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
      clear: () => values.clear(),
    });
  });

  it("replaces the compile action with a clear completed summary", async () => {
    vi.mocked(startWorkbenchJob).mockResolvedValue({
      id: "compile-1",
      status: "completed",
      state: {},
      result: {
        stages: [],
        warnings: ["canonical key jobs is used by multiple variables"],
        outputs: {
          compiled: {
            context: { r: [{ k: "JOBS_CREATED", c: "jobs" }] },
            metrics: { included_variables: 1, estimated_tokens: 11 },
          },
        },
      },
    });
    render(<AuthoringCatalog trace={trace} />);

    fireEvent.click(screen.getByRole("button", { name: /save new immutable version/i }));
    fireEvent.click(screen.getByRole("button", { name: /compile 1 approved variable/i }));

    expect(await screen.findByText(/baseline compiled successfully/i)).toBeInTheDocument();
    expect(screen.getByText(/1 approved selection.*1 compiled rule.*11 estimated tokens/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /compiled 1 rule/i })).toBeDisabled();
    expect(screen.getByText(/inspect compiled contract/i)).toBeInTheDocument();
    expect(screen.getByText(/"JOBS_CREATED"/i)).not.toBeVisible();
  });

  it("runs one simple curated-context comparison after compile", async () => {
    vi.mocked(startWorkbenchJob)
      .mockResolvedValueOnce({
        id: "compile-1", status: "completed", state: {},
        result: { stages: [], warnings: [], outputs: { compiled: { context: { r: [] }, metrics: { included_variables: 1, estimated_tokens: 11 } } } },
      })
      .mockResolvedValueOnce({
        id: "evaluation-1", status: "completed", state: {},
        result: {
          stages: [], warnings: [], outputs: { evaluation: {
            baseline: { context: { organization: {} }, prompt: "exact baseline prompt", candidates: [{ title: "Baseline idea", mechanism: "jobs", pro_facing_concept: "Baseline help" }], metrics: { input_tokens: 100, output_tokens: 50, duration_ms: 800 } },
            curated: { context: { v: { jobs: 12 } }, prompt: "exact curated prompt", candidates: [{ title: "Curated idea", mechanism: "jobs", pro_facing_concept: "Curated help" }], metrics: { input_tokens: 60, output_tokens: 45, duration_ms: 500 } },
            judge: { winner: "curated", reason: "More grounded recommendations.", suggested_changes: ["Keep the strongest signals first."] },
          } },
        },
      });
    render(<AuthoringCatalog trace={trace} />);
    fireEvent.click(screen.getByRole("button", { name: /save new immutable version/i }));
    fireEvent.click(screen.getByRole("button", { name: /compile 1 approved variable/i }));

    const evaluate = await screen.findByRole("button", { name: /test curated context/i });
    fireEvent.click(evaluate);

    expect(await screen.findByText(/curated performed better/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /baseline ideas/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /curated ideas/i })).toBeInTheDocument();
    expect(screen.getByText("Baseline idea")).toBeInTheDocument();
    expect(screen.getByText("Curated idea")).toBeInTheDocument();
    expect(screen.getByText(/100 input.*50 output.*800 ms/i)).toBeInTheDocument();
    expect(screen.getByText(/60 input.*45 output.*500 ms/i)).toBeInTheDocument();
    expect(screen.getByText(/keep the strongest signals first/i)).toBeInTheDocument();
    expect(screen.getByText(/inspect exact inputs and prompts/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /test complete/i })).toBeDisabled();
    expect(vi.mocked(startWorkbenchJob).mock.calls[1][0]).toMatchObject({ workbench_mode: "evaluate", context_policy: "compare" });
  });

  it("offers the exact CSV before activation after the context test", async () => {
    vi.mocked(startWorkbenchJob)
      .mockResolvedValueOnce({
        id: "compile-1", status: "completed", state: {},
        result: { stages: [], warnings: [], outputs: { compiled: { context: { r: [] }, metrics: { included_variables: 1 } } } },
      })
      .mockResolvedValueOnce({
        id: "evaluation-1", status: "completed", state: {},
        result: { stages: [], warnings: [], outputs: { evaluation: { judge: { winner: "curated", reason: "Better." } } } },
      });
    vi.mocked(promoteContext).mockResolvedValue({
      id: "promotion-one",
      included_variables: 1,
      counts: { approved: 1, pii_removed: 0, duplicates_merged: 0, retained: 1 },
      csv: "canonical_key,source_table,cohort_aggregate_prompt\njobs,ANALYTICS.JOBS,\n",
    });
    vi.mocked(previewContextPromotion).mockResolvedValue({
      id: "promotion-one",
      included_variables: 1,
      counts: { approved: 1, pii_removed: 0, duplicates_merged: 0, retained: 1 },
      csv: "canonical_key,source_table,cohort_aggregate_prompt\njobs,ANALYTICS.JOBS,\n",
    });
    render(<AuthoringCatalog trace={{
      ...trace,
      stages: [{
        name: "input", status: "succeeded", data: {
          identifier: "889901",
          source_mode: "snowflake",
          feature_catalog_entries: [{ feature: "jobs", "Value Statement": "Manage jobs." }],
          feature_catalog_version_id: "features-one",
        },
      }],
      outputs: {
        ...trace.outputs,
        audit: {
          ...trace.outputs.audit,
          inventory: [
            { ...trace.outputs.audit.inventory[0], source_table: "ANALYTICS.JOBS" },
            trace.outputs.audit.inventory[1],
          ],
        },
      },
    }} />);

    expect(screen.queryByRole("button", { name: /promote to waypoint/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /save new immutable version/i }));
    fireEvent.click(screen.getByRole("button", { name: /compile 1 approved variable/i }));
    fireEvent.click(await screen.findByRole("button", { name: /test curated context/i }));
    const download = await screen.findByRole("link", { name: /download snowflake handoff csv/i });
    expect(download).toHaveAttribute("download", "promotion-one-snowflake-handoff.csv");
    expect(vi.mocked(promoteContext)).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: /activate in waypoint/i }));

    expect(await screen.findByText(/promotion-one is now active/i)).toBeInTheDocument();
    expect(vi.mocked(previewContextPromotion)).toHaveBeenCalledWith({ evaluation_job_id: "evaluation-1" });
    expect(vi.mocked(promoteContext)).toHaveBeenCalledWith({ evaluation_job_id: "evaluation-1" });
  });

  it("invalidates compile, evaluation, and promotion when an entry is edited", async () => {
    vi.mocked(startWorkbenchJob)
      .mockResolvedValueOnce({
        id: "compile-1", status: "completed", state: {},
        result: { stages: [], warnings: [], outputs: { compiled: { context: { r: [] }, metrics: { included_variables: 1 } } } },
      })
      .mockResolvedValueOnce({
        id: "evaluation-1", status: "completed", state: {},
        result: { stages: [], warnings: [], outputs: { evaluation: { judge: { winner: "curated" } } } },
      });
    render(<AuthoringCatalog trace={trace} />);
    fireEvent.click(screen.getByRole("button", { name: /save new immutable version/i }));
    fireEvent.click(screen.getByRole("button", { name: /compile 1 approved variable/i }));
    fireEvent.click(await screen.findByRole("button", { name: /test curated context/i }));
    expect(await screen.findByRole("button", { name: /activate in waypoint/i })).toBeInTheDocument();

    fireEvent.click(screen.getByText("JOBS_CREATED"));
    fireEvent.change(screen.getByLabelText("Canonical key"), { target: { value: "jobs_changed" } });

    expect(screen.queryByRole("button", { name: /activate in waypoint/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save new immutable version/i })).toBeEnabled();
  });

  it("shows every variable through disposition filters", () => {
    const { container } = render(<AuthoringCatalog trace={trace} />);
    expect(screen.getByLabelText(/search variables/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /include 1/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /deprioritize 1/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /exclude 0/i })).toBeInTheDocument();
    expect(screen.getByText("JOBS_CREATED")).toBeInTheDocument();
    expect(container.querySelector(".catalog-cards")).not.toHaveTextContent("CALLS");

    fireEvent.click(screen.getByRole("button", { name: /deprioritize 1/i }));
    expect(screen.getByText("CALLS")).toBeInTheDocument();
    expect(screen.getByText(/61% confidence/i)).toBeInTheDocument();
    expect(screen.getByText(/does not identify the call type/i)).toBeInTheDocument();
    expect(container.querySelector("table")).toBeNull();
  });

  it("promotes an approved deprioritized variable into include", () => {
    render(<AuthoringCatalog trace={trace} />);
    const save = screen.getByRole("button", { name: /save new immutable version/i });
    const compile = screen.getByRole("button", { name: /compile 1 approved variable/i });
    expect(save).toBeEnabled();
    expect(compile).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /deprioritize 1/i }));
    fireEvent.click(screen.getByRole("button", { name: /approve deprioritize calls/i }));
    expect(screen.getByText(/no deprioritized variables parked/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /include 2/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /deprioritize 0/i })).toBeInTheDocument();
    expect(screen.queryByText("CALLS")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve deprioritize calls/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /include 2/i }));
    expect(screen.getByText("CALLS")).toBeInTheDocument();
    expect(screen.getAllByText("auto approved")).toHaveLength(2);
    expect(save).toBeEnabled();
    fireEvent.click(save);
    expect(compile).toBeEnabled();
  });

  it("saves parked deprioritized variables as an edited version", () => {
    render(<AuthoringCatalog trace={trace} />);

    const save = screen.getByRole("button", { name: /save new immutable version/i });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    expect(screen.getByText("Edited")).toBeInTheDocument();
    expect(screen.getByText(/next step.*compile 1 approved variable/i)).toBeInTheDocument();
    const compile = screen.getByRole("button", { name: /compile 1 approved variable/i });
    expect(compile).toBeEnabled();
    expect(compile).not.toHaveClass("secondary");
    expect(save).toBeDisabled();
  });

  it("shows every deprioritized variable without a 25-card cap", () => {
    const many = Array.from({ length: 30 }, (_, index) => ({
      key: `UNCERTAIN_${index}`,
      canonical_key: `uncertain_${index}`,
      usefulness_rank: 4,
      disposition: "deprioritize" as const,
      confidence: 0.5,
      uncertainty_reason: "Needs review.",
      approval_status: "review_required",
    }));
    const manyTrace = {
      ...trace,
      outputs: {
        ...trace.outputs,
        authoring: { ...trace.outputs.authoring, draft: many, review_exception_count: 30 },
      },
    };

    render(<AuthoringCatalog trace={manyTrace} />);

    fireEvent.click(screen.getByRole("button", { name: /deprioritize 30/i }));
    expect(screen.getAllByRole("button", { name: /^approve deprioritize uncertain_/i })).toHaveLength(30);
  });

  it("renders and edits repeated source keys independently without React key warnings", () => {
    const duplicateTrace = {
      ...trace,
      outputs: {
        ...trace.outputs,
        audit: {
          total_variables: 2,
          inventory: [
            { key: "SEGMENT", observed_state: "present", observed_type: "string", source_query: "first" },
            { key: "SEGMENT", observed_state: "present", observed_type: "string", source_query: "second" },
          ],
        },
        authoring: {
          ...trace.outputs.authoring,
          draft: [
            { key: "SEGMENT", canonical_key: "segment_first", disposition: "include", confidence: 0.5, approval_status: "auto_approved" },
            { key: "SEGMENT", canonical_key: "segment_second", disposition: "include", confidence: 0.5, approval_status: "auto_approved" },
          ],
        },
      },
    };
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<AuthoringCatalog trace={duplicateTrace} />);

    fireEvent.change(screen.getByDisplayValue("segment_first"), { target: { value: "changed" } });
    expect(screen.getByDisplayValue("segment_second")).toBeInTheDocument();
    expect(consoleError.mock.calls.flat().join(" ")).not.toContain("same key");
    consoleError.mockRestore();
  });

  it("restores a durable compiled result after reload", () => {
    const compiledTrace = {
      stages: [{ name: "input", status: "succeeded", data: { identifier: "889901" } }],
      warnings: [],
      outputs: {
        compiled: {
          context: { v: { jobs: 12 }, pc: { jobs: { a: "Jobs" } } },
          metrics: { estimated_tokens: 11 },
        },
      },
    };

    render(<AuthoringCatalog trace={compiledTrace} />);

    expect(screen.getByRole("heading", { name: /approved context-packet baseline/i })).toBeInTheDocument();
    expect(screen.getByText(/"jobs": 12/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /save new immutable version/i })).not.toBeInTheDocument();
  });

  it("does not present an older compiled packet beside a restored catalog", () => {
    render(<AuthoringCatalog trace={{
      ...trace,
      outputs: {
        ...trace.outputs,
        compiled: { context: { v: { old_value: 1 } }, metrics: { included_variables: 1 } },
      },
    }} />);

    expect(screen.queryByRole("heading", { name: /approved context-packet baseline/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/old_value/i)).not.toBeInTheDocument();
  });

  it("restores the reviewed immutable version linked to an authoring job", () => {
    const reviewed = [{
      key: "CALLS",
      canonical_key: "calls",
      value_category: "communication",
      related_features: [],
      usefulness_rank: 4,
      disposition: "deprioritize",
      aggregate_prompt: null,
      confidence: 0.61,
      approval_status: "human_approved",
      review_status: "reviewed",
    }];
    localStorage.setItem("waypoint-context-catalog-versions", JSON.stringify([{
      id: "context-reviewed",
      name: "Reviewed",
      created_at: "2026-09-16T00:00:00Z",
      prompt: "prompt",
      entries: reviewed,
    }]));
    localStorage.setItem("waypoint-context-workbench-job-version:authoring-1", "context-reviewed");

    render(<AuthoringCatalog trace={{ ...trace, job_id: "authoring-1" }} />);

    expect(screen.getByText(/no deprioritized variables parked/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /include 1/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /deprioritize 0/i })).toBeInTheDocument();
    expect(screen.getByText("CALLS")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /compile 1 approved variable/i })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /approve calls/i })).not.toBeInTheDocument();
  });
});
