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
    "uncertainty_reason", "exclusion_reason",
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
    feature_entries = sanitized.get("feature_catalog_entries")
    if isinstance(feature_entries, list):
        sanitized["feature_catalog_entries"] = [
            {key: item for key, item in entry.items() if key in _FEATURE_FIELDS}
            for entry in feature_entries
            if isinstance(entry, dict)
        ]
    catalog_entries = sanitized.get("catalog_override")
    if isinstance(catalog_entries, list):
        sanitized["catalog_override"] = [
            {key: item for key, item in entry.items() if key in _CATALOG_FIELDS}
            for entry in catalog_entries
            if isinstance(entry, dict)
        ]
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
