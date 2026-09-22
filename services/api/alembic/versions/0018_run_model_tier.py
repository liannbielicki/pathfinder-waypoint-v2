# type: ignore
"""Persist the selected model tier on each run."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("model_tier", sa.Text(), server_default="fast", nullable=False),
    )
    op.create_check_constraint(
        "ck_runs_model_tier", "runs", "model_tier IN ('fast', 'deep')"
    )
    op.alter_column("runs", "model_tier", server_default="deep")


def downgrade() -> None:
    op.drop_constraint("ck_runs_model_tier", "runs", type_="check")
    op.drop_column("runs", "model_tier")
