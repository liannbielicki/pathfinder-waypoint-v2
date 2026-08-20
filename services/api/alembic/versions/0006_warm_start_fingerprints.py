# type: ignore
"""warm start fingerprints and eligibility

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "winners",
        sa.Column(
            "fingerprint",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("winners", sa.Column("fingerprint_version", sa.Text(), nullable=True))
    op.add_column(
        "winners",
        sa.Column(
            "warm_start_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "winners",
        sa.Column(
            "warm_start_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("winners", sa.Column("validation_status", sa.Text(), nullable=True))
    # Warm-start retrieval reads only eligible winners, newest first, within a
    # fingerprint version — a partial index keeps it off the whole table.
    op.create_index(
        "ix_winners_warm_start",
        "winners",
        ["fingerprint_version", sa.text("created_at DESC")],
        postgresql_where=sa.text("warm_start_eligible"),
    )
    # abandon_stale / replay look calls up by (run_id, pro_id, status).
    op.create_index("ix_llm_calls_run_pro_status", "llm_calls", ["run_id", "pro_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_llm_calls_run_pro_status", table_name="llm_calls")
    op.drop_index("ix_winners_warm_start", table_name="winners")
    op.drop_column("winners", "validation_status")
    op.drop_column("winners", "warm_start_evidence")
    op.drop_column("winners", "warm_start_eligible")
    op.drop_column("winners", "fingerprint_version")
    op.drop_column("winners", "fingerprint")
