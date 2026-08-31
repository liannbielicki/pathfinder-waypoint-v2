from decimal import Decimal

import httpx
from pytest_httpx import HTTPXMock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import TEST_SETTINGS
from waypoint.api import create_app
from waypoint.tables import (
    CandidateRow,
    EvolveRoundRow,
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
        "pro_uuid": "pro_1", "theme": "C", "theme_category": "invoice_delivery",
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
