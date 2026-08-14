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
JourneyWindow = Literal["churn_risk", "onboarding", "upsell"]


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
    flow, or manual backfill). recommendation_id is the Waypoint winner_id carried
    through LCM -> Iterable as the intake row's row_id; outcome sources may echo
    either spelling back (TODOS: stable recommendation attribution)."""

    recommendation_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("recommendation_id", "row_id"),
    )
    source: str = Field(min_length=1)
    pro_id: str = ""
    org_id: str = ""
    channel: str = ""
    sent_at: datetime | None = None
    delivered: bool | None = None
    clicked: bool | None = None
    replied: bool | None = None
    unsubscribed: bool | None = None
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
