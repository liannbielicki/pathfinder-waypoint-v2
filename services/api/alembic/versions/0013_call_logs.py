# type: ignore
"""Operator to-do log for call-channel winners (worked in-house, not via LCM)."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "call_logs",
        sa.Column("winner_id", sa.Text(), sa.ForeignKey("winners.id"), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="todo"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("call_logs")
