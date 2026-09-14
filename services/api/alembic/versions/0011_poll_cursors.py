# type: ignore
"""Durable cursors for the direct Iterable/Amplitude outcome pollers."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "poll_cursors",
        sa.Column("source", sa.Text(), primary_key=True),
        sa.Column("cursor", JSONB(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # Backs the Amplitude poller's per-tick window match (pro + recent sends).
    op.create_index("ix_exposures_pro_id_sent_at", "exposures", ["pro_id", "sent_at"])


def downgrade() -> None:
    op.drop_index("ix_exposures_pro_id_sent_at", table_name="exposures")
    op.drop_table("poll_cursors")
