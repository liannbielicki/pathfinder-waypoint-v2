import type { RunDetail, Winner } from "@/lib/api";

export const WINNER_FIXTURE: Winner = {
  id: "w1", pro_id: "pro_1", kind: "winner", candidate_id: "c1",
  rationale: "", evidence: {},
};

export const RUN_FIXTURE: RunDetail = {
  id: "run-1",
  status: "queued",
  pro_ids: ["pro_1"],
  audience_query: "audience_v7",
  audience_run: "2026-08-06T18:00:00Z",
  channels: ["sms"],
  config_version: "waypoint_v1",
  loop_config: {
    MAX_ROUNDS: 10, MAX_NO_IMPROVE: 3, PATIENCE: 1,
    KEEP_DELTA_PP: 0.5, WIN_THRESHOLD_PP: 15,
    CANDIDATE_COUNT: 3, TIE_MARGIN: 0.05, WARM_START_THRESHOLD: 0.75,
  },
  cost_limit_usd: "25.00",
  cost_reserved_usd: "0.40",
  cost_spent_usd: "0.12",
  stop_reason: null,
  created_at: "2026-08-06T18:00:00Z",
  journey_window: "churn_risk",
  stages: {},
  rounds: [],
  candidates: [],
  winners: [],
  measurements: [],
  handoffs: [],
  killed: false,
  agents_in_flight: 0,
};
