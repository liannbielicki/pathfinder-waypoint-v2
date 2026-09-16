import json
import time

from fastapi.testclient import TestClient

from waypoint.workbench_jobs import WorkbenchJobStore, prune_job_result


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
            checkpoint(state)
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
