from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from pytest_httpx import HTTPXMock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.api import create_app
from waypoint.settings import Settings
from waypoint.tables import (
    CandidateRow,
    HandoffRow,
    JobRow,
    MeasurementRow,
    RunRow,
    TouchOutcomeRow,
    WinnerRow,
)

RUN_REQUEST = {
    "pro_ids": ["pro_1"],
    "audience_query": "audience_v7",
    "audience_run": "2026-08-06T18:00:00Z",
    "channels": ["sms"],
}

TEST_SETTINGS = Settings(
    _env_file=None,
    DATABASE_URL="postgresql+asyncpg://localhost:5432/waypoint_test",
    LLM_API_KEY="test",
    N8N_CONTEXT_URL="https://n8n.example/webhook/context",
    N8N_TOKEN="test",
    PERSONA_URL="https://personas.example/personas",
    PERSONA_TOKEN="test",
    HANDOFF_URL="https://lcm.example/handoff",
    HANDOFF_TOKEN="lcm-token",
    RUN_COST_USD="25.00",
    DAY_COST_USD="500.00",
    WORKER_COUNT=1,
    MODEL_FAST="claude-haiku-4-5",
    MODEL_DEEP="claude-sonnet-5",
    APP_PASSWORD="operator-password",
    SESSION_KEY="0123456789abcdef0123456789abcdef",
)


@pytest.fixture
async def client(db_session_factory) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings=TEST_SETTINGS, session_factory=db_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://operator.test") as c:
        yield c


@pytest.fixture
async def auth_client(client: httpx.AsyncClient) -> httpx.AsyncClient:
    response = await client.post("/api/auth/login", json={"password": "operator-password"})
    assert response.status_code == 200
    return client


async def test_run_api_requires_session(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/runs", json=RUN_REQUEST)).status_code == 401


async def test_wrong_password_is_rejected_without_www_authenticate(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/api/auth/login", json={"password": "wrong"})
    assert response.status_code == 401
    assert "www-authenticate" not in response.headers


async def test_tampered_cookie_is_rejected(client: httpx.AsyncClient) -> None:
    client.cookies.set("pf_session", "forged.value")
    assert (await client.post("/api/runs", json=RUN_REQUEST)).status_code == 401


async def test_start_returns_202_before_worker_runs(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    response = await auth_client.post("/api/runs", json=RUN_REQUEST)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["audience_query"] == "audience_v7"
    # A queued job exists for the run and the run budget comes from settings.
    job = (await db_session.execute(select(JobRow).where(JobRow.run_id == body["id"]))).scalar_one()
    assert job.status == "queued"
    run = await db_session.get(RunRow, body["id"])
    assert run is not None and run.cost_limit == Decimal("25.00")


async def test_run_detail_exposes_lifecycle_and_evidence(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    created = (await auth_client.post("/api/runs", json=RUN_REQUEST)).json()
    detail = (await auth_client.get(f"/api/runs/{created['id']}")).json()
    assert detail["status"] == "queued"
    assert detail["candidates"] == []
    assert detail["winners"] == []
    assert detail["killed"] is False


async def test_unknown_run_is_404(auth_client: httpx.AsyncClient) -> None:
    assert (await auth_client.get("/api/runs/missing")).status_code == 404


async def test_kill_stops_the_run(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    created = (await auth_client.post("/api/runs", json=RUN_REQUEST)).json()
    response = await auth_client.post(f"/api/runs/{created['id']}/kill")
    assert response.status_code == 200
    assert response.json()["status"] == "stopped"
    run = await db_session.get(RunRow, created["id"])
    assert run is not None
    assert run.status == "stopped" and run.stop_reason == "operator_kill"
    job = (
        await db_session.execute(select(JobRow).where(JobRow.run_id == created["id"]))
    ).scalar_one()
    assert job.status == "stopped"


async def test_kill_of_a_terminal_run_is_409_and_rewrites_nothing(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    created = (await auth_client.post("/api/runs", json=RUN_REQUEST)).json()
    run = await db_session.get(RunRow, created["id"])
    assert run is not None
    run.status = "complete"
    job = (
        await db_session.execute(select(JobRow).where(JobRow.run_id == created["id"]))
    ).scalar_one()
    job.status = "done"
    await db_session.commit()
    response = await auth_client.post(f"/api/runs/{created['id']}/kill")
    assert response.status_code == 409
    await db_session.refresh(run)
    await db_session.refresh(job)
    assert run.status == "complete" and run.stop_reason is None
    assert job.status == "done"


async def test_duplicate_pro_ids_are_deduped_not_500(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    body = {**RUN_REQUEST, "pro_ids": ["pro_1", "pro_2", "pro_1"]}
    response = await auth_client.post("/api/runs", json=body)
    assert response.status_code == 202
    created = response.json()
    assert created["pro_ids"] == ["pro_1", "pro_2"]
    jobs = (
        (await db_session.execute(select(JobRow).where(JobRow.run_id == created["id"])))
        .scalars()
        .all()
    )
    assert sorted(j.pro_id for j in jobs) == ["pro_1", "pro_2"]


async def test_handoff_without_ready_winner_is_409(
    auth_client: httpx.AsyncClient,
) -> None:
    created = (await auth_client.post("/api/runs", json=RUN_REQUEST)).json()
    response = await auth_client.post(f"/api/runs/{created['id']}/handoff")
    assert response.status_code == 409


async def test_handoff_creates_durable_receipt(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
    httpx_mock: HTTPXMock,
) -> None:
    created = (await auth_client.post("/api/runs", json=RUN_REQUEST)).json()
    run_id = created["id"]
    candidate = CandidateRow(
        run_id=run_id,
        pro_id="pro_1",
        recommendation={"title": "T", "mechanism": "invoice_delivery", "manager_rationale": "R"},
    )
    db_session.add(candidate)
    await db_session.flush()
    winner = WinnerRow(
        run_id=run_id,
        pro_id="pro_1",
        kind="winner",
        candidate_id=candidate.id,
        evidence={"org_id": "org_1", "final": {"reduction_pp": 4.0}},
    )
    db_session.add(winner)
    await db_session.flush()
    db_session.add(
        MeasurementRow(
            run_id=run_id,
            winner_id=winner.id,
            indicators=[
                {
                    "key": "invoices_sent",
                    "label": "Invoices sent",
                    "direction": "increase",
                    "source": "billing",
                    "window_days": 30,
                    "rationale": "r",
                }
            ],
        )
    )
    await db_session.commit()

    httpx_mock.add_response(json={"status": "accepted", "lcm_id": "lcm-9"})
    response = await auth_client.post(f"/api/runs/{run_id}/handoff")
    assert response.status_code == 200
    receipts = response.json()["receipts"]
    assert len(receipts) == 1
    assert receipts[0]["status"] == "accepted"
    assert receipts[0]["idempotency_key"] == f"{run_id}:{winner.id}"
    row = (
        await db_session.execute(select(HandoffRow).where(HandoffRow.run_id == run_id))
    ).scalar_one()
    assert row.payload["audience_lineage"]["audience_query"] == "audience_v7"


async def test_health_has_no_secret_or_dependency_payload(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).json() == {"status": "ok"}


async def test_kill_switch_env_applies_to_the_existing_fleet_row(
    db_session_factory,
    db_session: AsyncSession,
) -> None:
    from decimal import Decimal as D

    from waypoint.tables import FleetControlRow

    db_session.add(FleetControlRow(id=1, killed=False, day_cost_limit=D("1.00")))
    await db_session.commit()

    killed_settings = TEST_SETTINGS.model_copy(update={"KILL_SWITCH": True})
    app = create_app(settings=killed_settings, session_factory=db_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://t") as client:
        await client.post("/api/auth/login", json={"password": "operator-password"})
        response = await client.post("/api/runs", json=RUN_REQUEST)
        assert response.status_code == 202
    fleet = await db_session.get(FleetControlRow, 1)
    assert fleet is not None
    await db_session.refresh(fleet)
    assert fleet.killed is True  # Railway env flip + redeploy engages the kill


async def test_run_detail_reports_real_spend_from_usage_rows(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    from decimal import Decimal as D

    from waypoint.tables import UsageRow

    created = (await auth_client.post("/api/runs", json=RUN_REQUEST)).json()
    db_session.add(
        UsageRow(
            run_id=created["id"],
            stage="generate",
            model="m",
            input_tokens=10,
            output_tokens=5,
            cost_usd=D("0.75"),
        )
    )
    db_session.add(
        UsageRow(
            run_id=created["id"],
            stage="screen",
            model="m",
            input_tokens=10,
            output_tokens=5,
            cost_usd=D("0.25"),
        )
    )
    await db_session.commit()
    detail = (await auth_client.get(f"/api/runs/{created['id']}")).json()
    assert detail["cost_spent_usd"] == "1.0000"


async def test_spend_includes_abandoned_call_conversions_without_usage_rows(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    # An abandoned call converts its worst-case reservation to run.cost_spent
    # with NO usage row; the UI must not understate spend in exactly the
    # "did we pay for lost work?" case.
    created = (await auth_client.post("/api/runs", json=RUN_REQUEST)).json()
    run = await db_session.get(RunRow, created["id"])
    assert run is not None
    run.cost_spent = Decimal("0.9000")
    await db_session.commit()
    detail = (await auth_client.get(f"/api/runs/{created['id']}")).json()
    assert detail["cost_spent_usd"] == "0.9000"


async def test_run_creation_enqueues_one_job_per_pro(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    body = {**RUN_REQUEST, "pro_ids": ["pro_1", "pro_2", "pro_3"]}
    created = (await auth_client.post("/api/runs", json=body)).json()
    jobs = (
        (await db_session.execute(select(JobRow).where(JobRow.run_id == created["id"])))
        .scalars()
        .all()
    )
    assert sorted(j.pro_id for j in jobs) == ["pro_1", "pro_2", "pro_3"]
    assert all(j.stage == "pro" and j.status == "queued" for j in jobs)


async def test_loop_config_defaults_snapshot_onto_the_run(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    created = (await auth_client.post("/api/runs", json=RUN_REQUEST)).json()
    assert created["loop_config"] == {
        "MAX_ROUNDS": 10,
        "MAX_NO_IMPROVE": 3,
        "PATIENCE": 1,
        "KEEP_DELTA_PP": 0.5,
        "WIN_THRESHOLD_PP": 15.0,
    }


async def test_confirmed_override_snapshots_and_updates_persisted_defaults(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    from waypoint.tables import FleetControlRow

    body = {**RUN_REQUEST, "loop_config": {"MAX_ROUNDS": 4, "PATIENCE": 2}}
    created = (await auth_client.post("/api/runs", json=body)).json()
    assert created["loop_config"]["MAX_ROUNDS"] == 4
    assert created["loop_config"]["PATIENCE"] == 2
    assert created["loop_config"]["KEEP_DELTA_PP"] == 0.5  # untouched default
    fleet = await db_session.get(FleetControlRow, 1)
    assert fleet is not None
    await db_session.refresh(fleet)
    assert fleet.loop_defaults["MAX_ROUNDS"] == 4  # persisted for next time

    # The persisted defaults pre-fill the next run.
    second = (await auth_client.post("/api/runs", json=RUN_REQUEST)).json()
    assert second["loop_config"]["MAX_ROUNDS"] == 4


async def test_out_of_bounds_override_is_422_and_defaults_untouched(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    from waypoint.tables import FleetControlRow

    body = {**RUN_REQUEST, "loop_config": {"PATIENCE": 0}}
    response = await auth_client.post("/api/runs", json=body)
    assert response.status_code == 422
    fleet = await db_session.get(FleetControlRow, 1)
    if fleet is not None:
        await db_session.refresh(fleet)
        assert fleet.loop_defaults.get("PATIENCE") is None
    runs = (await db_session.execute(select(RunRow))).scalars().all()
    assert runs == []  # no run was created


async def test_fleet_settings_endpoint_exposes_defaults_and_the_cap(
    auth_client: httpx.AsyncClient,
) -> None:
    response = await auth_client.get("/api/fleet/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["max_in_flight_llm_calls"] == 4
    assert body["loop_defaults"]["MAX_ROUNDS"] == 10


async def test_fleet_settings_requires_session(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/fleet/settings")).status_code == 401


async def test_stages_aggregate_across_per_pro_jobs(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    from waypoint.queue import checkpoint_job

    body = {**RUN_REQUEST, "pro_ids": ["pro_1", "pro_2"]}
    created = (await auth_client.post("/api/runs", json=body)).json()
    jobs = (
        (await db_session.execute(select(JobRow).where(JobRow.run_id == created["id"])))
        .scalars()
        .all()
    )
    await checkpoint_job(db_session, jobs[0].id, "context", {"orgs": 1})
    await checkpoint_job(db_session, jobs[0].id, "evolve", {"rounds": 2})
    await checkpoint_job(db_session, jobs[1].id, "context", {"orgs": 1})
    await db_session.commit()
    detail = (await auth_client.get(f"/api/runs/{created['id']}")).json()
    # A stage shows done only when EVERY job checkpointed it — an honest floor.
    assert "context" in detail["stages"]
    assert "evolve" not in detail["stages"]


async def test_run_carries_journey_window(auth_client: httpx.AsyncClient) -> None:
    response = await auth_client.post(
        "/api/runs", json={**RUN_REQUEST, "journey_window": "onboarding"}
    )
    assert response.status_code == 202
    assert response.json()["journey_window"] == "onboarding"


async def test_run_defaults_journey_window(auth_client: httpx.AsyncClient) -> None:
    response = await auth_client.post("/api/runs", json=RUN_REQUEST)
    assert response.status_code == 202
    assert response.json()["journey_window"] == "churn_risk"


async def test_unknown_journey_window_is_rejected(auth_client: httpx.AsyncClient) -> None:
    response = await auth_client.post(
        "/api/runs", json={**RUN_REQUEST, "journey_window": "revenue_maximization"}
    )
    assert response.status_code == 422


OUTCOME = {
    "recommendation_id": "nonexistent-winner",
    "source": "iterable_n8n",
    "pro_id": "pro_1",
    "channel": "sms",
    "returned_7d": True,
}


async def test_outcomes_require_auth(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/outcomes", json=[OUTCOME])).status_code == 401


async def test_unattributed_outcome_is_stored_with_limitation(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    response = await auth_client.post("/api/outcomes", json=[OUTCOME])
    assert response.status_code == 202
    assert response.json() == {"stored": 1, "unattributed": 1}
    row = (await db_session.execute(select(TouchOutcomeRow))).scalar_one()
    assert row.evidence_limitation is not None
    assert "matches no winner" in row.evidence_limitation


async def test_attributed_outcome_backfills_from_winner(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    run = RunRow(pro_ids=["pro_1"], audience_query="q", audience_run="r",
                 channels=["sms"], journey_window="churn_risk")
    db_session.add(run)
    await db_session.flush()
    candidate = CandidateRow(
        run_id=run.id, pro_id="pro_1", status="champion",
        recommendation={"title": "t", "mechanism": "invoice_delivery", "actions": ["a"],
                        "pro_facing_concept": "c", "manager_rationale": "m",
                        "channel": "sms", "risk": ""},
    )
    db_session.add(candidate)
    await db_session.flush()
    winner = WinnerRow(run_id=run.id, pro_id="pro_1", kind="winner",
                       candidate_id=candidate.id, rationale="m")
    db_session.add(winner)
    await db_session.commit()

    response = await auth_client.post(
        "/api/outcomes",
        json=[{**OUTCOME, "recommendation_id": winner.id}],
    )
    assert response.status_code == 202
    assert response.json() == {"stored": 1, "unattributed": 0}
    row = (await db_session.execute(select(TouchOutcomeRow))).scalar_one()
    assert row.evidence_limitation is None
    assert row.mechanism == "invoice_delivery"
    assert row.journey_window == "churn_risk"
    assert row.run_id == run.id


async def test_outcome_resubmission_updates_in_place(auth_client: httpx.AsyncClient,
                                                     db_session: AsyncSession) -> None:
    await auth_client.post("/api/outcomes", json=[OUTCOME])
    await auth_client.post("/api/outcomes", json=[{**OUTCOME, "returned_30d": False}])
    rows = (await db_session.execute(select(TouchOutcomeRow))).scalars().all()
    assert len(rows) == 1
    assert rows[0].returned_7d is True
    assert rows[0].returned_30d is False


async def test_attributed_outcome_without_channel_backfills_and_counts_as_evidence(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    run = RunRow(pro_ids=["pro_1"], audience_query="q", audience_run="r",
                 channels=["sms"], journey_window="churn_risk")
    db_session.add(run)
    await db_session.flush()
    candidate = CandidateRow(
        run_id=run.id, pro_id="pro_1", status="champion",
        recommendation={"title": "t", "mechanism": "invoice_delivery", "actions": ["a"],
                        "pro_facing_concept": "c", "manager_rationale": "m",
                        "channel": "sms", "risk": ""},
    )
    db_session.add(candidate)
    await db_session.flush()
    winner = WinnerRow(run_id=run.id, pro_id="pro_1", kind="winner",
                       candidate_id=candidate.id, rationale="m",
                       evidence={"org_id": "org-42"})
    db_session.add(winner)
    await db_session.commit()

    # No channel/org_id supplied by the source — the TouchOutcomeIn defaults.
    outcome = {k: v for k, v in OUTCOME.items() if k != "channel"}
    response = await auth_client.post(
        "/api/outcomes", json=[{**outcome, "recommendation_id": winner.id}]
    )
    assert response.status_code == 202
    row = (await db_session.execute(select(TouchOutcomeRow))).scalar_one()
    assert row.channel == "sms"  # backfilled from the candidate's recommendation
    assert row.org_id == "org-42"  # backfilled from the winner's evidence

    from waypoint.evidence import pattern_summaries

    patterns = await pattern_summaries(db_session, "churn_risk", ["sms"])
    assert any(p.channel == "sms" and p.mechanism == "invoice_delivery" for p in patterns)


async def test_resubmission_re_attributes_once_the_winner_exists(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    rec_id = "future-winner"
    first = await auth_client.post(
        "/api/outcomes", json=[{**OUTCOME, "recommendation_id": rec_id}]
    )
    assert first.json() == {"stored": 1, "unattributed": 1}

    run = RunRow(pro_ids=["pro_1"], audience_query="q", audience_run="r",
                 channels=["sms"], journey_window="churn_risk")
    db_session.add(run)
    await db_session.flush()
    candidate = CandidateRow(
        run_id=run.id, pro_id="pro_1", status="champion",
        recommendation={"title": "t", "mechanism": "invoice_delivery", "actions": ["a"],
                        "pro_facing_concept": "c", "manager_rationale": "m",
                        "channel": "sms", "risk": ""},
    )
    db_session.add(candidate)
    await db_session.flush()
    winner = WinnerRow(id=rec_id, run_id=run.id, pro_id="pro_1", kind="winner",
                       candidate_id=candidate.id, rationale="m")
    db_session.add(winner)
    await db_session.commit()

    second = await auth_client.post(
        "/api/outcomes", json=[{**OUTCOME, "recommendation_id": rec_id}]
    )
    assert second.json() == {"stored": 1, "unattributed": 0}
    row = (
        await db_session.execute(
            select(TouchOutcomeRow).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.evidence_limitation is None
    assert row.mechanism == "invoice_delivery"
    assert row.run_id == run.id
