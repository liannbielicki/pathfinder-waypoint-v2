import json
from decimal import Decimal

import pytest
from pytest_httpx import HTTPXMock
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.handoff import HandoffUnavailable, LCMClient, handoff_key
from waypoint.models import MeasurementIndicator, MeasurementPlan
from waypoint.tables import HandoffRow, RunRow, WinnerRow

LCM_URL = "https://lcm.example/handoff"

PLAN = MeasurementPlan(indicators=[MeasurementIndicator(
    key="invoices_sent", label="Invoices sent", direction="increase",
    source="billing", window_days=30, rationale="The proposal sends invoices.",
)])

WINNER = {
    "run_id": "run-1",
    "winner_id": "win-1",
    "pro_id": "pro_1",
    "org_id": "org_1",
    "recommendation": {"title": "Send open invoices reminder",
                       "mechanism": "invoice_delivery"},
    "score": {"reduction_pp": 4.2, "ci_lower_pp": 3.1, "ci_upper_pp": 5.3},
}

LINEAGE = {"audience_query": "audience_v7", "audience_run": "2026-08-06T18:00:00Z"}


async def handoff_count(session: AsyncSession, key: str) -> int:
    return (await session.execute(
        select(func.count()).select_from(HandoffRow)
        .where(HandoffRow.idempotency_key == key)
    )).scalar_one()


@pytest.fixture
async def seeded_run(db_session: AsyncSession) -> None:
    db_session.add(RunRow(
        id="run-1", pro_ids=["pro_1"], audience_query="audience_v7",
        audience_run="2026-08-06T18:00:00Z", channels=["sms"],
        cost_limit=Decimal("100.00"),
    ))
    db_session.add(WinnerRow(id="win-1", run_id="run-1", pro_id="pro_1", kind="winner"))
    await db_session.commit()


def make_client(db_session: AsyncSession) -> LCMClient:
    return LCMClient(url=LCM_URL, token="lcm-token", session=db_session)


def test_handoff_key_is_deterministic() -> None:
    assert handoff_key("run_1", "winner_1") == "run_1:winner_1"


async def test_retry_returns_one_durable_receipt(
    httpx_mock: HTTPXMock, db_session: AsyncSession, seeded_run: None,
) -> None:
    httpx_mock.add_response(json={"status": "accepted", "lcm_id": "lcm-123"})
    client = make_client(db_session)
    first = await client.handoff(WINNER, PLAN, LINEAGE)
    second = await client.handoff(WINNER, PLAN, LINEAGE)
    assert second.idempotency_key == first.idempotency_key
    assert len(httpx_mock.get_requests()) == 1
    assert await handoff_count(db_session, first.idempotency_key) == 1


async def test_payload_stops_before_send_and_preserves_lineage(
    httpx_mock: HTTPXMock, db_session: AsyncSession, seeded_run: None,
) -> None:
    httpx_mock.add_response(json={"status": "accepted"})
    await make_client(db_session).handoff(WINNER, PLAN, LINEAGE)
    request = httpx_mock.get_request()
    assert request is not None
    payload = json.loads(request.content)
    assert payload["audience_lineage"] == LINEAGE
    assert payload["pro_id"] == "pro_1"
    assert payload["org_id"] == "org_1"
    assert payload["measurement_plan"]["indicators"][0]["key"] == "invoices_sent"
    assert payload["idempotency_key"] == "run-1:win-1"
    # Pathfinder never sends: no send/dispatch/schedule command crosses this boundary.
    assert not any("send" in key or "dispatch" in key for key in payload)
    assert request.headers["authorization"] == "Bearer lcm-token"


async def test_rejected_handoff_is_recorded_honestly(
    httpx_mock: HTTPXMock, db_session: AsyncSession, seeded_run: None,
) -> None:
    httpx_mock.add_response(status_code=422, json={"error": "bad category"})
    receipt = await make_client(db_session).handoff(WINNER, PLAN, LINEAGE)
    assert receipt.status == "rejected"
    row = (await db_session.execute(
        select(HandoffRow).where(HandoffRow.idempotency_key == receipt.idempotency_key)
    )).scalar_one()
    assert row.status == "rejected"
    assert row.response == {"error": "bad category"}


async def test_lcm_outage_raises_and_recovers_idempotently(
    httpx_mock: HTTPXMock, db_session: AsyncSession, seeded_run: None,
) -> None:
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("boom"))
    client = make_client(db_session)
    with pytest.raises(HandoffUnavailable):
        await client.handoff(WINNER, PLAN, LINEAGE)
    # Recovery: the pending row is completed by one successful retry.
    httpx_mock.add_response(json={"status": "accepted"})
    receipt = await client.handoff(WINNER, PLAN, LINEAGE)
    assert receipt.status == "accepted"
    assert await handoff_count(db_session, receipt.idempotency_key) == 1
