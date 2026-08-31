# type: ignore
"""V3 item identity and authoritative measurement fields."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in ("item_id", "item_version"):
        op.add_column("winners", sa.Column(column, sa.Text(), nullable=True))
    for column in ("item_id", "item_version", "arm"):
        op.add_column("touch_outcomes", sa.Column(column, sa.Text(), nullable=True))
    op.add_column("touch_outcomes", sa.Column("first_return_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("touch_outcomes", sa.Column("returned_1d", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("touch_outcomes", "returned_1d")
    op.drop_column("touch_outcomes", "first_return_at")
    for column in ("arm", "item_version", "item_id"):
        op.drop_column("touch_outcomes", column)
    for column in ("item_version", "item_id"):
        op.drop_column("winners", column)
