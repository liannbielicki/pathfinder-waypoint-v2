"""Durable local job state for the Context Workbench."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from waypoint.db import session_scope
from waypoint.tables import (
    ContextPromotionRow,
    WorkbenchCatalogVersionRow,
    WorkbenchJobRow,
)
from waypoint.workbench import is_pii_variable_key, scrub_pii

_SAFE_REQUEST_FIELDS = {
    "identifier", "identifier_type", "source_mode", "model", "context_policy",
    "enrichment", "channels", "journey_window", "candidate_count", "workbench_mode",
    "authoring_prompt", "feature_catalog_entries", "feature_catalog_version_id",
    "catalog_override", "catalog_version_id", "catalog_version_name",
    "catalog_version_saved_at",
}
_FEATURE_FIELDS = {
    "feature", "Product Area", "product_area", "category", "Value Statement",
    "description",
}
_CATALOG_FIELDS = {
    "key", "canonical_key", "value_category", "related_features", "usefulness_rank",
    "aggregate_prompt", "disposition", "review_status", "approval_status", "confidence",
    "uncertainty_reason", "exclusion_reason", "source_table",
}
_HIDDEN_STAGE_DATA = {
    "raw_context",
    "normalized_context",
    "scrubbed_context",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def sanitize_job_request(value: dict[str, Any]) -> dict[str, Any]:
    sanitized = {key: item for key, item in value.items() if key in _SAFE_REQUEST_FIELDS}
    for key in ("authoring_prompt", "catalog_version_name"):
        if key not in sanitized:
            continue
        safe_metadata, _ledger = scrub_pii({key: sanitized[key]})
        if key in safe_metadata:
            sanitized[key] = safe_metadata[key]
        else:
            sanitized.pop(key)
    feature_entries = sanitized.get("feature_catalog_entries")
    if isinstance(feature_entries, list):
        safe_features = []
        for entry in feature_entries:
            if not isinstance(entry, dict):
                continue
            safe, _ledger = scrub_pii(
                {key: item for key, item in entry.items() if key in _FEATURE_FIELDS}
            )
            if safe.get("feature"):
                safe_features.append(safe)
        sanitized["feature_catalog_entries"] = safe_features
    catalog_entries = sanitized.get("catalog_override")
    if isinstance(catalog_entries, list):
        entries = [entry for entry in catalog_entries if isinstance(entry, dict)]
        approved = [
            entry for entry in entries if str(entry.get("disposition")) == "include"
        ]
        pii_entries = [
            entry
            for entry in entries
            if is_pii_variable_key(str(entry.get("key") or ""))
            or is_pii_variable_key(str(entry.get("canonical_key") or ""))
        ]
        safe_catalog = []
        for entry in entries:
            if entry in pii_entries:
                continue
            safe, _ledger = scrub_pii(
                {key: item for key, item in entry.items() if key in _CATALOG_FIELDS}
            )
            safe_catalog.append(safe)
        sanitized["catalog_override"] = safe_catalog
        sanitized["catalog_approved_count"] = len(approved)
        sanitized["catalog_pii_removed_count"] = sum(
            1 for entry in approved if entry in pii_entries
        )
    return sanitized


@dataclass(frozen=True)
class WorkbenchJob:
    id: str
    status: str
    request: dict[str, Any]
    state: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    created_at: str
    updated_at: str


class WorkbenchJobStore:
    """Small SQLite store for restart-safe local Workbench jobs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def create(self, request: dict[str, Any]) -> WorkbenchJob:
        job_id = str(uuid.uuid4())
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    "queued",
                    json.dumps(sanitize_job_request(request), separators=(",", ":")),
                    "{}",
                    None,
                    None,
                    now,
                    now,
                ),
            )
        job = self.get(job_id)
        assert job is not None
        return job

    def get(self, job_id: str) -> WorkbenchJob | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return WorkbenchJob(
            id=str(row["id"]),
            status=str(row["status"]),
            request=json.loads(row["request_json"]),
            state=json.loads(row["state_json"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=str(row["error"]) if row["error"] else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def checkpoint(self, job_id: str, state: dict[str, Any]) -> None:
        self._update(
            job_id,
            status="running",
            state_json=json.dumps(state, separators=(",", ":")),
            error=None,
        )

    def set_status(self, job_id: str, status: str) -> None:
        self._update(job_id, status=status)

    def complete(self, job_id: str, result: dict[str, Any], *, status: str) -> None:
        self._update(
            job_id,
            status=status,
            result_json=json.dumps(result, separators=(",", ":")),
            error=None,
        )

    def fail(self, job_id: str, error: str) -> None:
        self._update(job_id, status="failed", error=error[:1000])

    def resumable(self) -> list[WorkbenchJob]:
        return self._list("WHERE status IN ('queued', 'running') ORDER BY created_at")

    def latest(self) -> WorkbenchJob | None:
        jobs = self._list("ORDER BY created_at DESC LIMIT 1")
        return jobs[0] if jobs else None

    def _list(self, suffix: str) -> list[WorkbenchJob]:
        with self._connect() as connection:
            rows = connection.execute(f"SELECT id FROM jobs {suffix}").fetchall()
        return [job for row in rows if (job := self.get(str(row["id"]))) is not None]

    def _update(self, job_id: str, **values: Any) -> None:
        values["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",
                (*values.values(), job_id),
            )


def _postgres_job(row: WorkbenchJobRow) -> WorkbenchJob:
    return WorkbenchJob(
        id=row.id,
        status=row.status,
        request=dict(row.request or {}),
        state=dict(row.state or {}),
        result=dict(row.result) if row.result is not None else None,
        error=row.error,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _catalog_payload(row: WorkbenchCatalogVersionRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "entries": list(row.entries or []),
        "details": dict(row.details or {}),
        "created_at": row.created_at.isoformat(),
    }


class PostgresWorkbenchStore:
    """Async durable store used by the Railway-hosted Workbench."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def create(
        self,
        request: dict[str, Any],
        *,
        session: AsyncSession | None = None,
    ) -> WorkbenchJob:
        row = WorkbenchJobRow(request=sanitize_job_request(request))
        if session is not None:
            session.add(row)
            await session.flush()
            return _postgres_job(row)
        async with session_scope(self.factory) as owned:
            owned.add(row)
            await owned.flush()
            return _postgres_job(row)

    async def get(self, job_id: str) -> WorkbenchJob | None:
        async with self.factory() as session:
            row = await session.get(WorkbenchJobRow, job_id)
            return _postgres_job(row) if row is not None else None

    async def checkpoint(self, job_id: str, state: dict[str, Any]) -> None:
        await self._update(job_id, status="running", state=state, error=None)

    async def set_status(self, job_id: str, status: str) -> None:
        await self._update(job_id, status=status)

    async def complete(self, job_id: str, result: dict[str, Any], *, status: str) -> None:
        await self._update(job_id, status=status, result=result, error=None)

    async def fail(self, job_id: str, error: str) -> None:
        await self._update(job_id, status="failed", error=error[:1000])

    async def resumable(self) -> list[WorkbenchJob]:
        async with self.factory() as session:
            rows = (
                await session.execute(
                    select(WorkbenchJobRow)
                    .where(WorkbenchJobRow.status.in_(("queued", "running")))
                    .order_by(WorkbenchJobRow.created_at)
                )
            ).scalars().all()
            return [_postgres_job(row) for row in rows]

    async def latest(self) -> WorkbenchJob | None:
        async with self.factory() as session:
            row = (
                await session.execute(
                    select(WorkbenchJobRow)
                    .order_by(WorkbenchJobRow.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return _postgres_job(row) if row is not None else None

    async def promote(
        self,
        bundle: dict[str, Any],
        *,
        session: AsyncSession | None = None,
    ) -> None:
        promotion_id = str(bundle.get("id") or "")
        if not promotion_id:
            raise ValueError("promotion id is required")
        if session is not None:
            await self._promote(session, promotion_id, bundle)
            return
        async with session_scope(self.factory) as owned:
            await self._promote(owned, promotion_id, bundle)

    async def _promote(
        self,
        session: AsyncSession,
        promotion_id: str,
        bundle: dict[str, Any],
    ) -> None:
        existing = await session.get(ContextPromotionRow, promotion_id)
        if existing is not None and existing.bundle != bundle:
            raise ValueError("promotion id already exists with different content")
        await session.execute(
            update(ContextPromotionRow)
            .where(ContextPromotionRow.id != promotion_id)
            .values(active=False)
        )
        if existing is None:
            inserted = (
                await session.execute(
                    insert(ContextPromotionRow)
                    .values(id=promotion_id, bundle=bundle, active=True)
                    .on_conflict_do_nothing(index_elements=["id"])
                    .returning(ContextPromotionRow.id)
                )
            ).scalar_one_or_none()
            if inserted is None:
                existing = await session.get(ContextPromotionRow, promotion_id)
                assert existing is not None
                if existing.bundle != bundle:
                    raise ValueError("promotion id already exists with different content")
        if existing is not None and not existing.active:
            existing.active = True
            existing.activated_at = datetime.now(UTC)
        await session.flush()

    async def read_active_promotion(self) -> dict[str, Any] | None:
        async with self.factory() as session:
            row = (
                await session.execute(
                    select(ContextPromotionRow).where(ContextPromotionRow.active.is_(True))
                )
            ).scalar_one_or_none()
            return dict(row.bundle) if row is not None else None

    async def save_catalog_version(
        self,
        version: dict[str, Any],
        *,
        session: AsyncSession | None = None,
    ) -> dict[str, Any]:
        if session is None:
            async with session_scope(self.factory) as owned:
                return await self.save_catalog_version(version, session=owned)
        version_id = str(version.get("id") or "")
        incoming: dict[str, Any] = {
            "kind": str(version.get("kind") or ""),
            "name": str(version.get("name") or version_id),
            "entries": list(version.get("entries") or []),
            "details": dict(version.get("details") or {}),
        }
        existing = await session.get(WorkbenchCatalogVersionRow, version_id)
        if existing is not None:
            current = _catalog_payload(existing)
            if (
                current["kind"] == incoming["kind"]
                and current["entries"] == incoming["entries"]
                and current["details"].get("recovered_metadata") is True
            ):
                existing.name = incoming["name"]
                existing.details = incoming["details"]
                await session.flush()
                await session.refresh(existing)
                return _catalog_payload(existing)
            if any(current[key] != incoming[key] for key in incoming):
                raise ValueError("catalog version id already exists with different content")
            return current
        inserted = (
            await session.execute(
                insert(WorkbenchCatalogVersionRow)
                .values(id=version_id, **incoming)
                .on_conflict_do_nothing(index_elements=["id"])
                .returning(WorkbenchCatalogVersionRow.id)
            )
        ).scalar_one_or_none()
        row = await session.get(WorkbenchCatalogVersionRow, version_id)
        assert row is not None
        current = _catalog_payload(row)
        if inserted is None and any(
            current[key] != incoming[key] for key in incoming
        ):
            raise ValueError("catalog version id already exists with different content")
        return current

    async def read_catalog_version(self, version_id: str) -> dict[str, Any] | None:
        async with self.factory() as session:
            row = await session.get(WorkbenchCatalogVersionRow, version_id)
            return _catalog_payload(row) if row is not None else None

    async def list_catalog_versions(
        self, kind: str | None = None
    ) -> list[dict[str, Any]]:
        async with self.factory() as session:
            query = select(WorkbenchCatalogVersionRow)
            if kind is not None:
                query = query.where(WorkbenchCatalogVersionRow.kind == kind)
            rows = (
                await session.execute(
                    query.order_by(
                        WorkbenchCatalogVersionRow.created_at.desc(),
                        WorkbenchCatalogVersionRow.id.desc(),
                    )
                )
            ).scalars().all()
            return [_catalog_payload(row) for row in rows]

    async def _update(self, job_id: str, **values: Any) -> None:
        async with session_scope(self.factory) as session:
            row = await session.get(WorkbenchJobRow, job_id)
            if row is None:
                return
            for key, value in values.items():
                setattr(row, key, value)
            await session.flush()


def prune_job_result(result: dict[str, Any]) -> dict[str, Any]:
    """Remove large or organization-valued stage bodies before durable storage."""
    pruned = cast(dict[str, Any], json.loads(json.dumps(result)))
    for stage in pruned.get("stages", []):
        name = str(stage.get("name") or "")
        if (
            name in _HIDDEN_STAGE_DATA
            or name.startswith((
                "authoring_prompt_",
                "authoring_response_",
                "authoring_repair_",
                "authoring_single_",
            ))
        ):
            stage["data"] = None
    return pruned
