import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from pytest_httpx import HTTPXMock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import TEST_SETTINGS
from waypoint.api import create_app
from waypoint.tables import (
    CandidateRow,
    ContextPromotionRow,
    EvolveRoundRow,
    HandoffRow,
    JobRow,
    MeasurementRow,
    RunRow,
    TouchOutcomeRow,
    WinnerRow,
    WorkbenchCatalogVersionRow,
    WorkbenchJobRow,
)

RUN_REQUEST = {
    "pro_ids": ["pro_1"],
    "audience_query": "audience_v7",
    "audience_run": "2026-08-06T18:00:00Z",
    "channels": ["sms"],
}


def _ready_staging_rows() -> list[object]:
    return [
        WorkbenchCatalogVersionRow(
            id="context-ready", kind="context", name="Ready context",
            entries=[], details={"feature_catalog_version_id": "features-ready"},
        ),
        WorkbenchCatalogVersionRow(
            id="features-ready", kind="feature", name="Ready features",
            entries=[], details={},
        ),
        ContextPromotionRow(
            id="promotion-ready",
            active=True,
            bundle={
                "id": "promotion-ready",
                "context_catalog_version_id": "context-ready",
                "feature_catalog_version_id": "features-ready",
                "rules": [],
            },
        ),
    ]



async def test_run_api_rejects_unknown_channel(client: httpx.AsyncClient) -> None:
    await client.post("/api/auth/login", json={"password": "operator-password"})
    bad = {**RUN_REQUEST, "channels": ["fax"]}
    assert (await client.post("/api/runs", json=bad)).status_code == 422


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
    assert body["context_source"] == "standard"
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


async def test_run_detail_winner_shows_warm_start_eligibility(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    created = (await auth_client.post("/api/runs", json=RUN_REQUEST)).json()
    db_session.add(
        WinnerRow(run_id=created["id"], pro_id="pro_1", kind="winner",
                  fingerprint_version="fp_v1")
    )
    await db_session.commit()
    winner = (await auth_client.get(f"/api/runs/{created['id']}")).json()["winners"][0]
    assert winner["warm_start_eligible"] is False
    assert winner["validation_status"] is None
    assert winner["fingerprint_version"] == "fp_v1"


async def test_run_detail_exposes_per_pro_loop_rounds(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    created = (await auth_client.post("/api/runs", json=RUN_REQUEST)).json()
    db_session.add(
        EvolveRoundRow(
            run_id=created["id"], pro_id="pro_1", round=1,
            mechanism="discount", outcome="win", score_pp=1.2,
        )
    )
    await db_session.commit()
    detail = (await auth_client.get(f"/api/runs/{created['id']}")).json()
    assert detail["rounds"] == [
        {
            "pro_id": "pro_1",
            "round": 1,
            "mechanism": "discount",
            "outcome": "win",
            "score_pp": 1.2,
        }
    ]


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
        recommendation={"title": "T", "mechanism": "invoice_delivery",
                        "pro_facing_concept": "C", "manager_rationale": "R"},
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

    httpx_mock.add_response(json={
        "batch": run_id, "rows": [{"row_id": winner.id, "status": "accepted"}],
    })
    response = await auth_client.post(f"/api/runs/{run_id}/handoff")
    assert response.status_code == 200
    receipts = response.json()["receipts"]
    assert len(receipts) == 1
    assert receipts[0]["status"] == "accepted"
    assert receipts[0]["idempotency_key"] == f"{run_id}:{winner.id}"
    row = (
        await db_session.execute(select(HandoffRow).where(HandoffRow.run_id == run_id))
    ).scalar_one()
    # Pathfinder Intake API shape: pro_uuid only, no email/name PII.
    assert row.payload == {
        "pro_uuid": "pro_1", "theme": "T: C", "theme_category": "invoice_delivery",
        "org_id": "org_1", "row_id": winner.id,
    }


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
        "CANDIDATE_COUNT": 3,
        "TIE_MARGIN": 0.05,
        "WARM_START_THRESHOLD": 0.75,
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
    assert body["staging_context_available"] is False
    assert body["staging_context"] is None


async def test_staging_run_is_rejected_when_workbench_url_is_unavailable(
    db_session_factory,
) -> None:
    from waypoint.api import create_app

    settings = TEST_SETTINGS.model_copy(update={"N8N_CONTEXT_URL_WORKBENCH": None})
    app = create_app(settings=settings, session_factory=db_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://operator.test") as client:
        assert (await client.post(
            "/api/auth/login", json={"password": "operator-password"}
        )).status_code == 200
        response = await client.post(
            "/api/runs", json={**RUN_REQUEST, "context_source": "staging"}
        )

    assert response.status_code == 422
    assert "staging" in response.text.casefold()


async def test_staging_readiness_ignores_deprecated_staging_url(
    db_session_factory,
    db_session: AsyncSession,
) -> None:
    from waypoint.api import create_app

    settings = TEST_SETTINGS.model_copy(update={"N8N_CONTEXT_URL_STAGING": None})
    db_session.add_all(_ready_staging_rows())
    await db_session.commit()
    app = create_app(settings=settings, session_factory=db_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://operator.test") as client:
        assert (await client.post(
            "/api/auth/login", json={"password": "operator-password"}
        )).status_code == 200
        response = await client.get("/api/fleet/settings")

    assert response.status_code == 200
    assert response.json()["staging_context_available"] is True


@pytest.mark.parametrize("invalid", ["pro_abc", "1234", "1234567"])
async def test_staging_rejects_ids_outside_five_or_six_digits_before_enqueue(
    auth_client: httpx.AsyncClient,
    invalid: str,
) -> None:
    response = await auth_client.post(
        "/api/runs",
        json={**RUN_REQUEST, "pro_ids": [invalid], "context_source": "staging"},
    )

    assert response.status_code == 422
    assert "five- or six-digit organization id" in response.text.casefold()


async def test_staging_preserves_numeric_organization_id_as_a_string(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    db_session.add_all(_ready_staging_rows())
    await db_session.commit()
    response = await auth_client.post(
        "/api/runs",
        json={
            **RUN_REQUEST,
            "pro_ids": ["31336"],
            "context_source": "staging",
            "context_promotion_id": "promotion-ready",
            "include_features_not_in_current_plan": True,
        },
    )

    assert response.status_code == 202
    assert response.json()["pro_ids"] == ["31336"]
    assert response.json()["audience_query"] == "workbench:promotion-ready"
    assert response.json()["include_features_not_in_current_plan"] is True
    persisted = await db_session.get(RunRow, response.json()["id"])
    assert persisted is not None
    assert persisted.include_features_not_in_current_plan is True


async def test_staging_callback_compiles_compact_context_and_requeues_once(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    promotion = ContextPromotionRow(
        id="promotion-callback",
        active=True,
        bundle={
            "id": "promotion-callback",
            "rules": [{
                "source_key": "SAFE_SIGNAL",
                "source_table": "ANALYTICS.SIGNALS",
                "canonical_key": "safe_signal",
                "related_features": ["checklists"],
            }, {
                "source_key": "RAW_PROFILE",
                "source_table": "ANALYTICS.SIGNALS",
                "canonical_key": "raw_profile",
                "related_features": [],
            }],
            "feature_catalog": [{
                "feature": "checklists",
                "Plans": "Core SaaS Essentials, Core SaaS MAX, Core SaaS MAX+",
                "Value Statement": "Create reusable job checklists.",
            }],
        },
    )
    run = RunRow(
        id="run-callback",
        pro_ids=["889901"],
        audience_query="workbench:promotion-callback",
        audience_run="2026-09-18T18:00:00Z",
        channels=["sms"],
        context_source="staging",
        include_features_not_in_current_plan=True,
        cost_limit=Decimal("25.00"),
        status="waiting",
    )
    job = JobRow(
        id="job-callback",
        run_id=run.id,
        stage="pro",
        pro_id="889901",
        status="waiting",
        checkpoint={"staging_request": {"promotion_id": promotion.id}},
    )
    db_session.add_all([promotion, run])
    await db_session.flush()
    db_session.add(job)
    await db_session.commit()

    async def fake_context_layer(self, organization_id, base_url, api_key):
        assert organization_id == "cc962bf1-13bb-4eea-bf66-f3adc9e22192"
        return {"firmographics": {"segment": "1A", "industry": "HVAC"}}

    monkeypatch.setattr("waypoint.api.ContextLayerClient.fetch", fake_context_layer)
    payload = {
        "request_id": job.id,
        "organization_id": "889901",
        "promotion_id": promotion.id,
        "rows": [
            {
                "VARIABLE_NAME": "ORG_SNAPSHOT",
                "VALUE": {
                    "ORG_UUID": "cc962bf1-13bb-4eea-bf66-f3adc9e22192",
                    "CORE_SAAS_PLAN_LEVEL": "Basic",
                },
            },
            {
                "VARIABLE_NAME": "SAFE_SIGNAL",
                "VALUE": 7,
                "METADATA": {"source_table": "ANALYTICS.SIGNALS"},
            },
            {
                "VARIABLE_NAME": "RAW_PROFILE",
                "VALUE": {"email": "must-not-persist@example.test"},
                "METADATA": {"source_table": "ANALYTICS.SIGNALS"},
            },
        ],
    }
    headers = {"authorization": "Bearer test"}

    first = await client.post("/api/context/staging/callback", json=payload, headers=headers)
    second = await client.post("/api/context/staging/callback", json=payload, headers=headers)

    await db_session.refresh(job)
    assert first.status_code == 200
    assert first.json()["status"] == "queued"
    assert second.status_code == 200
    assert second.json()["status"] == "already_completed"
    assert job.status == "queued"
    stored = job.checkpoint["staging_context"]
    assert stored["promotion_id"] == promotion.id
    assert stored["brief"]["curated_context"] == {
        "v": {
            "core_saas_plan": "Core SaaS Basic",
            "industry": "HVAC",
            "safe_signal": 7,
            "segment": "1A",
        },
        "f": {"safe_signal": ["checklists"]},
        "pc": {
            "checklists": {
                "e": "not_in_current_plan",
                "p": ["Core SaaS Essentials", "Core SaaS MAX", "Core SaaS MAX+"],
                "v": "Create reusable job checklists.",
            }
        },
    }
    assert "SAFE_SIGNAL" not in str(stored)
    assert "cc962bf1-13bb-4eea-bf66-f3adc9e22192" not in str(stored)
    assert "must-not-persist" not in str(stored)


async def test_staging_callback_never_resurrects_a_stopped_job(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    promotion = ContextPromotionRow(
        id="promotion-stopped",
        active=True,
        bundle={
            "id": "promotion-stopped",
            "rules": [{
                "source_key": "SAFE_SIGNAL",
                "source_table": "ANALYTICS.SIGNALS",
                "canonical_key": "safe_signal",
                "related_features": [],
            }],
            "feature_catalog": [],
        },
    )
    run = RunRow(
        id="run-stopped-callback",
        pro_ids=["889901"],
        audience_query="workbench:promotion-stopped",
        audience_run="2026-09-18T18:00:00Z",
        channels=["sms"],
        context_source="staging",
        cost_limit=Decimal("25.00"),
        status="waiting",
    )
    job = JobRow(
        id="job-stopped-callback",
        run_id=run.id,
        stage="pro",
        pro_id="889901",
        status="waiting",
        checkpoint={"staging_request": {"promotion_id": promotion.id}},
    )
    db_session.add_all([promotion, run])
    await db_session.flush()
    db_session.add(job)
    await db_session.commit()

    fetch_started = asyncio.Event()
    finish_fetch = asyncio.Event()

    async def fake_context_layer(self, organization_id, base_url, api_key):
        assert organization_id == "cc962bf1-13bb-4eea-bf66-f3adc9e22192"
        fetch_started.set()
        await finish_fetch.wait()
        return {"firmographics": {"segment": "1A", "industry": "HVAC"}}

    monkeypatch.setattr("waypoint.api.ContextLayerClient.fetch", fake_context_layer)
    callback = asyncio.create_task(
        client.post(
            "/api/context/staging/callback",
            json={
                "request_id": job.id,
                "organization_id": "889901",
                "promotion_id": promotion.id,
                "rows": [
                    {
                        "VARIABLE_NAME": "ORG_SNAPSHOT",
                        "VALUE": {"ORG_UUID": "cc962bf1-13bb-4eea-bf66-f3adc9e22192"},
                    },
                    {
                        "VARIABLE_NAME": "SAFE_SIGNAL",
                        "VALUE": 7,
                        "METADATA": {"source_table": "ANALYTICS.SIGNALS"},
                    },
                ],
            },
            headers={"authorization": "Bearer test"},
        )
    )
    await fetch_started.wait()
    run.status = "stopped"
    job.status = "stopped"
    await db_session.commit()
    finish_fetch.set()
    response = await callback

    await db_session.refresh(job)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert job.status == "stopped"
    assert "staging_context" not in job.checkpoint


async def test_staging_callback_requires_the_n8n_token(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/context/staging/callback",
        json={
            "request_id": "job",
            "organization_id": "889901",
            "promotion_id": "promotion",
            "rows": [],
        },
        headers={"authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


async def test_workbench_snowflake_callback_resumes_the_existing_job(
    db_session_factory,
    monkeypatch,
) -> None:
    dispatched: dict[str, object] = {}
    resumed: dict[str, object] = {}
    context_layer_identifiers: list[str] = []

    async def fake_start(
        self, organization_id, webhook_url, token, *, request_id, promotion_id,
        callback_mode="staging",
    ):
        dispatched.update({
            "organization_id": organization_id,
            "request_id": request_id,
            "promotion_id": promotion_id,
            "callback_mode": callback_mode,
        })
        return {"status": "accepted", "request_id": request_id}

    async def fake_execute(body, *, resume_state=None, checkpoint=None):
        resumed.update(dict(resume_state or {}))
        return {
            "stages": [],
            "warnings": [],
            "outputs": {"authoring": {"draft": [], "review_exception_count": 0}},
        }

    async def fake_context_layer(self, organization_id, base_url, api_key):
        context_layer_identifiers.append(organization_id)
        return {"firmographics": {"segment": "1A", "industry": "HVAC"}}

    monkeypatch.setattr("waypoint.hosted_workbench.N8NContextClient.start", fake_start)
    monkeypatch.setattr(
        "waypoint.hosted_workbench.ContextLayerClient.fetch", fake_context_layer
    )
    app = create_app(
        settings=TEST_SETTINGS,
        session_factory=db_session_factory,
        workbench_executor=fake_execute,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://operator.test") as client:
        assert (await client.post(
            "/api/auth/login", json={"password": "operator-password"}
        )).status_code == 200
        started = await client.post("/api/context-workbench/jobs", json={
            "identifier": "889901",
            "source_mode": "both",
            "workbench_mode": "authoring",
        })
        assert started.status_code == 202
        job_id = started.json()["id"]
        for _ in range(100):
            job = (await client.get(f"/api/context-workbench/jobs/{job_id}")).json()
            if job["state"].get("phase") == "collecting_sources":
                break
            await asyncio.sleep(0.01)
        assert dispatched == {
            "organization_id": "889901",
            "request_id": job_id,
            "promotion_id": "workbench-authoring",
            "callback_mode": "workbench",
        }

        callback = await client.post(
            "/api/context-workbench/source-callback",
            headers={"authorization": "Bearer test"},
            json={
                "request_id": job_id,
                "organization_id": "889901",
                "promotion_id": "workbench-authoring",
                "callback_mode": "workbench",
                "rows": [
                    {
                        "VARIABLE_NAME": "ORG_SNAPSHOT",
                        "VALUE": {
                            "ORG_UUID": "cc962bf1-13bb-4eea-bf66-f3adc9e22192"
                        },
                    },
                    {"VARIABLE_NAME": "SAFE_SIGNAL", "VALUE": 7},
                    {"VARIABLE_NAME": "EMAIL", "VALUE": "hidden@example.test"},
                ],
            },
        )
        assert callback.status_code == 200
        for _ in range(100):
            job = (await client.get(f"/api/context-workbench/jobs/{job_id}")).json()
            if job["status"] == "completed":
                break
            await asyncio.sleep(0.01)

    assert job["status"] == "completed"
    assert context_layer_identifiers == ["cc962bf1-13bb-4eea-bf66-f3adc9e22192"]
    inventory = resumed["inventory"]
    assert isinstance(inventory, list)
    inventory_keys = {item["key"] for item in inventory}
    assert "SAFE_SIGNAL" in inventory_keys
    assert "context_layer.firmographics.segment" in inventory_keys
    assert "EMAIL" not in inventory_keys


async def test_workbench_evaluation_uses_the_async_scrubbed_source_callback(
    db_session_factory,
    monkeypatch,
) -> None:
    dispatched: dict[str, object] = {}
    resumed: dict[str, object] = {}

    async def fake_start(
        self, organization_id, webhook_url, token, *, request_id, promotion_id,
        callback_mode="staging",
    ):
        dispatched.update({
            "organization_id": organization_id,
            "request_id": request_id,
            "promotion_id": promotion_id,
            "callback_mode": callback_mode,
        })
        return {"status": "accepted", "request_id": request_id}

    async def fake_execute(body, *, resume_state=None, checkpoint=None):
        resumed.update(dict(resume_state or {}))
        return {
            "stages": [],
            "warnings": [],
            "outputs": {"evaluation": {"judge": {"winner": "curated"}}},
        }

    async def fake_context_layer(self, organization_id, base_url, api_key):
        return {"firmographics": {"segment": "1A", "industry": "HVAC"}}

    monkeypatch.setattr("waypoint.hosted_workbench.N8NContextClient.start", fake_start)
    monkeypatch.setattr(
        "waypoint.hosted_workbench.ContextLayerClient.fetch", fake_context_layer
    )
    app = create_app(
        settings=TEST_SETTINGS,
        session_factory=db_session_factory,
        workbench_executor=fake_execute,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://operator.test") as client:
        assert (await client.post(
            "/api/auth/login", json={"password": "operator-password"}
        )).status_code == 200
        started = await client.post("/api/context-workbench/jobs", json={
            "identifier": "889901",
            "source_mode": "both",
            "workbench_mode": "evaluate",
        })
        assert started.status_code == 202
        job_id = started.json()["id"]
        for _ in range(100):
            job = (await client.get(f"/api/context-workbench/jobs/{job_id}")).json()
            if job["state"].get("phase") == "collecting_sources":
                break
            await asyncio.sleep(0.01)

        assert dispatched == {
            "organization_id": "889901",
            "request_id": job_id,
            "promotion_id": "workbench-authoring",
            "callback_mode": "workbench",
        }
        callback = await client.post(
            "/api/context-workbench/source-callback",
            headers={"authorization": "Bearer test"},
            json={
                "request_id": job_id,
                "organization_id": "889901",
                "promotion_id": "workbench-authoring",
                "callback_mode": "workbench",
                "rows": [
                    {
                        "VARIABLE_NAME": "ORG_UUID",
                        "VALUE": "cc962bf1-13bb-4eea-bf66-f3adc9e22192",
                    },
                    {"VARIABLE_NAME": "SAFE_SIGNAL", "VALUE": 7},
                    {"VARIABLE_NAME": "EMAIL", "VALUE": "hidden@example.test"},
                ],
            },
        )
        assert callback.status_code == 200
        for _ in range(100):
            job = (await client.get(f"/api/context-workbench/jobs/{job_id}")).json()
            if job["status"] == "completed":
                break
            await asyncio.sleep(0.01)

    assert job["status"] == "completed"
    sources = resumed["scrubbed_sources"]
    assert isinstance(sources, dict)
    snowflake_rows = sources["snowflake"]["rows"]
    assert any(row["VARIABLE_NAME"] == "SAFE_SIGNAL" for row in snowflake_rows)
    assert all(row["VARIABLE_NAME"] != "EMAIL" for row in snowflake_rows)
    assert sources["context_layer"]["firmographics"]["segment"] == "1A"


async def test_staging_rejects_a_promotion_that_changed_after_display(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    db_session.add_all(_ready_staging_rows())
    await db_session.commit()

    response = await auth_client.post(
        "/api/runs",
        json={
            **RUN_REQUEST,
            "pro_ids": ["889901"],
            "context_source": "staging",
            "context_promotion_id": "promotion-previously-displayed",
        },
    )

    assert response.status_code == 409
    assert "changed" in response.text.casefold()
    assert (await db_session.execute(select(RunRow))).scalars().all() == []


async def test_fleet_settings_requires_session(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/fleet/settings")).status_code == 401


async def test_hosted_workbench_routes_require_the_waypoint_session(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get("/api/context-workbench/status")).status_code == 401
    assert (await client.post(
        "/api/context-workbench/jobs",
        json={"identifier": "889901", "workbench_mode": "compile"},
    )).status_code == 401


async def test_hosted_workbench_catalog_versions_are_shared_and_immutable(
    auth_client: httpx.AsyncClient,
) -> None:
    version = {
        "id": "context-shared",
        "kind": "context",
        "name": "Shared context",
        "entries": [{
            "key": "JOBS_CREATED_T28",
            "canonical_key": "jobs_created_t28",
            "disposition": "include",
        }],
        "details": {"feature_catalog_version_id": "features-shared"},
    }

    created = await auth_client.post("/api/context-workbench/catalogs", json=version)
    listed = await auth_client.get("/api/context-workbench/catalogs?kind=context")
    loaded = await auth_client.get("/api/context-workbench/catalogs/context-shared")
    conflict = await auth_client.post(
        "/api/context-workbench/catalogs", json={**version, "name": "Changed"}
    )

    assert created.status_code == 201
    assert listed.json()[0]["id"] == "context-shared"
    assert loaded.json()["entries"][0]["key"] == "JOBS_CREATED_T28"
    assert conflict.status_code == 409


async def test_catalog_metadata_is_scrubbed_before_persistence(
    auth_client: httpx.AsyncClient,
) -> None:
    response = await auth_client.post("/api/context-workbench/catalogs", json={
        "id": "context-safe-metadata",
        "kind": "context",
        "name": "pro@example.com",
        "entries": [],
        "details": {"prompt": "Contact pro@example.com", "confidence_threshold": 0.8},
    })

    assert response.status_code == 201
    assert response.json()["name"] == "context catalog"
    assert response.json()["details"] == {"confidence_threshold": 0.8}


async def test_reuploading_the_same_feature_catalog_is_idempotent(
    auth_client: httpx.AsyncClient,
) -> None:
    csv_text = "feature,description,customer_email\njobs,Manage jobs,pro@example.com\n"

    first = await auth_client.post(
        "/api/context-workbench/catalog/validate",
        json={"name": "September features", "filename": "features.csv", "csv_text": csv_text},
    )
    repeated = await auth_client.post(
        "/api/context-workbench/catalog/validate",
        json={
            "name": "Renamed upload",
            "filename": "renamed.csv",
            "csv_text": csv_text.replace("pro@example.com", "other@example.com"),
        },
    )

    assert first.status_code == 200
    assert first.json()["entries"] == [{"feature": "jobs", "description": "Manage jobs"}]
    assert repeated.status_code == 200
    assert repeated.json()["id"] == first.json()["id"]


async def test_orphaned_promotion_does_not_make_staging_available(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    db_session.add(ContextPromotionRow(
        id="promotion-orphaned",
        active=True,
        bundle={
            "context_catalog_version_id": "missing-context",
            "feature_catalog_version_id": "missing-features",
            "rules": [],
        },
    ))
    await db_session.commit()

    body = (await auth_client.get("/api/fleet/settings")).json()

    assert body["staging_context_available"] is False
    assert body["staging_context"] is None


async def test_fleet_settings_identifies_the_active_staging_catalog(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    db_session.add_all([
        WorkbenchCatalogVersionRow(
            id="context-shared", kind="context", name="Shared context",
            entries=[], details={"feature_catalog_version_id": "features-shared"},
        ),
        WorkbenchCatalogVersionRow(
            id="features-shared", kind="feature", name="September features",
            entries=[], details={},
        ),
        ContextPromotionRow(
            id="promotion-shared",
            active=True,
                bundle={
                    "id": "promotion-shared",
                    "created_at": "2026-09-18T16:04:05+00:00",
                    "activated_at": "2026-09-18T16:04:05+00:00",
                "context_catalog_version_id": "context-shared",
                "feature_catalog_version_id": "features-shared",
                "rules": [{"canonical_key": "jobs_created_t28"}],
            },
            activated_at=datetime(2026, 9, 18, 16, 4, 5, tzinfo=UTC),
        ),
    ])
    await db_session.commit()

    body = (await auth_client.get("/api/fleet/settings")).json()

    assert body["staging_context_available"] is True
    assert body["staging_context"] == {
        "promotion_id": "promotion-shared",
        "context_catalog_version_id": "context-shared",
        "context_catalog_name": "Shared context",
        "feature_catalog_version_id": "features-shared",
        "feature_catalog_name": "September features",
        "included_variables": 1,
        "created_at": "2026-09-18T16:04:05+00:00",
    }


async def test_recovered_catalogs_keep_an_existing_promotion_available(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    rows = _ready_staging_rows()
    context = rows[0]
    feature = rows[1]
    assert isinstance(context, WorkbenchCatalogVersionRow)
    assert isinstance(feature, WorkbenchCatalogVersionRow)
    context.details = {
        "feature_catalog_version_id": "features-ready",
        "recovered_metadata": True,
    }
    feature.details = {"recovered_metadata": True}
    db_session.add_all(rows)
    await db_session.commit()

    response = await auth_client.get("/api/fleet/settings")

    assert response.status_code == 200
    assert response.json()["staging_context_available"] is True
    assert response.json()["staging_context"]["promotion_id"] == "promotion-ready"


async def test_active_waypoint_run_locks_the_hosted_workbench(
    auth_client: httpx.AsyncClient,
) -> None:
    await auth_client.post("/api/runs", json=RUN_REQUEST)

    status = await auth_client.get("/api/context-workbench/status")
    blocked = await auth_client.post(
        "/api/context-workbench/jobs",
        json={"identifier": "889901", "workbench_mode": "compile"},
    )

    assert status.status_code == 200
    assert status.json()["activity"] == "waypoint"
    assert blocked.status_code == 409
    assert "Waypoint run is active" in blocked.text


async def test_degraded_waypoint_run_does_not_lock_the_hosted_workbench(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    created = (await auth_client.post("/api/runs", json=RUN_REQUEST)).json()
    run = await db_session.get(RunRow, created["id"])
    assert run is not None
    run.status = "degraded"
    await db_session.commit()

    status = await auth_client.get("/api/context-workbench/status")

    assert status.status_code == 200
    assert status.json()["activity"] == "idle"


async def test_active_workbench_blocks_login_and_waypoint_run_creation(
    client: httpx.AsyncClient,
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    db_session.add(
        WorkbenchJobRow(
            id="workbench-active",
            status="running",
            request={"identifier": "889901", "workbench_mode": "authoring"},
        )
    )
    await db_session.commit()

    login = await client.post(
        "/api/auth/login", json={"password": "operator-password"}
    )
    run = await auth_client.post("/api/runs", json=RUN_REQUEST)

    assert login.status_code == 409
    assert "Context Workbench is running" in login.text
    assert run.status_code == 409
    assert "Context Workbench is running" in run.text


async def test_hosted_workbench_runs_and_checkpoints_in_postgres(
    db_session_factory,
) -> None:
    checkpoints: list[dict[str, object]] = []

    async def fake_execute(body, *, resume_state=None, checkpoint=None):
        state = {"phase": "drafting", "pending_keys": 2}
        checkpoints.append(state)
        assert checkpoint is not None
        await checkpoint(state)
        return {
            "stages": [],
            "warnings": [],
            "outputs": {
                "authoring": {
                    "draft": [],
                    "review_exception_count": 0,
                }
            },
        }

    app = create_app(
        settings=TEST_SETTINGS,
        session_factory=db_session_factory,
        workbench_executor=fake_execute,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://operator.test"
    ) as client:
        assert (await client.post(
            "/api/auth/login", json={"password": "operator-password"}
        )).status_code == 200
        started = await client.post(
            "/api/context-workbench/jobs",
            json={
                "identifier": "889901",
                "source_mode": "context_layer",
                "workbench_mode": "authoring",
            },
        )
        assert started.status_code == 202
        job_id = started.json()["id"]
        for _ in range(100):
            job = (await client.get(
                f"/api/context-workbench/jobs/{job_id}"
            )).json()
            if job["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("hosted Workbench job did not complete")

    assert checkpoints == [{"phase": "drafting", "pending_keys": 2}]
    assert job["state"] == checkpoints[0]
    assert job["result"]["outputs"]["authoring"]["draft"] == []


async def test_hosted_workbench_activates_an_immutable_promotion(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    entry = {
        "key": "JOBS_CREATED_T28",
        "canonical_key": "jobs_created_t28",
        "value_category": "activity",
        "related_features": ["jobs"],
        "usefulness_rank": 5,
        "disposition": "include",
        "aggregate_prompt": "Calculate the matched cohort percentile.",
        "review_status": "reviewed",
        "approval_status": "auto_approved",
        "confidence": 0.95,
        "uncertainty_reason": None,
        "source_table": "ANALYTICS.JOBS",
    }
    db_session.add_all([
        WorkbenchCatalogVersionRow(
            id="context-v1", kind="context", name="Context v1",
            entries=[entry], details={"feature_catalog_version_id": "features-v1"},
        ),
        WorkbenchCatalogVersionRow(
            id="features-v1", kind="feature", name="Features v1",
            entries=[{
                "feature": "jobs",
                "Product Area": "Operations",
                "Value Statement": "Create and manage jobs.",
            }], details={},
        ),
        WorkbenchJobRow(
        id="evaluation-complete",
        status="completed",
        request={
            "identifier": "889901",
            "workbench_mode": "evaluate",
            "catalog_override": [entry],
            "feature_catalog_entries": [{
                "feature": "jobs",
                "Product Area": "Operations",
                "Value Statement": "Create and manage jobs.",
            }],
            "catalog_version_id": "context-v1",
            "feature_catalog_version_id": "features-v1",
        },
        result={"outputs": {"evaluation": {"judge": {"winner": "curated"}}}},
        ),
    ])
    await db_session.commit()

    response = await auth_client.post(
        "/api/context-workbench/promotions",
        json={"evaluation_job_id": "evaluation-complete"},
    )

    assert response.status_code == 200
    assert response.json()["included_variables"] == 1
    assert response.json()["csv"].splitlines()[0] == (
        "canonical_key,source_table,cohort_aggregate_prompt"
    )
    promotion = await db_session.get(ContextPromotionRow, response.json()["id"])
    assert promotion is not None and promotion.active is True
    activated_at = promotion.activated_at
    assert promotion.bundle["feature_catalog"][0]["feature"] == "jobs"

    repeated = await auth_client.post(
        "/api/context-workbench/promotions",
        json={"evaluation_job_id": "evaluation-complete"},
    )
    await db_session.refresh(promotion)

    assert repeated.status_code == 200
    assert promotion.activated_at == activated_at


async def test_hosted_workbench_rejects_mismatched_feature_lineage(
    auth_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    entry = {
        "key": "JOBS_CREATED_T28",
        "canonical_key": "jobs_created_t28",
        "value_category": "activity",
        "related_features": ["jobs"],
        "usefulness_rank": 5,
        "disposition": "include",
        "aggregate_prompt": "Calculate the matched cohort percentile.",
        "review_status": "reviewed",
        "approval_status": "auto_approved",
        "confidence": 0.95,
        "uncertainty_reason": None,
        "source_table": "ANALYTICS.JOBS",
    }
    features = [{"feature": "jobs", "description": "Manage jobs"}]
    db_session.add_all([
        WorkbenchCatalogVersionRow(
            id="context-v1", kind="context", name="Context v1",
            entries=[entry], details={"feature_catalog_version_id": "features-v1"},
        ),
        WorkbenchCatalogVersionRow(
            id="features-v2", kind="feature", name="Features v2",
            entries=features, details={},
        ),
        WorkbenchJobRow(
            id="evaluation-wrong-features",
            status="completed",
            request={
                "identifier": "889901",
                "workbench_mode": "evaluate",
                "catalog_override": [entry],
                "feature_catalog_entries": features,
                "catalog_version_id": "context-v1",
                "feature_catalog_version_id": "features-v2",
            },
            result={"outputs": {"evaluation": {"judge": {"winner": "curated"}}}},
        ),
    ])
    await db_session.commit()

    response = await auth_client.post(
        "/api/context-workbench/promotions",
        json={"evaluation_job_id": "evaluation-wrong-features"},
    )

    assert response.status_code == 409
    assert "feature catalog" in response.text.casefold()
    assert (await db_session.execute(select(ContextPromotionRow))).scalars().all() == []


async def test_hosted_workbench_cannot_promote_during_a_waypoint_run(
    auth_client: httpx.AsyncClient,
) -> None:
    await auth_client.post("/api/runs", json=RUN_REQUEST)

    response = await auth_client.post(
        "/api/context-workbench/promotions",
        json={"evaluation_job_id": "anything"},
    )

    assert response.status_code == 409
    assert "Waypoint run" in response.text


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
    # A real-Pro send; a guardrailed one is labelled instead (see outcomes.py).
    "routing": "route-to-pro",
    "pro_id": "pro_1",
    "channel": "sms",
    # V3: horizons are derived from a confirmed send + return event, never
    # asserted by the caller.
    "send_status": "confirmed",
    "sent_at": "2026-08-01T12:00:00Z",
    "first_return_at": "2026-08-04T12:00:00Z",
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


async def test_control_exposure_endpoint_registers_without_a_winner(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    response = await auth_client.post("/api/exposures", json=[{
        "exposure_id": "exp-api-ctl", "pro_id": "pro-1", "org_id": "org-1",
        "item_id": "item-1", "item_version": "v1", "arm": "B", "channel": "sms",
    }])
    assert response.status_code == 202
    assert response.json() == {"stored": 1, "unknown_recommendation": 0}


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


async def test_attributed_outcome_via_row_id_alias_backfills_from_winner(
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

    outcome = {k: v for k, v in OUTCOME.items() if k != "recommendation_id"}
    response = await auth_client.post(
        "/api/outcomes",
        json=[{**outcome, "row_id": winner.id}],
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
    await auth_client.post("/api/outcomes", json=[{**OUTCOME, "send_status": "pending"}])
    await auth_client.post("/api/outcomes", json=[OUTCOME])
    rows = (await db_session.execute(select(TouchOutcomeRow))).scalars().all()
    assert len(rows) == 1
    assert rows[0].returned_1d is False  # derived once the send was confirmed
    assert rows[0].returned_7d is True
    assert rows[0].returned_30d is True


async def test_caller_asserted_horizons_are_dropped_at_the_wire(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """A caller cannot POST returned_7d=True into the evidence store."""
    payload = {
        "recommendation_id": "x", "source": "hostile", "returned_7d": True, "arm": "A",
    }
    response = await auth_client.post("/api/outcomes", json=[payload])
    assert response.status_code == 202
    row = (await db_session.execute(select(TouchOutcomeRow))).scalar_one()
    assert row.returned_7d is None
    assert row.arm is None


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


# --- scoped outcomes token --------------------------------------------------
# n8n writes outcomes and nothing else, so it gets a token that can do exactly
# that. APP_PASSWORD is full operator access and n8n persists secrets in
# plaintext execution history — the two must not be the same credential.

BEARER = {"authorization": "Bearer tok-good"}


async def test_outcomes_accepts_the_scoped_token(token_client: httpx.AsyncClient) -> None:
    assert (await token_client.post("/api/outcomes", json=[OUTCOME], headers=BEARER)).status_code == 202


async def test_a_wrong_token_is_401_and_never_falls_back_to_the_cookie(
    token_client: httpx.AsyncClient,
) -> None:
    await token_client.post("/api/auth/login", json={"password": "operator-password"})
    # This client HAS a valid session cookie. Presenting a bad token must still
    # fail: silently downgrading would turn a leaked-token alarm into a success.
    response = await token_client.post(
        "/api/outcomes", json=[OUTCOME], headers={"authorization": "Bearer tok-wrong"}
    )
    assert response.status_code == 401


async def test_the_token_unlocks_nothing_but_outcomes(token_client: httpx.AsyncClient) -> None:
    assert (await token_client.get("/api/fleet/settings", headers=BEARER)).status_code == 401
    assert (await token_client.post("/api/runs", json={}, headers=BEARER)).status_code == 401


async def test_outcomes_still_works_on_the_cookie(auth_client: httpx.AsyncClient) -> None:
    assert (await auth_client.post("/api/outcomes", json=[OUTCOME])).status_code == 202


async def test_a_bearer_header_with_no_token_configured_is_refused(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/outcomes", json=[OUTCOME], headers={"authorization": "Bearer anything"}
    )
    assert response.status_code == 401


async def test_calls_panel_lists_call_winners_and_tracks_done(
    auth_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    created = (await auth_client.post("/api/runs", json=RUN_REQUEST)).json()
    run_id = created["id"]
    winners = []
    for channel in ("call", "sms"):
        candidate = CandidateRow(
            run_id=run_id, pro_id=f"pro_{channel}",
            recommendation={"title": f"T-{channel}", "mechanism": "onboarding_call",
                            "pro_facing_concept": "C", "manager_rationale": "R",
                            "actions": ["a"], "channel": channel},
        )
        db_session.add(candidate)
        await db_session.flush()
        winner = WinnerRow(run_id=run_id, pro_id=f"pro_{channel}", kind="winner",
                           candidate_id=candidate.id, evidence={"org_id": "org_1"})
        db_session.add(winner)
        await db_session.flush()
        winners.append(winner)
    call_winner, sms_winner = winners
    # Mark the call winner's candidate as champion with a final score, and add
    # ranked runner-ups plus a suppressed idea that must never surface.
    champion = await db_session.get(CandidateRow, call_winner.candidate_id)
    assert champion is not None
    champion.status = "champion"
    champion.score = {"final": {"reduction_pp": 5.0}}
    for title, pp, status in (("second", 3.0, "discarded"), ("third", 2.0, "discarded"),
                              ("fourth", 1.0, "discarded"), ("blocked", 9.0, "suppressed")):
        db_session.add(CandidateRow(
            run_id=run_id, pro_id="pro_call", status=status,
            score={"screen": {"reduction_pp": pp}},
            recommendation={"title": title, "mechanism": "m", "pro_facing_concept": "c",
                            "manager_rationale": "r", "actions": ["a"], "channel": "call"},
        ))
    await db_session.commit()

    listed = (await auth_client.get("/api/calls")).json()
    assert [c["winner_id"] for c in listed] == [call_winner.id]
    assert listed[0]["status"] == "todo" and listed[0]["title"] == "T-call"
    assert [a["title"] for a in listed[0]["alternatives"]] == ["second", "third"]
    assert listed[0]["alternatives"][0]["score_pp"] == 3.0

    patched = await auth_client.patch(
        f"/api/calls/{call_winner.id}", json={"status": "done", "note": "left voicemail"}
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "done" and patched.json()["note"] == "left voicemail"
    assert (await auth_client.get("/api/calls")).json()[0]["status"] == "done"
    # An sms winner is not a call: the panel refuses to log it.
    assert (
        await auth_client.patch(f"/api/calls/{sms_winner.id}", json={"status": "done"})
    ).status_code == 404
