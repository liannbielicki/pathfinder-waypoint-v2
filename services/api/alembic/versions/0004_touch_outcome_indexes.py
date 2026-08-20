# type: ignore
"""touch outcome indexes

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-14

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pattern_summaries sorts the whole table for its LIMIT; this partial
    # index (only attributable rows) covers the (journey_window, channel)
    # filter plus the created_at DESC ordering it sorts by.
    op.create_index(
        "ix_touch_outcomes_window_channel_created_at",
        "touch_outcomes",
        ["journey_window", "channel", sa.text("created_at DESC")],
        postgresql_where=sa.text("evidence_limitation IS NULL"),
    )
    # failed_mechanisms filters on (pro_id, evidence_limitation IS NULL).
    op.create_index(
        "ix_touch_outcomes_pro_id_evidence_limitation",
        "touch_outcomes",
        ["pro_id", "evidence_limitation"],
    )


def downgrade() -> None:
    op.drop_index("ix_touch_outcomes_pro_id_evidence_limitation", table_name="touch_outcomes")
    op.drop_index("ix_touch_outcomes_window_channel_created_at", table_name="touch_outcomes")
