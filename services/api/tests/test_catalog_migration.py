import hashlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.tables import ContextPromotionRow, WorkbenchJobRow


def _migration_module():
    path = (
        Path(__file__).parents[1]
        / "alembic/versions/0016_workbench_catalog_versions.py"
    )
    spec = importlib.util.spec_from_file_location("workbench_catalog_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_catalog_migration_preserves_conflicting_historical_versions(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    db_session.add_all([
        WorkbenchJobRow(
            id="historic-a",
            status="completed",
            request={
                "catalog_version_id": "context-reused",
                "catalog_override": [{"key": "A"}],
                "feature_catalog_version_id": "features-reused",
                "feature_catalog_entries": [{"feature": "A"}],
            },
            updated_at=now - timedelta(hours=1),
        ),
        WorkbenchJobRow(
            id="historic-b",
            status="completed",
            request={
                "catalog_version_id": "context-reused",
                "catalog_override": [{"key": "B"}],
                "feature_catalog_version_id": "features-reused",
                "feature_catalog_entries": [{"feature": "B"}],
            },
            updated_at=now,
        ),
    ])
    fingerprint = json.dumps(
        {"evaluation_job_id": "historic-a", "policy": "pii-first-canonical-v1"},
        sort_keys=True,
        separators=(",", ":"),
    )
    promotion_id = f"promotion-{hashlib.sha256(fingerprint.encode()).hexdigest()[:16]}"
    db_session.add(ContextPromotionRow(
        id=promotion_id,
        bundle={
            "id": promotion_id,
            "context_catalog_version_id": "context-reused",
            "feature_catalog_version_id": "features-reused",
            "rules": [{"source_key": "A"}],
        },
        active=True,
    ))
    await db_session.commit()

    migration = _migration_module()
    await db_session.execute(text(migration.FEATURE_CATALOG_CONFLICT_REPAIR))
    await db_session.execute(text(migration.CONTEXT_CATALOG_CONFLICT_REPAIR))
    await db_session.run_sync(
        lambda session: migration._repair_promotion_references(session.connection())
    )
    await db_session.execute(text(migration.CATALOG_CONFLICT_PREFLIGHT))

    repaired = (
        await db_session.execute(text("""
            SELECT
                request->>'catalog_version_id',
                request->>'feature_catalog_version_id'
            FROM workbench_jobs
            ORDER BY id
        """))
    ).all()
    context_ids = {row[0] for row in repaired}
    feature_ids = {row[1] for row in repaired}
    assert len(context_ids) == 2
    assert "context-reused" in context_ids
    assert any(value.startswith("context-reused-recovered-") for value in context_ids)
    assert len(feature_ids) == 2
    assert "features-reused" in feature_ids
    assert any(value.startswith("features-reused-recovered-") for value in feature_ids)

    historic_a = next(row for row in repaired if row[0] != "context-reused")
    promotion = await db_session.get(ContextPromotionRow, promotion_id)
    assert promotion is not None
    assert promotion.bundle["context_catalog_version_id"] == historic_a[0]
    assert promotion.bundle["feature_catalog_version_id"] == historic_a[1]
