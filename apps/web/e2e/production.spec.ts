import { expect, test, type Page } from "@playwright/test";

const CANDIDATE = (pro: string) => ({
  id: `cand-${pro}`,
  pro_id: pro,
  recommendation: {
    title: `Invoice reminder for ${pro}`,
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
          { persona_id: "growing_crew_lead", label: "Growing crew lead", family: "growth_teams", role: "counterweight", fit_score: 0.7, rationale: "matches" },
        ],
      },
      reactions: [5.4, 5.1],
    },
  },
  score: {
    final: { reduction_pp: 3.8, ci_lower_pp: 2.7, ci_upper_pp: 4.9, in_calibrated_range: true, calibration_version: "22cc4a1c89354327", baseline_confidence: "high", abstained: false },
  },
  status: "generated",
});

const MIXED_RUN = {
  id: "run-prod",
  status: "complete",
  pro_ids: ["pro_a", "pro_b", "pro_c"],
  audience_query: "audience_v7",
  audience_run: "2026-08-06T18:00:00Z",
  channels: ["sms"],
  config_version: "waypoint_v1",
  cost_limit_usd: "25.00",
  cost_reserved_usd: "2.10",
  cost_spent_usd: "1.84",
  stop_reason: null,
  created_at: "2026-08-06T18:00:00Z",
  stages: { context: {}, generate: {}, critics: {}, screen: {}, search: {}, final: {}, score: {}, measure: {}, ready: {} },
  rounds: [],
  candidates: [CANDIDATE("pro_a")],
  winners: [
    { id: "win-a", pro_id: "pro_a", kind: "winner", candidate_id: "cand-pro_a", rationale: "Open AR is the signal.", evidence: { org_id: "org_a" } },
    { id: "win-b", pro_id: "pro_b", kind: "no_action", candidate_id: null, rationale: "no_candidate_cleared_floor", evidence: {} },
    { id: "win-c", pro_id: "pro_c", kind: "abstained", candidate_id: null, rationale: "low panel fit: panel of 3 needs more qualifying matches; only 2 available", evidence: {} },
  ],
  measurements: [
    { id: "m-a", winner_id: "win-a", indicators: [{ key: "invoices_sent", label: "Invoices sent", direction: "increase", source: "billing", window_days: 30, rationale: "r" }] },
  ],
  handoffs: [],
  killed: false,
};

async function login(page: Page) {
  await page.route("**/api/auth/login", (route) => route.fulfill({ json: { status: "ok" } }));
  await page.goto("/");
  await page.getByLabel(/operator password/i).fill("operator-password");
  await page.getByRole("button", { name: /sign in/i }).click();
}

test("mixed production run keeps every outcome distinct and evidence attached", async ({ page }) => {
  await login(page);
  await page.route("**/api/runs/run-prod", (route) => route.fulfill({ json: MIXED_RUN }));
  await page.goto("/runs/run-prod");

  // All three outcome kinds are visible and distinct.
  await expect(page.getByText("Invoice reminder for pro_a")).toBeVisible();
  await expect(page.getByText(/no action for pro_b/i)).toBeVisible();
  await expect(page.getByText(/abstained for pro_c/i)).toBeVisible();
  // The abstain rationale renders twice (outcome list item + detail); assert
  // the reason is shown without tripping strict-mode on the duplicate match.
  await expect(page.getByText(/low panel fit/).first()).toBeVisible();

  // Evidence stays attached: panel roles, reactions, calibration provenance.
  await expect(page.getByText("Growing crew lead")).toBeVisible();
  await expect(page.getByText(/counterweight/)).toBeVisible();
  await expect(page.getByText(/22cc4a1c89354327/)).toBeVisible();

  // Cost and lineage are shown; handoff is available for the ready winner.
  await expect(page.getByText(/\$1\.84/)).toBeVisible();
  await expect(page.getByText(/audience_v7/).first()).toBeVisible();
  await expect(page.getByRole("button", { name: /create lcm handoff/i })).toBeEnabled();
});

test("degraded and waiting states are explicit, with kill available", async ({ page }) => {
  await login(page);
  for (const status of ["waiting", "degraded", "resumed"]) {
    await page.route("**/api/runs/run-prod", (route) =>
      route.fulfill({ json: { ...MIXED_RUN, status, winners: [], measurements: [], candidates: [] } }),
    );
    await page.goto("/runs/run-prod");
    await expect(page.getByRole("status")).toHaveText(new RegExp(status));
    await expect(page.getByRole("button", { name: /kill run/i })).toBeEnabled();
  }
});
