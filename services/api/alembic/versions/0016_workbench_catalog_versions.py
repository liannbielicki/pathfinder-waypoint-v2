# type: ignore
"""Persist shared immutable Workbench catalog versions."""

import hashlib
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_CATALOG_CONFLICT_REPAIR = """
    WITH feature_snapshots AS (
        SELECT
            request->>'feature_catalog_version_id' AS original_id,
            request->'feature_catalog_entries' AS entries,
            MAX(updated_at) AS last_seen,
            md5((request->'feature_catalog_entries')::text) AS fingerprint
        FROM workbench_jobs
        WHERE status = 'completed'
          AND NULLIF(request->>'feature_catalog_version_id', '') IS NOT NULL
          AND jsonb_typeof(request->'feature_catalog_entries') = 'array'
        GROUP BY
            request->>'feature_catalog_version_id',
            request->'feature_catalog_entries'
    ),
    feature_variants AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY original_id
                ORDER BY last_seen DESC, fingerprint DESC
            ) AS variant_number
        FROM feature_snapshots
    ),
    feature_mapping AS (
        SELECT
            original_id,
            entries,
            CASE
                WHEN variant_number = 1 AND NOT EXISTS (
                    SELECT 1
                    FROM workbench_jobs context_job
                    WHERE NULLIF(
                        context_job.request->>'catalog_version_id', ''
                    ) = feature_variants.original_id
                ) THEN original_id
                ELSE original_id || '-recovered-feature-' || fingerprint
            END AS recovered_id,
            variant_number
        FROM feature_variants
    ),
    updated_jobs AS (
        UPDATE workbench_jobs job
        SET request = jsonb_set(
            job.request,
            '{feature_catalog_version_id}',
            to_jsonb(feature_mapping.recovered_id),
            false
        )
        FROM feature_mapping
        WHERE job.request->>'feature_catalog_version_id' = feature_mapping.original_id
          AND job.request->'feature_catalog_entries' = feature_mapping.entries
        RETURNING job.id
    )
    UPDATE context_promotions promotion
    SET bundle = jsonb_set(
        promotion.bundle,
        '{feature_catalog_version_id}',
        to_jsonb(feature_mapping.recovered_id),
        false
    )
    FROM feature_mapping
    WHERE feature_mapping.variant_number = 1
      AND feature_mapping.recovered_id != feature_mapping.original_id
      AND promotion.bundle->>'feature_catalog_version_id' = feature_mapping.original_id
"""

CONTEXT_CATALOG_CONFLICT_REPAIR = """
    WITH context_snapshots AS (
        SELECT
            request->>'catalog_version_id' AS original_id,
            request->'catalog_override' AS entries,
            request->>'feature_catalog_version_id' AS feature_catalog_version_id,
            MAX(updated_at) AS last_seen,
            md5(jsonb_build_object(
                'entries', request->'catalog_override',
                'feature_catalog_version_id',
                request->>'feature_catalog_version_id'
            )::text) AS fingerprint
        FROM workbench_jobs
        WHERE status = 'completed'
          AND NULLIF(request->>'catalog_version_id', '') IS NOT NULL
          AND jsonb_typeof(request->'catalog_override') = 'array'
        GROUP BY
            request->>'catalog_version_id',
            request->'catalog_override',
            request->>'feature_catalog_version_id'
    ),
    context_variants AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY original_id
                ORDER BY last_seen DESC, fingerprint DESC
            ) AS variant_number
        FROM context_snapshots
    ),
    context_mapping AS (
        SELECT
            original_id,
            entries,
            feature_catalog_version_id,
            CASE
                WHEN variant_number = 1 THEN original_id
                ELSE original_id || '-recovered-context-' || fingerprint
            END AS recovered_id
        FROM context_variants
    )
    UPDATE workbench_jobs job
    SET request = jsonb_set(
        job.request,
        '{catalog_version_id}',
        to_jsonb(context_mapping.recovered_id),
        false
    )
    FROM context_mapping
    WHERE job.request->>'catalog_version_id' = context_mapping.original_id
      AND job.request->'catalog_override' = context_mapping.entries
      AND job.request->>'feature_catalog_version_id'
          IS NOT DISTINCT FROM context_mapping.feature_catalog_version_id
"""

CATALOG_CONFLICT_PREFLIGHT = """
    DO $$
    BEGIN
        IF EXISTS (
            WITH recovered AS (
                SELECT
                    request->>'catalog_version_id' AS id,
                    jsonb_build_object(
                        'kind', 'context',
                        'entries', request->'catalog_override',
                        'feature_catalog_version_id',
                        request->>'feature_catalog_version_id'
                    ) AS payload
                FROM workbench_jobs
                WHERE status = 'completed'
                  AND NULLIF(request->>'catalog_version_id', '') IS NOT NULL
                  AND jsonb_typeof(request->'catalog_override') = 'array'
                UNION ALL
                SELECT
                    request->>'feature_catalog_version_id' AS id,
                    jsonb_build_object(
                        'kind', 'feature',
                        'entries', request->'feature_catalog_entries'
                    ) AS payload
                FROM workbench_jobs
                WHERE status = 'completed'
                  AND NULLIF(request->>'feature_catalog_version_id', '') IS NOT NULL
                  AND jsonb_typeof(request->'feature_catalog_entries') = 'array'
            )
            SELECT 1
            FROM recovered
            GROUP BY id
            HAVING COUNT(DISTINCT payload) > 1
        ) THEN
            RAISE EXCEPTION
                'conflicting historical Workbench catalog versions require manual repair';
        END IF;
    END $$
"""


def _promotion_id(job_id: str) -> str:
    fingerprint = json.dumps(
        {"evaluation_job_id": job_id, "policy": "pii-first-canonical-v1"},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"promotion-{hashlib.sha256(fingerprint.encode()).hexdigest()[:16]}"


def _repair_promotion_references(connection: Any | None = None) -> None:
    connection = connection or op.get_bind()
    jobs = connection.execute(sa.text("""
        SELECT id, request
        FROM workbench_jobs
        WHERE status = 'completed'
    """)).mappings()
    jobs_by_promotion = {_promotion_id(str(row["id"])): row["request"] for row in jobs}
    promotions = connection.execute(sa.text("""
        SELECT id, active
        FROM context_promotions
    """)).mappings()
    for promotion in promotions:
        request = jobs_by_promotion.get(str(promotion["id"]))
        context_id = str(request.get("catalog_version_id") or "") if request else ""
        feature_id = str(request.get("feature_catalog_version_id") or "") if request else ""
        if not context_id or not feature_id:
            if promotion["active"]:
                raise RuntimeError(
                    f"active promotion {promotion['id']} has no matching historical job"
                )
            continue
        connection.execute(
            sa.text("""
                UPDATE context_promotions
                SET bundle = jsonb_set(
                    jsonb_set(
                        bundle,
                        '{context_catalog_version_id}',
                        to_jsonb(CAST(:context_id AS text)),
                        false
                    ),
                    '{feature_catalog_version_id}',
                    to_jsonb(CAST(:feature_id AS text)),
                    false
                )
                WHERE id = :promotion_id
            """),
            {
                "promotion_id": promotion["id"],
                "context_id": context_id,
                "feature_id": feature_id,
            },
        )


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
    op.execute(FEATURE_CATALOG_CONFLICT_REPAIR)
    op.execute(CONTEXT_CATALOG_CONFLICT_REPAIR)
    _repair_promotion_references()
    op.execute(CATALOG_CONFLICT_PREFLIGHT)
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
