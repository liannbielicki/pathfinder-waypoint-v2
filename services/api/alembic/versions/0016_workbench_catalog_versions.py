# type: ignore
"""Persist shared immutable Workbench catalog versions."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "context_promotions",
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE context_promotions SET activated_at = created_at")
    op.alter_column(
        "context_promotions",
        "activated_at",
        nullable=False,
        server_default=sa.func.now(),
    )
    op.create_table(
        "workbench_catalog_versions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("entries", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "kind IN ('context', 'feature')",
            name="ck_workbench_catalog_versions_kind",
        ),
    )
    op.execute("""
        INSERT INTO workbench_catalog_versions
            (id, kind, name, entries, details, created_at)
        SELECT DISTINCT ON (request->>'catalog_version_id')
            request->>'catalog_version_id',
            'context',
            COALESCE(NULLIF(request->>'catalog_version_name', ''),
                     request->>'catalog_version_id'),
            request->'catalog_override',
            jsonb_strip_nulls(jsonb_build_object(
                'feature_catalog_version_id', request->>'feature_catalog_version_id',
                'recovered_metadata', true
            )),
            updated_at
        FROM workbench_jobs
        WHERE status = 'completed'
          AND NULLIF(request->>'catalog_version_id', '') IS NOT NULL
          AND jsonb_typeof(request->'catalog_override') = 'array'
        ORDER BY request->>'catalog_version_id', updated_at DESC
        ON CONFLICT (id) DO NOTHING
    """)
    op.execute("""
        INSERT INTO workbench_catalog_versions
            (id, kind, name, entries, details, created_at)
        SELECT DISTINCT ON (request->>'feature_catalog_version_id')
            request->>'feature_catalog_version_id',
            'feature',
            request->>'feature_catalog_version_id',
            request->'feature_catalog_entries',
            '{"recovered_metadata": true}'::jsonb,
            updated_at
        FROM workbench_jobs
        WHERE status = 'completed'
          AND NULLIF(request->>'feature_catalog_version_id', '') IS NOT NULL
          AND jsonb_typeof(request->'feature_catalog_entries') = 'array'
        ORDER BY request->>'feature_catalog_version_id', updated_at DESC
        ON CONFLICT (id) DO NOTHING
    """)


def downgrade() -> None:
    op.drop_table("workbench_catalog_versions")
    op.drop_column("context_promotions", "activated_at")
