import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, isUnauthorized } from "./api";
import { getWorkbenchJob, getWorkbenchStatus, resumeWorkbenchJob, startWorkbenchJob } from "./workbench";

describe("Workbench API errors", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("explains when the browser loses the backend connection", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    await expect(getWorkbenchStatus()).rejects.toThrow(
      "Lost connection to the shared Waypoint backend",
    );
  });

  it("reports a non-JSON backend failure by HTTP status", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: vi.fn().mockRejectedValue(new SyntaxError("not JSON")),
    }));

    await expect(getWorkbenchStatus()).rejects.toThrow(
      "Workbench backend returned HTTP 500 Internal Server Error",
    );
  });

  it("carries the HTTP status so a 5s poller can stop on 401", async () => {
    // Without the status, every poller retried an expired session forever and
    // buried the API log in 401s.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 401,
      statusText: "Unauthorized",
      json: vi.fn().mockResolvedValue({ detail: "Not authenticated" }),
    }));

    const cause = await getWorkbenchStatus().catch((error: unknown) => error);
    expect(isUnauthorized(cause)).toBe(true);
    expect((cause as ApiError).message).toBe("Not authenticated");
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

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/context-workbench/jobs", expect.objectContaining({ method: "POST", credentials: "include" }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/context-workbench/jobs/job-1", expect.objectContaining({ credentials: "include" }));
    expect(fetchMock).toHaveBeenNthCalledWith(3, "/api/context-workbench/jobs/job-1/resume", expect.objectContaining({ method: "POST", credentials: "include" }));
  });
});
