import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.tables import WorkbenchJobRow


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


async def test_catalog_migration_rejects_conflicting_historical_versions(
    db_session: AsyncSession,
) -> None:
    db_session.add_all([
        WorkbenchJobRow(
            id="historic-a",
            status="completed",
            request={
                "catalog_version_id": "context-reused",
                "catalog_override": [{"key": "A"}],
                "feature_catalog_version_id": "features-a",
            },
        ),
        WorkbenchJobRow(
            id="historic-b",
            status="completed",
            request={
                "catalog_version_id": "context-reused",
                "catalog_override": [{"key": "B"}],
                "feature_catalog_version_id": "features-b",
            },
        ),
    ])
    await db_session.commit()

    migration = _migration_module()
    with pytest.raises(DBAPIError, match="conflicting historical Workbench catalog"):
        await db_session.execute(text(migration.CATALOG_CONFLICT_PREFLIGHT))
