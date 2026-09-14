# type: ignore
"""Add canonical exposure identity and send-confirmation fields."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exposures",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("run_id", sa.Text(), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("pro_id", sa.Text(), nullable=False),
        sa.Column("org_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("item_id", sa.Text(), nullable=True),
        sa.Column("item_version", sa.Text(), nullable=True),
        sa.Column("arm", sa.Text(), nullable=True),
        sa.Column("channel", sa.Text(), nullable=False, server_default=""),
        sa.Column("routing", sa.Text(), nullable=False, server_default=""),
        sa.Column("send_status", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.add_column("winners", sa.Column("legacy_unresolved", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("touch_outcomes", sa.Column("exposure_id", sa.Text(), nullable=True))
    op.add_column("touch_outcomes", sa.Column("send_status", sa.Text(), nullable=False, server_default="unknown"))
    op.add_column("touch_outcomes", sa.Column("send_confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key("fk_touch_outcomes_exposure", "touch_outcomes", "exposures", ["exposure_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_touch_outcomes_exposure", "touch_outcomes", type_="foreignkey")
    for column in ("send_confirmed_at", "send_status", "exposure_id"):
        op.drop_column("touch_outcomes", column)
    op.drop_column("winners", "legacy_unresolved")
    op.drop_table("exposures")
