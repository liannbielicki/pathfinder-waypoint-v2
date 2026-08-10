"""Pydantic boundary models shared by API, workers, and tests."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

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


class RunCreate(BaseModel):
    pro_ids: list[str] = Field(min_length=1)
    audience_query: str = Field(min_length=1)
    audience_run: str = Field(min_length=1)
    channels: list[str] = Field(min_length=1)
    # Confirmed loop-control overrides, UPPER_CASE spec keys (e.g. MAX_ROUNDS).
    # The confirm-typing gate is a UI contract: the UI only sends confirmed
    # fields, and the server treats any supplied key as confirmed.
    loop_config: dict[str, float] | None = None


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
    window_days: Literal[7, 30, 90]
    rationale: str


class MeasurementPlan(BaseModel):
    indicators: list[MeasurementIndicator] = Field(min_length=1, max_length=2)


class HandoffReceipt(BaseModel):
    handoff_id: str
    idempotency_key: str
    status: Literal["accepted", "rejected"]
