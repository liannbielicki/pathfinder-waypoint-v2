# type: ignore
"""compounding evolve loop: round ledger, recorded paid calls, loop config

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "loop_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "fleet_control",
        sa.Column(
            "loop_defaults",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("jobs", sa.Column("pro_id", sa.Text(), nullable=True))
    op.drop_constraint("uq_jobs_run_stage", "jobs", type_="unique")
    op.create_unique_constraint("uq_jobs_run_stage_pro", "jobs", ["run_id", "stage", "pro_id"])
    op.add_column("candidates", sa.Column("round", sa.Integer(), nullable=True))
    op.create_table(
        "evolve_rounds",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("pro_id", sa.Text(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("mechanism", sa.Text(), nullable=False),
        sa.Column("candidate_id", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("score_pp", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("best_score_after", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "pro_id", "round", name="uq_evolve_rounds_run_pro_round"),
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("call_key", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("pro_id", sa.Text(), nullable=True),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("actual_usd", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("provider_request_id", sa.Text(), nullable=True),
        sa.Column("usage_id", sa.Text(), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("call_key", name="uq_llm_calls_key"),
    )


def downgrade() -> None:
    op.drop_table("llm_calls")
    op.drop_table("evolve_rounds")
    op.drop_column("candidates", "round")
    op.drop_constraint("uq_jobs_run_stage_pro", "jobs", type_="unique")
    op.create_unique_constraint("uq_jobs_run_stage", "jobs", ["run_id", "stage"])
    op.drop_column("jobs", "pro_id")
    op.drop_column("fleet_control", "loop_defaults")
    op.drop_column("runs", "loop_config")
