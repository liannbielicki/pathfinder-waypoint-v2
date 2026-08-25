# type: ignore
"""touch outcome routing

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-25

Routing has to live on the ROW, not be recomputed from whichever submission
happens to arrive last. One touch is written several times (the send event, then
each return horizon), and only some of those submissions know how the message
was routed — so a per-submission check protected only the first write and let a
later one launder a guardrailed touch into evidence.

Backfill is deliberately '' (no claim), not 'route-to-pro': every row that
predates this column was written without the gate, so none of them is PROOF of a
real send. '' fails closed — those rows read as unattributable until a source
resubmits them with real routing.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "touch_outcomes",
        sa.Column("routing", sa.String(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("touch_outcomes", "routing")
