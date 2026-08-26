import { expect, test, type Page } from "@playwright/test";

const RUN_BASE = {
  id: "run-e2e",
  status: "queued",
  pro_ids: ["pro_1"],
  audience_query: "audience_v7",
  audience_run: "2026-08-06T18:00:00Z",
  channels: ["sms"],
  config_version: "waypoint_v1",
  cost_limit_usd: "25.00",
  cost_reserved_usd: "0.00",
  cost_spent_usd: "0.00",
  stop_reason: null,
  created_at: "2026-08-06T18:00:00Z",
  stages: {},
  rounds: [],
  candidates: [],
  winners: [],
  measurements: [],
  handoffs: [],
  killed: false,
};

const WINNER_RUN = {
  ...RUN_BASE,
  status: "complete",
  stages: { context: {}, generate: {}, critics: {}, screen: {}, search: {}, final: {}, score: {}, measure: {}, ready: {} },
  candidates: [
    {
      id: "cand-1",
      pro_id: "pro_1",
      recommendation: {
        title: "Send open invoices reminder",
        mechanism: "invoice_delivery",
        pro_facing_concept: "Get paid faster.",
        manager_rationale: "Open AR is the signal.",
        channel: "sms",
      },
      critics: { block_kind: "none", reason: "grounded" },
      persona_evidence: {
        final: {
          panel: {
            items: [
              { persona_id: "solo_hustler", label: "Solo hustler", family: "solo_operators", role: "closest", fit_score: 1, rationale: "matches" },
            ],
          },
          reactions: [5.3],
        },
      },
      score: {
        final: { reduction_pp: 4.2, ci_lower_pp: 3.1, ci_upper_pp: 5.3, in_calibrated_range: true, calibration_version: "22cc4a1c89354327", baseline_confidence: "high", abstained: false },
      },
      status: "generated",
    },
  ],
  winners: [
    { id: "win-1", pro_id: "pro_1", kind: "winner", candidate_id: "cand-1", rationale: "Open AR is the signal.", evidence: { org_id: "org_1" } },
  ],
  measurements: [
    { id: "m-1", winner_id: "win-1", indicators: [{ key: "invoices_sent", label: "Invoices sent", direction: "increase", source: "billing", window_days: 30, rationale: "r" }] },
  ],
};

async function login(page: Page) {
  await page.route("**/api/auth/login", (route) =>
    route.fulfill({ json: { status: "ok" } }),
  );
  await page.goto("/");
  await page.getByLabel(/operator password/i).fill("operator-password");
  await page.getByRole("button", { name: /sign in/i }).press("Enter");
}

test("keyboard login failure is visible", async ({ page }) => {
  await page.route("**/api/auth/login", (route) =>
    route.fulfill({ status: 401, body: "Invalid credentials" }),
  );
  await page.goto("/");
  await page.getByLabel(/operator password/i).fill("wrong");
  await page.keyboard.press("Enter");
  await expect(page.locator("p[role=alert]")).toBeVisible();
});

test("async start returns immediately and shows queued state", async ({ page }) => {
  await login(page);
  await page.route("**/api/runs", (route) =>
    route.fulfill({ status: 202, json: { ...RUN_BASE, stages: undefined } }),
  );
  await page.route("**/api/runs/run-e2e", (route) => route.fulfill({ json: RUN_BASE }));
  await page.getByLabel(/pro ids/i).fill("pro_1");
  await page.getByLabel(/audience run/i).fill("2026-08-06T18:00:00Z");
  await page.getByRole("button", { name: /start run/i }).click();
  await expect(page.getByRole("status")).toHaveText(/queued/);
  await expect(page.getByText(/audience_v7/).first()).toBeVisible();
});

test("failed run shows explicit failure, never fake success", async ({ page }) => {
  await login(page);
  await page.route("**/api/runs/run-e2e", (route) =>
    route.fulfill({
      json: { ...RUN_BASE, status: "failed", stop_reason: "generate_failed: model error" },
    }),
  );
  await page.goto("/runs/run-e2e");
  await expect(page.getByRole("status")).toHaveText(/failed/);
  await expect(page.getByText(/generate_failed/)).toBeVisible();
  await expect(page.getByRole("button", { name: /create lcm handoff/i })).toBeDisabled();
});

test("kill stops the run from the UI", async ({ page }) => {
  await login(page);
  let killed = false;
  await page.route("**/api/runs/run-e2e", (route) =>
    route.fulfill({
      json: killed
        ? { ...RUN_BASE, status: "stopped", stop_reason: "operator_kill" }
        : { ...RUN_BASE, status: "running" },
    }),
  );
  await page.route("**/api/runs/run-e2e/kill", (route) => {
    killed = true;
    return route.fulfill({ json: { ...RUN_BASE, status: "stopped" } });
  });
  await page.goto("/runs/run-e2e");
  // Two-step kill: arm, type the confirmation word, then confirm.
  await page.getByRole("button", { name: /^kill run$/i }).click();
  await page.getByLabel(/type "kill" to confirm/i).fill("kill");
  await page.getByRole("button", { name: /confirm kill/i }).click();
  await expect(page.getByRole("status")).toHaveText(/stopped/);
  await expect(page.getByText(/operator_kill/)).toBeVisible();
});

test("no-action renders as a legitimate outcome", async ({ page }) => {
  await login(page);
  await page.route("**/api/runs/run-e2e", (route) =>
    route.fulfill({
      json: {
        ...RUN_BASE,
        status: "no_action",
        winners: [{ id: "w1", pro_id: "pro_1", kind: "no_action", candidate_id: null, rationale: "no_candidate_cleared_floor", evidence: {} }],
      },
    }),
  );
  await page.goto("/runs/run-e2e");
  await expect(page.getByRole("status")).toHaveText(/no action/);
  await expect(page.getByText(/legitimate outcome/i)).toBeVisible();
  await expect(page.getByRole("button", { name: /create lcm handoff/i })).toBeDisabled();
});

test("winner evidence and handoff receipt flow", async ({ page }) => {
  await login(page);
  let handedOff = false;
  await page.route("**/api/runs/run-e2e", (route) =>
    route.fulfill({
      json: handedOff
        ? {
            ...WINNER_RUN,
            handoffs: [{ id: "h1", idempotency_key: "run-e2e:win-1", status: "accepted", response: { lcm_id: "lcm-9" } }],
          }
        : WINNER_RUN,
    }),
  );
  await page.route("**/api/runs/run-e2e/handoff", (route) => {
    handedOff = true;
    return route.fulfill({
      json: { receipts: [{ handoff_id: "h1", idempotency_key: "run-e2e:win-1", status: "accepted" }] },
    });
  });
  await page.goto("/runs/run-e2e");
  await expect(page.getByText("Send open invoices reminder")).toBeVisible();
  await expect(page.getByText("Solo hustler")).toBeVisible();
  await expect(page.getByText(/4\.2 pp/)).toBeVisible();
  const handoffButton = page.getByRole("button", { name: /create lcm handoff/i });
  await expect(handoffButton).toBeEnabled();
  await handoffButton.click();
  await expect(page.getByText("run-e2e:win-1")).toBeVisible();
  await expect(page.getByText(/accepted/).first()).toBeVisible();
});

test("API outage shows a reconnecting state, not a stale screen", async ({ page }) => {
  await login(page);
  await page.route("**/api/runs/run-e2e", (route) => route.abort());
  await page.goto("/runs/run-e2e");
  await expect(page.locator("p[role=alert]")).toHaveText(/cannot reach the api/i);
});
