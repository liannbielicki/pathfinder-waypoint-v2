# type: ignore
"""V3 learning loop: item corpus, learning kill switch, version markers."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "items",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("mechanism", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False, server_default=""),
        sa.Column("concept", sa.Text(), nullable=False),
        sa.Column("concept_hash", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("item_metadata", JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("resolver_version", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("mechanism", "channel", "concept_hash", name="uq_items_identity"),
    )
    op.add_column(
        "fleet_control",
        sa.Column("learning_killed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("touch_outcomes", sa.Column("checkpoint_version", sa.Text(), nullable=True))
    op.add_column(
        "exposures",
        sa.Column("learning_version", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column("exposures", sa.Column("winner_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_exposures_winner", "exposures", "winners", ["winner_id"], ["id"]
    )
    op.create_index("ix_exposures_winner_id", "exposures", ["winner_id"])
    # Backs the bounded checkpoint sweep: confirmed sends with an unresolved
    # learning horizon, oldest first.
    op.create_index(
        "ix_touch_outcomes_checkpoint_due",
        "touch_outcomes",
        ["sent_at"],
        postgresql_where=sa.text(
            "send_status = 'confirmed' AND "
            "(returned_1d IS NULL OR returned_7d IS NULL OR returned_30d IS NULL)"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_touch_outcomes_checkpoint_due", table_name="touch_outcomes")
    op.drop_index("ix_exposures_winner_id", table_name="exposures")
    op.drop_constraint("fk_exposures_winner", "exposures", type_="foreignkey")
    op.drop_column("exposures", "winner_id")
    op.drop_column("exposures", "learning_version")
    op.drop_column("touch_outcomes", "checkpoint_version")
    op.drop_column("fleet_control", "learning_killed")
    op.drop_table("items")
