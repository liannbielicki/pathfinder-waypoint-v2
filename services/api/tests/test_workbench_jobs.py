import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from waypoint.hosted_workbench import HostedWorkbench
from waypoint.workbench_jobs import (
    PostgresWorkbenchStore,
    WorkbenchJobStore,
    prune_job_result,
)


def _complete_entry(key: str) -> dict[str, object]:
    return {
        "key": key,
        "canonical_key": key.casefold(),
        "value_category": "activity",
        "related_features": [],
        "usefulness_rank": 3,
        "disposition": "include",
        "aggregate_prompt": None,
        "confidence": 0.9,
        "uncertainty_reason": None,
        "approval_status": "auto_approved",
    }


def test_job_store_checkpoints_and_reopens(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = WorkbenchJobStore(path)
    job = store.create({"identifier": "org-123", "workbench_mode": "authoring"})
    store.checkpoint(job.id, {"entries": [_complete_entry("DONE")]})

    reopened = WorkbenchJobStore(path).get(job.id)

    assert reopened is not None
    assert reopened.state["entries"][0]["key"] == "DONE"
    assert reopened.status == "running"


def test_job_store_never_persists_secret_request_fields(tmp_path):
    store = WorkbenchJobStore(tmp_path / "jobs.sqlite3")
    job = store.create({
        "identifier": "org-123",
        "workbench_mode": "authoring",
        "ai_api_key": "anthropic-secret",
        "context_api_key": "context-secret",
        "n8n_webhook_token": "n8n-secret",
        "n8n_webhook_url": "https://n8n.test/capability-secret",
        "feature_catalog_csv": "feature,secret_token\nfoo,nested-secret\n",
        "feature_catalog_entries": [{
            "feature": "foo",
            "Product Area": "Operations",
            "Value Statement": "Useful product context",
            "secret_token": "nested-secret",
        }],
    })

    persisted = json.dumps(WorkbenchJobStore(store.path).get(job.id).request)
    assert "anthropic-secret" not in persisted
    assert "context-secret" not in persisted
    assert "n8n-secret" not in persisted
    assert "api_key" not in persisted
    assert "token" not in persisted
    assert "webhook" not in persisted
    assert "nested-secret" not in persisted
    assert "Useful product context" in persisted


def test_job_store_pii_gates_catalogs_before_persistence(tmp_path):
    store = WorkbenchJobStore(tmp_path / "jobs.sqlite3")
    job = store.create({
        "identifier": "org-123",
        "workbench_mode": "evaluate",
        "feature_catalog_entries": [{
            "feature": "jobs",
            "Value Statement": "Owner jane@example.com",
        }],
        "catalog_override": [
            _complete_entry("ORGANIZATION_ID"),
            _complete_entry("JOBS_CREATED_T28"),
        ],
    })

    assert job.request["catalog_override"] == [
        _complete_entry("ORGANIZATION_ID"),
        _complete_entry("JOBS_CREATED_T28"),
    ]
    assert job.request["catalog_approved_count"] == 2
    assert job.request["catalog_pii_removed_count"] == 0
    assert job.request["feature_catalog_entries"] == [{"feature": "jobs"}]


def test_prune_job_result_removes_prompt_response_and_scrubbed_stage_data():
    result = {
        "stages": [
            {"name": "input", "status": "succeeded", "data": {"identifier": "org-1"}},
            {"name": "authoring_prompt_1", "status": "succeeded", "data": "large prompt"},
            {"name": "authoring_response_1", "status": "succeeded", "data": "large response"},
            {"name": "scrubbed_context", "status": "succeeded", "data": {"VALUE": 12}},
            {"name": "pii_gate", "status": "succeeded", "data": {"removed_count": 1}},
        ],
        "warnings": [],
        "outputs": {"authoring": {"completed_keys": 1}},
    }

    pruned = prune_job_result(result)

    assert pruned["stages"][0]["data"] == {"identifier": "org-1"}
    assert [stage["data"] for stage in pruned["stages"][1:4]] == [None, None, None]
    assert pruned["stages"][4]["data"] == {"removed_count": 1}


async def test_postgres_store_persists_only_sanitized_job_state(db_session_factory):
    store = PostgresWorkbenchStore(db_session_factory)
    job = await store.create({
        "identifier": "org-123",
        "workbench_mode": "evaluate",
        "ai_api_key": "must-not-persist",
        "catalog_override": [
            _complete_entry("ORGANIZATION_ID"),
            _complete_entry("JOBS_CREATED_T28"),
        ],
    })
    await store.checkpoint(job.id, {"phase": "drafting", "pending_keys": 3})
    await store.complete(
        job.id,
        prune_job_result({
            "stages": [{"name": "scrubbed_context", "data": {"VALUE": 12}}],
            "outputs": {},
        }),
        status="completed",
    )

    persisted = await store.get(job.id)

    assert persisted is not None
    assert persisted.status == "completed"
    assert persisted.state == {"phase": "drafting", "pending_keys": 3}
    assert persisted.request["catalog_override"] == [
        _complete_entry("ORGANIZATION_ID"),
        _complete_entry("JOBS_CREATED_T28"),
    ]
    assert "must-not-persist" not in json.dumps(persisted.request)
    assert persisted.result["stages"][0]["data"] is None


async def test_postgres_store_enforces_one_active_job(db_session_factory):
    store = PostgresWorkbenchStore(db_session_factory)
    await store.create({"identifier": "org-1", "workbench_mode": "authoring"})

    with pytest.raises(IntegrityError):
        await store.create({"identifier": "org-2", "workbench_mode": "authoring"})


async def test_postgres_store_activates_one_immutable_promotion(db_session_factory):
    store = PostgresWorkbenchStore(db_session_factory)
    first = {"id": "promotion-one", "rules": [{"canonical_key": "jobs"}]}
    second = {"id": "promotion-two", "rules": [{"canonical_key": "invoices"}]}

    await store.promote(first)
    await store.promote(second)

    assert await store.read_active_promotion() == second
    with pytest.raises(ValueError, match="different content"):
        await store.promote({**second, "rules": []})


async def test_postgres_store_shares_immutable_catalog_versions(db_session_factory):
    store = PostgresWorkbenchStore(db_session_factory)
    version = {
        "id": "context-one",
        "kind": "context",
        "name": "First context",
        "entries": [_complete_entry("JOBS_CREATED_T28")],
        "details": {"feature_catalog_version_id": "features-one"},
    }

    saved = await store.save_catalog_version(version)
    repeated = await store.save_catalog_version(version)
    loaded = await PostgresWorkbenchStore(db_session_factory).read_catalog_version(
        "context-one"
    )

    assert repeated == saved
    assert loaded == saved
    assert loaded["created_at"].endswith("+00:00")
    with pytest.raises(ValueError, match="different content"):
        await store.save_catalog_version({**version, "name": "Changed"})


async def test_concurrent_identical_catalog_saves_are_idempotent(db_session_factory):
    version = {
        "id": "context-concurrent",
        "kind": "context",
        "name": "Concurrent context",
        "entries": [_complete_entry("JOBS_CREATED_T28")],
        "details": {},
    }

    first, second = await asyncio.gather(
        PostgresWorkbenchStore(db_session_factory).save_catalog_version(version),
        PostgresWorkbenchStore(db_session_factory).save_catalog_version(version),
    )

    assert first == second


async def test_backfilled_feature_metadata_can_be_repaired_once(db_session_factory):
    store = PostgresWorkbenchStore(db_session_factory)
    entries = [{"feature": "jobs", "description": "Manage jobs"}]
    await store.save_catalog_version({
        "id": "features-recovered", "kind": "feature",
        "name": "features-recovered", "entries": entries,
        "details": {"recovered_metadata": True},
    })

    repaired = await store.save_catalog_version({
        "id": "features-recovered", "kind": "feature",
        "name": "September features", "entries": entries,
        "details": {"source_filename": "features.csv"},
    })

    assert repaired["name"] == "September features"
    assert repaired["details"] == {"source_filename": "features.csv"}
    with pytest.raises(ValueError, match="different content"):
        await store.save_catalog_version({**repaired, "name": "Changed again"})


async def test_backfilled_context_metadata_keeps_its_feature_reference_on_repair(
    db_session_factory,
):
    store = PostgresWorkbenchStore(db_session_factory)
    entries = [_complete_entry("JOBS_CREATED_T28")]
    await store.save_catalog_version({
        "id": "context-recovered", "kind": "context",
        "name": "Recovered context", "entries": entries,
        "details": {
            "feature_catalog_version_id": "features-one",
            "recovered_metadata": True,
        },
    })

    repaired = await store.save_catalog_version({
        "id": "context-recovered", "kind": "context",
        "name": "September context", "entries": entries,
        "details": {
            "feature_catalog_version_id": "features-one",
            "prompt": "Create compact context.",
        },
    })

    assert repaired["name"] == "September context"
    assert repaired["details"] == {
        "feature_catalog_version_id": "features-one",
        "prompt": "Create compact context.",
    }


async def test_postgres_store_lists_catalog_versions_newest_first(db_session_factory):
    store = PostgresWorkbenchStore(db_session_factory)
    await store.save_catalog_version({
        "id": "context-one", "kind": "context", "name": "First",
        "entries": [], "details": {},
    })
    await store.save_catalog_version({
        "id": "features-one", "kind": "feature", "name": "Features",
        "entries": [{"feature": "jobs"}], "details": {},
    })

    assert [item["id"] for item in await store.list_catalog_versions()] == [
        "features-one", "context-one",
    ]
    assert [item["id"] for item in await store.list_catalog_versions("context")] == [
        "context-one"
    ]


async def test_two_hosted_processes_execute_one_workbench_job_once(
    db_session_factory,
) -> None:
    calls = 0

    async def fake_execute(body, *, resume_state=None, checkpoint=None):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return {
            "stages": [],
            "warnings": [],
            "outputs": {"authoring": {"review_exception_count": 0}},
        }

    store = PostgresWorkbenchStore(db_session_factory)
    job = await store.create({"identifier": "org-1", "workbench_mode": "authoring"})
    first = HostedWorkbench(db_session_factory, fake_execute)
    second = HostedWorkbench(db_session_factory, fake_execute)

    await asyncio.gather(first.run_job(job.id), second.run_job(job.id))

    completed = await store.get(job.id)
    assert calls == 1
    assert completed is not None and completed.status == "completed"


def _wait_for_terminal(client: TestClient, job_id: str) -> dict[str, object]:
    for _ in range(100):
        payload = client.get(f"/api/context-workbench/jobs/{job_id}").json()
        if payload["status"] in {"needs_review", "completed", "failed"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("job did not reach a terminal state")


def test_job_api_starts_reads_and_resumes_durable_jobs(tmp_path):
    from waypoint.workbench_api import create_workbench_app

    calls: list[dict[str, object]] = []

    async def fake_execute(body, *, resume_state=None, checkpoint=None):
        calls.append(dict(resume_state or {}))
        state = {"entries": [_complete_entry("DONE")], "revised_keys": []}
        if checkpoint:
            await checkpoint(state)
        return {
            "stages": [],
            "warnings": [],
            "outputs": {
                "authoring": {
                    "draft": state["entries"],
                    "review_exception_count": 0,
                }
            },
        }

    path = tmp_path / "jobs.sqlite3"
    app = create_workbench_app(job_db_path=path, job_executor=fake_execute)
    with TestClient(app) as client:
        started = client.post("/api/context-workbench/jobs", json={
            "identifier": "org-123",
            "source_mode": "snowflake",
            "workbench_mode": "authoring",
            "ai_api_key": "request-secret",
        })
        assert started.status_code == 202
        job_id = started.json()["id"]
        completed = _wait_for_terminal(client, job_id)
        assert completed["status"] == "completed"
        assert completed["result"]["outputs"]["authoring"]["draft"][0]["key"] == "DONE"
        assert client.get("/api/context-workbench/jobs/latest").json()["id"] == job_id

    store = WorkbenchJobStore(path)
    resumable = store.create({"identifier": "org-456", "workbench_mode": "authoring"})
    store.checkpoint(resumable.id, {"entries": [_complete_entry("SAVED")]})
    app = create_workbench_app(job_db_path=path, job_executor=fake_execute)
    with TestClient(app) as client:
        resumed = _wait_for_terminal(client, resumable.id)
        assert resumed["status"] == "completed"
        assert calls[-1]["entries"][0]["key"] == "SAVED"
        response = client.post(f"/api/context-workbench/jobs/{resumable.id}/resume")
        assert response.status_code == 200
        assert response.json()["id"] == resumable.id
