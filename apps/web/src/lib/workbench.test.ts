import { afterEach, describe, expect, it, vi } from "vitest";
import { getWorkbenchJob, resumeWorkbenchJob, runWorkbench, startWorkbenchJob } from "./workbench";

describe("Workbench API errors", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("explains when the browser loses the backend connection", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    await expect(runWorkbench({})).rejects.toThrow(
      "Lost connection to the Workbench backend at http://localhost:8766",
    );
  });

  it("reports a non-JSON backend failure by HTTP status", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: vi.fn().mockRejectedValue(new SyntaxError("not JSON")),
    }));

    await expect(runWorkbench({})).rejects.toThrow(
      "Workbench backend returned HTTP 500 Internal Server Error",
    );
  });
});

describe("durable Workbench jobs", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("starts, reads, and resumes the same job id", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: "job-1", status: "running", state: {}, result: null }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await startWorkbenchJob({ identifier: "org-1" });
    await getWorkbenchJob("job-1");
    await resumeWorkbenchJob("job-1");

    expect(fetchMock).toHaveBeenNthCalledWith(1, "http://localhost:8766/api/context-workbench/jobs", expect.objectContaining({ method: "POST" }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "http://localhost:8766/api/context-workbench/jobs/job-1", undefined);
    expect(fetchMock).toHaveBeenNthCalledWith(3, "http://localhost:8766/api/context-workbench/jobs/job-1/resume", expect.objectContaining({ method: "POST" }));
  });
});
