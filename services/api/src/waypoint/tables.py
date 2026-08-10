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
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
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
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LlmCallRow(Base):
    """Durable paid-call lifecycle: pending → committed → reconciled | abandoned."""

    __tablename__ = "llm_calls"
    __table_args__ = (UniqueConstraint("call_key", name="uq_llm_calls_key"),)

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
    __tablename__ = "winners"
    __table_args__ = (UniqueConstraint("run_id", "pro_id", name="uq_winners_run_pro"),)

    id: Mapped[str] = mapped_column(primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    pro_id: Mapped[str]
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("candidates.id"), default=None)
    kind: Mapped[str]  # winner | no_action | abstained
    rationale: Mapped[str] = mapped_column(default="")
    evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)
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


class FleetControlRow(Base):
    __tablename__ = "fleet_control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    killed: Mapped[bool] = mapped_column(Boolean, default=False)
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
