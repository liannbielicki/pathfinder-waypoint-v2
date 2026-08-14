# type: ignore
"""journey window, touch outcomes, persona eval cache

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "journey_window",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'churn_risk'"),
        ),
    )
    op.create_table(
        "touch_outcomes",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("recommendation_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("pro_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("org_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "journey_window", sa.Text(), nullable=False, server_default=sa.text("'churn_risk'")
        ),
        sa.Column("channel", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("mechanism", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("churn_risk_state", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered", sa.Boolean(), nullable=True),
        sa.Column("clicked", sa.Boolean(), nullable=True),
        sa.Column("replied", sa.Boolean(), nullable=True),
        sa.Column("unsubscribed", sa.Boolean(), nullable=True),
        sa.Column("returned_7d", sa.Boolean(), nullable=True),
        sa.Column("returned_14d", sa.Boolean(), nullable=True),
        sa.Column("returned_30d", sa.Boolean(), nullable=True),
        sa.Column("returned_90d", sa.Boolean(), nullable=True),
        sa.Column("evidence_limitation", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recommendation_id", "source", name="uq_touch_outcomes_rec_source"),
    )
    op.create_table(
        "persona_evals",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("cache_key", sa.Text(), nullable=False),
        sa.Column("reactions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("snapshot_version", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cache_key", name="uq_persona_evals_key"),
    )


def downgrade() -> None:
    op.drop_table("persona_evals")
    op.drop_table("touch_outcomes")
    op.drop_column("runs", "journey_window")
