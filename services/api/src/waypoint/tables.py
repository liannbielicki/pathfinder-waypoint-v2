"""SQLAlchemy durable model. Supabase/Postgres is the sole durable truth."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _new_id() -> str:
    return uuid4().hex


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012 — SQLAlchemy declarative config, not instance state
        dict[str, Any]: JSONB,
        list[str]: JSONB,
        list[Any]: JSONB,
        Decimal: Numeric(12, 4),
        str: Text,
        datetime: DateTime(timezone=True),
    }


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_id)
    status: Mapped[str] = mapped_column(default="queued")
    pro_ids: Mapped[list[str]]
    audience_query: Mapped[str]
    audience_run: Mapped[str]
    channels: Mapped[list[str]]
    journey_window: Mapped[str] = mapped_column(default="churn_risk")
    config_version: Mapped[str] = mapped_column(default="waypoint_v1")
    loop_config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    cost_limit: Mapped[Decimal] = mapped_column(default=Decimal(0))
    cost_reserved: Mapped[Decimal] = mapped_column(default=Decimal(0))
    cost_spent: Mapped[Decimal] = mapped_column(default=Decimal(0))
    stop_reason: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("run_id", "stage", "pro_id", name="uq_jobs_run_stage_pro"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    stage: Mapped[str]
    pro_id: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(default="queued")
    worker_id: Mapped[str | None] = mapped_column(default=None)
    lease_until: Mapped[datetime | None] = mapped_column(default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CandidateRow(Base):
    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    pro_id: Mapped[str]
    recommendation: Mapped[dict[str, Any]]
    critics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    persona_evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    score: Mapped[dict[str, Any]] = mapped_column(default=dict)
    cost_usd: Mapped[Decimal | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(default="generated")
    round: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EvolveRoundRow(Base):
    """Authoritative per-Pro round ledger; loop state is replayed from these rows."""

    __tablename__ = "evolve_rounds"
    __table_args__ = (
        UniqueConstraint("run_id", "pro_id", "round", name="uq_evolve_rounds_run_pro_round"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    pro_id: Mapped[str]
    round: Mapped[int] = mapped_column(Integer)
    mechanism: Mapped[str]
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("candidates.id"), default=None)
    outcome: Mapped[str]  # win | lose | suppressed | unavailable
    score_pp: Mapped[float | None] = mapped_column(Numeric(8, 4, asdecimal=False), default=None)
    # Batch-ranking evidence for the round decision: rank order, ranker scores,
    # tie margin/decision, finalists, and the selection reason.
    ranking: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LlmCallRow(Base):
    """Durable paid-call lifecycle: pending → committed → reconciled | abandoned."""

    __tablename__ = "llm_calls"
    __table_args__ = (
        UniqueConstraint("call_key", name="uq_llm_calls_key"),
        # backs abandon_stale / replay lookups, which filter (run_id, pro_id, status)
        Index("ix_llm_calls_run_pro_status", "run_id", "pro_id", "status"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_id)
    call_key: Mapped[str]
    run_id: Mapped[str]
    pro_id: Mapped[str | None] = mapped_column(default=None)
    stage: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending")
    model: Mapped[str]
    reserved_usd: Mapped[Decimal]
    actual_usd: Mapped[Decimal | None] = mapped_column(default=None)
    provider_request_id: Mapped[str | None] = mapped_column(default=None)
    usage_id: Mapped[str | None] = mapped_column(default=None)
    response_text: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class WinnerRow(Base):
    """A winner is warm-start eligible ONLY after outcomes.ingest observes a
    real 7-day return; nothing on the scoring path may set eligibility.
    fingerprint carries the sanitized band allowlist (warmstart.py) so
    cross-org retrieval never joins back into org-scoped rows."""

    __tablename__ = "winners"
    __table_args__ = (
        UniqueConstraint("run_id", "pro_id", name="uq_winners_run_pro"),
        Index(
            "ix_winners_warm_start",
            "fingerprint_version",
            text("created_at DESC"),
            postgresql_where=text("warm_start_eligible"),
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    pro_id: Mapped[str]
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("candidates.id"), default=None)
    kind: Mapped[str]  # winner | no_action | abstained
    rationale: Mapped[str] = mapped_column(default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    fingerprint: Mapped[dict[str, Any]] = mapped_column(default=dict)
    fingerprint_version: Mapped[str | None] = mapped_column(default=None)
    # V3 reusable learning identity. Nullable while historical winners are migrated.
    item_id: Mapped[str | None] = mapped_column(default=None)
    item_version: Mapped[str | None] = mapped_column(default=None)
    warm_start_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    warm_start_evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # None = pending | "validated" = observed 7d return | "validated_negative"
    validation_status: Mapped[str | None] = mapped_column(default=None)
    # Historical winners without a durable V3 item mapping remain audit-only.
    legacy_unresolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MeasurementRow(Base):
    __tablename__ = "measurements"
    __table_args__ = (
        CheckConstraint(
            "jsonb_array_length(indicators) BETWEEN 1 AND 2", name="ck_measurement_count"
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    winner_id: Mapped[str | None] = mapped_column(ForeignKey("winners.id"), default=None)
    indicators: Mapped[list[Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class HandoffRow(Base):
    __tablename__ = "handoffs"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_handoffs_key"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    winner_id: Mapped[str | None] = mapped_column(ForeignKey("winners.id"), default=None)
    idempotency_key: Mapped[str]
    payload: Mapped[dict[str, Any]]
    response: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(default="pending")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ExposureRow(Base):
    """Canonical exposure identity, including the neutral/control arm.

    A control exposure is not a WinnerRow and must still be measurable against
    the same product identity and observation windows as an A exposure.
    """

    __tablename__ = "exposures"

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_id)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), default=None)
    # The winner this exposure realizes or controls for (arm A or B). A
    # standalone neutral exposure carries none. This link is how a silent
    # control reaches its winner's causal comparison (outcomes.promote_winners).
    winner_id: Mapped[str | None] = mapped_column(ForeignKey("winners.id"), default=None)
    pro_id: Mapped[str]
    org_id: Mapped[str] = mapped_column(default="")
    item_id: Mapped[str | None] = mapped_column(default=None)
    item_version: Mapped[str | None] = mapped_column(default=None)
    arm: Mapped[str | None] = mapped_column(default=None)
    channel: Mapped[str] = mapped_column(default="")
    # Authoritative routing claim for every outcome attributed to this
    # exposure (see outcomes.REAL_SEND_ROUTING). "" = unclaimed, fails closed.
    routing: Mapped[str] = mapped_column(default="")
    send_status: Mapped[str] = mapped_column(default="unknown")
    sent_at: Mapped[datetime | None] = mapped_column(default=None)
    learning_version: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ItemRow(Base):
    """Canonical reusable theme item. The corpus is expandable and organic:
    resolution (items.py) creates items and versions from winners'
    recommendations — no fixed theme set exists anywhere. concept_hash backs
    the unique constraint so concurrent resolvers cannot split identity."""

    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("mechanism", "channel", "concept_hash", name="uq_items_identity"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_id)
    mechanism: Mapped[str]
    channel: Mapped[str] = mapped_column(default="")
    concept: Mapped[str]  # canonical pro-facing concept text (current version)
    concept_hash: Mapped[str]
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Organic, versioned metadata: prior concept versions and resolver notes.
    item_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(default="active")
    resolver_version: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class FleetControlRow(Base):
    __tablename__ = "fleet_control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    killed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Independent V3 learning-loop kill switch: stops checkpoint resolution
    # and outcome-driven learning without touching run processing (and vice
    # versa — `killed` never implies this one).
    learning_killed: Mapped[bool] = mapped_column(Boolean, default=False)
    loop_defaults: Mapped[dict[str, Any]] = mapped_column(default=dict)
    day: Mapped[str | None] = mapped_column(default=None)
    day_cost_limit: Mapped[Decimal] = mapped_column(default=Decimal(0))
    day_cost_reserved: Mapped[Decimal] = mapped_column(default=Decimal(0))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class UsageRow(Base):
    __tablename__ = "llm_usage"

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_id)
    run_id: Mapped[str]
    stage: Mapped[str]
    model: Mapped[str]
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TouchOutcomeRow(Base):
    """One observed outcome record per (recommendation, source). Horizon fields
    are tri-state: True/False are measured facts, None means not yet measurable.
    evidence_limitation labels records that cannot honestly claim attribution,
    and is DERIVED on every write from (winner resolved?, routing) — never
    left at whatever the first submission computed."""

    __tablename__ = "touch_outcomes"
    __table_args__ = (
        UniqueConstraint("recommendation_id", "source", name="uq_touch_outcomes_rec_source"),
        Index(
            "ix_touch_outcomes_window_channel_created_at",
            "journey_window",
            "channel",
            text("created_at DESC"),
            postgresql_where=text("evidence_limitation IS NULL"),
        ),
        Index(
            "ix_touch_outcomes_pro_id_evidence_limitation",
            "pro_id",
            "evidence_limitation",
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_id)
    recommendation_id: Mapped[str]  # Waypoint winner_id for this exposure
    item_id: Mapped[str | None] = mapped_column(default=None)
    item_version: Mapped[str | None] = mapped_column(default=None)
    arm: Mapped[str | None] = mapped_column(default=None)  # A, B, or unknown
    exposure_id: Mapped[str | None] = mapped_column(ForeignKey("exposures.id"), default=None)
    source: Mapped[str]  # e.g. "iterable_n8n", "manual"
    run_id: Mapped[str | None] = mapped_column(default=None)
    pro_id: Mapped[str] = mapped_column(default="")
    org_id: Mapped[str] = mapped_column(default="")
    journey_window: Mapped[str] = mapped_column(default="churn_risk")
    channel: Mapped[str] = mapped_column(default="")
    mechanism: Mapped[str] = mapped_column(default="")
    # How the message was routed, merged across submissions (outcomes.py).
    # "" = no source has claimed a routing yet, which fails closed.
    routing: Mapped[str] = mapped_column(default="")
    churn_risk_state: Mapped[str | None] = mapped_column(default=None)
    sent_at: Mapped[datetime | None] = mapped_column(default=None)
    send_status: Mapped[str] = mapped_column(default="unknown")
    send_confirmed_at: Mapped[datetime | None] = mapped_column(default=None)
    delivered: Mapped[bool | None] = mapped_column(Boolean, default=None)
    clicked: Mapped[bool | None] = mapped_column(Boolean, default=None)
    replied: Mapped[bool | None] = mapped_column(Boolean, default=None)
    unsubscribed: Mapped[bool | None] = mapped_column(Boolean, default=None)
    first_return_at: Mapped[datetime | None] = mapped_column(default=None)
    returned_1d: Mapped[bool | None] = mapped_column(Boolean, default=None)
    returned_7d: Mapped[bool | None] = mapped_column(Boolean, default=None)
    returned_14d: Mapped[bool | None] = mapped_column(Boolean, default=None)
    returned_30d: Mapped[bool | None] = mapped_column(Boolean, default=None)
    returned_90d: Mapped[bool | None] = mapped_column(Boolean, default=None)
    evidence_limitation: Mapped[str | None] = mapped_column(default=None)
    # Stamped by the checkpoint sweep when it resolves a horizon (audit trail
    # for which resolver version turned "unmeasured" into a measured negative).
    checkpoint_version: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class PersonaEvalRow(Base):
    """Cached persona reactions keyed by (prompt version, panel ids, concept,
    channel) hash — spec: reuse persona evaluation where the persona, journey
    state, and touch pattern are materially equivalent."""

    __tablename__ = "persona_evals"
    __table_args__ = (UniqueConstraint("cache_key", name="uq_persona_evals_key"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_id)
    cache_key: Mapped[str]
    reactions: Mapped[dict[str, Any]]  # persona_id -> reaction number
    snapshot_version: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
