# type: ignore
"""Persist the Standard or Staging context source on each run."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("context_source", sa.Text(), nullable=False, server_default="standard"),
    )
    op.create_check_constraint(
        "ck_runs_context_source",
        "runs",
        "context_source IN ('standard', 'staging')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_runs_context_source", "runs", type_="check")
    op.drop_column("runs", "context_source")
