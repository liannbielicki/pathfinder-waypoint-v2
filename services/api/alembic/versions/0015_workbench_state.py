# type: ignore
"""Persist sanitized Context Workbench jobs and immutable promotions."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workbench_jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("request", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("state", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'needs_review', 'completed', 'failed')",
            name="ck_workbench_jobs_status",
        ),
    )
    op.create_index(
        "uq_workbench_jobs_one_active",
        "workbench_jobs",
        [sa.text("(1)")],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "context_promotions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("bundle", postgresql.JSONB(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "uq_context_promotions_one_active",
        "context_promotions",
        [sa.text("(1)")],
        unique=True,
        postgresql_where=sa.text("active"),
    )


def downgrade() -> None:
    op.drop_index("uq_context_promotions_one_active", table_name="context_promotions")
    op.drop_table("context_promotions")
    op.drop_index("uq_workbench_jobs_one_active", table_name="workbench_jobs")
    op.drop_table("workbench_jobs")
