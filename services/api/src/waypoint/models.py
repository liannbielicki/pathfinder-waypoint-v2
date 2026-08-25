"""Pydantic boundary models shared by API, workers, and tests."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, model_validator

RunStatus = Literal[
    "queued",
    "running",
    "waiting",
    "degraded",
    "failed",
    "resumed",
    "stopped",
    "complete",
    "abstained",
    "no_action",
]

# UI-visible terminal statuses; the finer taxonomy is carried in stop_reason.
TERMINAL_RUN_STATUSES = frozenset({"complete", "no_action", "abstained", "stopped", "failed"})

# Placeholder audience_query the UI sends at run creation; the pipeline
# replaces it with the version the n8n flow self-reports. Shared with the
# frontend (lib/api.ts PENDING_AUDIENCE_QUERY) — keep the literals in sync.
PENDING_AUDIENCE_QUERY = "pending_n8n"

# Closed set of high-leverage customer states (spec "Journey window"). Narrow
# on purpose; widening it is a product decision, not a code default.
# churn_risk_open is churn_risk's targeting without its gate: it optimizes for
# retention and minimizing churn risk just the same, but no brief may ever
# exclude a Pro from it. For audiences already filtered upstream, or where the
# churn signal is missing or untrusted. It shares churn_risk's evidence corpus
# (see evidence.evidence_windows) — same objective, same history.
JourneyWindow = Literal["churn_risk", "churn_risk_open", "onboarding", "upsell"]


class RunCreate(BaseModel):
    pro_ids: list[str] = Field(min_length=1)
    audience_query: str = Field(min_length=1)
    audience_run: str = Field(min_length=1)
    channels: list[str] = Field(min_length=1)
    # Confirmed loop-control overrides, UPPER_CASE spec keys (e.g. MAX_ROUNDS).
    # The confirm-typing gate is a UI contract: the UI only sends confirmed
    # fields, and the server treats any supplied key as confirmed.
    loop_config: dict[str, float] | None = None
    journey_window: JourneyWindow = "churn_risk"


class RunView(BaseModel):
    id: str
    status: str
    pro_ids: list[str]
    audience_query: str
    audience_run: str
    channels: list[str]
    config_version: str
    loop_config: dict[str, float]
    cost_limit_usd: Decimal
    cost_reserved_usd: Decimal
    cost_spent_usd: Decimal
    stop_reason: str | None
    created_at: datetime
    journey_window: str


class Recommendation(BaseModel):
    """Structured recommendation. Prose is composed in the view layer, never stored."""

    title: str = Field(min_length=1)
    mechanism: str = Field(min_length=1)
    actions: list[str] = Field(min_length=1)
    pro_facing_concept: str = Field(min_length=1)
    manager_rationale: str = Field(min_length=1)
    channel: Literal["sms", "email", "none"]
    risk: str = ""


class RankedCandidate(BaseModel):
    candidate_id: str = Field(min_length=1)
    rank: int = Field(ge=1)
    # 0-1 expected return-to-app value. NaN fails the bound checks, so an
    # invalid score is a validation error, never a silent comparison surprise.
    score: float = Field(ge=0.0, le=1.0)


class RankerDecision(BaseModel):
    """Strict ranker output contract: every candidate exactly once, contiguous
    unique ranks, bounded scores, and an EXPLICIT tie decision (the field is
    required — an omitted tie call is a malformed ranking, not an implied no)."""

    ranking: list[RankedCandidate] = Field(min_length=1)
    tie: bool
    tie_reason: str = ""

    def by_rank(self) -> list[RankedCandidate]:
        return sorted(self.ranking, key=lambda r: r.rank)


def validate_ranking(decision: RankerDecision, expected_ids: list[str]) -> RankerDecision:
    """Reject unknown ids, missing/duplicated candidates, and duplicate or
    gapped ranks. Raises ValueError so the caller re-asks under a fresh key."""
    ids = [r.candidate_id for r in decision.ranking]
    if sorted(ids) != sorted(expected_ids):
        raise ValueError(
            f"ranking must cover every candidate exactly once: expected {sorted(expected_ids)}, "
            f"got {sorted(ids)}"
        )
    ranks = sorted(r.rank for r in decision.ranking)
    if ranks != list(range(1, len(expected_ids) + 1)):
        raise ValueError(f"ranks must be unique 1..{len(expected_ids)}; got {ranks}")
    return decision


class MeasurementIndicator(BaseModel):
    key: str
    label: str
    direction: Literal["increase", "decrease"]
    source: str
    window_days: Literal[7, 14, 30, 90]
    rationale: str


class MeasurementPlan(BaseModel):
    indicators: list[MeasurementIndicator] = Field(min_length=1, max_length=2)


class TouchOutcomeIn(BaseModel):
    """One observed-outcome record from an outcome source (n8n Iterable/Amplitude
    flow, or manual backfill).

    TWO ways to name the touch, either is enough:

    * `recommendation_id` — the Waypoint winner_id, echoed back under either
      spelling (`recommendation_id` or `row_id`).
    * `run_id` + `pro_id` — the NATURAL key, and the one that needs nothing
      stamped into a message: `uq_winners_run_pro` makes one run plus one pro
      exactly one winner. Both halves already cross the boundary on their own
      (the LCM intake batch IS the run id, and the Iterable recipient IS the
      pro_uuid), so an outcome is attributable without Waypoint ids ever
      entering Iterable.

    `routing` is how a REAL send is told apart from a guardrailed test send that
    merely carries a real Pro's context. It must be `route-to-pro` for the
    record to count as evidence — see waypoint.outcomes.
    """

    recommendation_id: str = Field(
        default="",
        validation_alias=AliasChoices("recommendation_id", "row_id"),
    )
    source: str = Field(min_length=1)
    run_id: str = ""
    pro_id: str = ""
    org_id: str = ""
    channel: str = ""
    routing: str = ""
    sent_at: datetime | None = None
    delivered: bool | None = None
    clicked: bool | None = None
    replied: bool | None = None
    unsubscribed: bool | None = None
    returned_7d: bool | None = None
    returned_14d: bool | None = None
    returned_30d: bool | None = None
    returned_90d: bool | None = None

    @model_validator(mode="after")
    def _names_a_touch(self) -> TouchOutcomeIn:
        # Refuse at the boundary rather than storing a row keyed on "": every
        # such row would collide with every other on (recommendation_id, source).
        if self.recommendation_id:
            return self
        if not (self.run_id.strip() and self.pro_id.strip()):
            raise ValueError(
                "name the touch: recommendation_id/row_id, or both run_id and pro_id"
            )
        # The natural key is rendered into "unresolved:<run_id>:<pro_id>" when it
        # resolves to no winner, so a ":" inside either half would let two
        # different pairs render the same key and silently overwrite each other.
        # Real ids are uuid4().hex; reject anything that could alias.
        if ":" in self.run_id or ":" in self.pro_id:
            raise ValueError("run_id and pro_id must not contain ':'")
        return self


class FollowUpBranch(BaseModel):
    action: str = Field(min_length=1)  # "stop" or ONE concrete next touch (seed, not copy)
    channel: Literal["sms", "email", "none"] = "none"


class FollowUpPlan(BaseModel):
    """Bounded war game (spec "Multi-touch behavior"): four fixed branches, one
    conditional next touch each. Data only — nothing here is executed; only the
    approved next touch is ever sent, after the outcome is observed."""

    on_return: FollowUpBranch
    on_click_no_use: FollowUpBranch
    on_no_interaction: FollowUpBranch
    on_negative: FollowUpBranch


class HandoffReceipt(BaseModel):
    handoff_id: str
    idempotency_key: str
    status: Literal["accepted", "rejected", "duplicate"]
