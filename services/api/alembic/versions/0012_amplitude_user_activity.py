# type: ignore
"""Per-pro Amplitude return lookups: exposure coverage stamp + id cache.

The Export API cannot serve this project (a quiet hour measures ~4GB, its
response cap), so the amplitude source switched to per-pro User Activity
lookups. Coverage of return events becomes a per-exposure stamp instead of a
global cursor, and pro_uuid -> amplitude_id resolutions are cached.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "exposures", sa.Column("returns_checked_at", sa.DateTime(timezone=True))
    )
    op.create_table(
        "amplitude_ids",
        sa.Column("pro_id", sa.Text(), primary_key=True),
        sa.Column("amplitude_id", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("amplitude_ids")
    op.drop_column("exposures", "returns_checked_at")
