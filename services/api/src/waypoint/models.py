"""Pydantic boundary models shared by API, workers, and tests."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field

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
    """One observed exposure/outcome record.

    LCM Personalization turns Waypoint's theme into an approved Housecall Pro
    SMS and sends it. It is not the measurement authority; outcome sources
    report exposure and events back to Waypoint.
    """

    recommendation_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("recommendation_id", "row_id"),
    )
    exposure_id: str | None = None
    source: str = Field(min_length=1)
    item_id: str | None = None
    item_version: str | None = None
    arm: Literal["A", "B"] | None = None
    pro_id: str = ""
    org_id: str = ""
    channel: str = ""
    sent_at: datetime | None = None
    send_status: Literal["unknown", "pending", "confirmed", "failed"] = "unknown"
    send_confirmed_at: datetime | None = None
    delivered: bool | None = None
    clicked: bool | None = None
    replied: bool | None = None
    unsubscribed: bool | None = None
    first_return_at: datetime | None = None
    returned_1d: bool | None = None
    returned_7d: bool | None = None
    returned_14d: bool | None = None
    returned_30d: bool | None = None
    returned_90d: bool | None = None


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
