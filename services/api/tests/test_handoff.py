import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from pytest_httpx import HTTPXMock
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.handoff import (
    AudienceLineageUnresolved,
    HandoffUnavailable,
    LCMClient,
    handoff_key,
    push_ready_winners,
    ready_rows,
)
from waypoint.models import PENDING_AUDIENCE_QUERY
from waypoint.tables import CandidateRow, HandoffRow, MeasurementRow, RunRow, WinnerRow

LCM_URL = "https://lcm.example/handoff"

ROW = {
    "pro_uuid": "pro_fb73ab6510404409bafe684fbc6564fc",
    "theme": "Human-assist outreach for an HVAC owner-operator stalled on overdue AR",
    "theme_category": "other",
    "org_id": "882486",
    "row_id": "win-1",
}


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
    return LCMClient(url=LCM_URL, token="lcm-token", bypass_token="bypass-secret",
                      session=db_session)


def test_handoff_key_is_deterministic() -> None:
    assert handoff_key("run_1", "win_1") == "run_1:win_1"


async def test_retry_returns_one_durable_receipt(
    httpx_mock: HTTPXMock, db_session: AsyncSession, seeded_run: None,
) -> None:
    httpx_mock.add_response(json={
        "batch": "run-1", "rows": [{"row_id": "win-1", "status": "accepted"}],
    })
    client = make_client(db_session)
    first = await client.handoff("run-1", [ROW])
    second = await client.handoff("run-1", [ROW])
    assert second[0].idempotency_key == first[0].idempotency_key
    assert len(httpx_mock.get_requests()) == 1
    assert await handoff_count(db_session, first[0].idempotency_key) == 1


async def test_payload_is_one_batch_with_no_pii(
    httpx_mock: HTTPXMock, db_session: AsyncSession, seeded_run: None,
) -> None:
    httpx_mock.add_response(json={
        "batch": "run-1", "rows": [{"row_id": "win-1", "status": "accepted"}],
    })
    await make_client(db_session).handoff("run-1", [ROW])
    request = httpx_mock.get_request()
    assert request is not None
    payload = json.loads(request.content)
    assert payload["batch"] == "run-1"
    assert payload["rows"] == [ROW]
    # No email/name crosses this boundary — pro_uuid only.
    assert not any("email" in key or "name" in key for key in ROW)
    assert request.headers["authorization"] == "Bearer lcm-token"
    assert request.headers["x-vercel-protection-bypass"] == "bypass-secret"


async def test_rejected_row_is_recorded_honestly(
    httpx_mock: HTTPXMock, db_session: AsyncSession, seeded_run: None,
) -> None:
    httpx_mock.add_response(json={
        "batch": "run-1",
        "rows": [{"row_id": "win-1", "status": "rejected", "reason": "bad category"}],
    })
    receipts = await make_client(db_session).handoff("run-1", [ROW])
    assert receipts[0].status == "rejected"
    row = (await db_session.execute(
        select(HandoffRow).where(HandoffRow.idempotency_key == receipts[0].idempotency_key)
    )).scalar_one()
    assert row.status == "rejected"
    assert row.response == {"row_id": "win-1", "status": "rejected", "reason": "bad category"}


async def test_handoff_rows_carry_attribution_row_id(
    httpx_mock: HTTPXMock, db_session: AsyncSession, seeded_run: None,
) -> None:
    httpx_mock.add_response(json={
        "batch": "run-1", "rows": [{"row_id": "win-1", "status": "accepted"}],
    })
    await make_client(db_session).handoff("run-1", [ROW])
    request = httpx_mock.get_request()
    assert request is not None
    payload = json.loads(request.content)
    for row in payload["rows"]:
        assert row["row_id"] == ROW["row_id"] == "win-1"


async def test_lcm_outage_raises_and_recovers_idempotently(
    httpx_mock: HTTPXMock, db_session: AsyncSession, seeded_run: None,
) -> None:
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("boom"))
    client = make_client(db_session)
    with pytest.raises(HandoffUnavailable):
        await client.handoff("run-1", [ROW])
    # Recovery: the pending row is completed by one successful retry.
    httpx_mock.add_response(json={
        "batch": "run-1", "rows": [{"row_id": "win-1", "status": "accepted"}],
    })
    receipts = await client.handoff("run-1", [ROW])
    assert receipts[0].status == "accepted"
    assert await handoff_count(db_session, receipts[0].idempotency_key) == 1


async def test_batch_level_failure_leaves_row_pending_and_retries(
    httpx_mock: HTTPXMock, db_session: AsyncSession, seeded_run: None,
) -> None:
    httpx_mock.add_response(status_code=500, json={"error": "boom"})
    client = make_client(db_session)
    with pytest.raises(HandoffUnavailable):
        await client.handoff("run-1", [ROW])

    key = handoff_key("run-1", ROW["row_id"])
    row = (await db_session.execute(
        select(HandoffRow).where(HandoffRow.idempotency_key == key)
    )).scalar_one()
    assert row.status == "pending"
    assert row.response is None

    httpx_mock.add_response(json={
        "batch": "run-1", "rows": [{"row_id": "win-1", "status": "accepted"}],
    })
    receipts = await client.handoff("run-1", [ROW])
    assert receipts[0].status == "accepted"
    assert len(httpx_mock.get_requests()) == 2


async def test_incomplete_per_row_response_leaves_row_pending_and_retries(
    httpx_mock: HTTPXMock, db_session: AsyncSession, seeded_run: None,
) -> None:
    httpx_mock.add_response(json={"batch": "run-1", "rows": []})
    client = make_client(db_session)
    with pytest.raises(HandoffUnavailable):
        await client.handoff("run-1", [ROW])

    key = handoff_key("run-1", ROW["row_id"])
    row = (await db_session.execute(
        select(HandoffRow).where(HandoffRow.idempotency_key == key)
    )).scalar_one()
    assert row.status == "pending"
    assert row.response is None

    httpx_mock.add_response(json={
        "batch": "run-1", "rows": [{"row_id": "win-1", "status": "accepted"}],
    })
    receipts = await client.handoff("run-1", [ROW])
    assert receipts[0].status == "accepted"
    assert len(httpx_mock.get_requests()) == 2


# --- trickle push (per-Pro streaming to LCM) --------------------------------

STUB_SETTINGS = SimpleNamespace(
    HANDOFF_URL=LCM_URL,
    HANDOFF_TOKEN=SecretStr("lcm-token"),
    BYPASS_TOKEN=SecretStr("bypass-secret"),
)


async def seed_ready_winner(
    db_session: AsyncSession,
    *,
    audience_query: str,
    status: str = "running",
    winner_evidence: dict | None = None,
) -> None:
    db_session.add(RunRow(
        id="run-t", pro_ids=["pro_1"], audience_query=audience_query, status=status,
        audience_run="2026-08-06T18:00:00Z", channels=["sms"],
        cost_limit=Decimal("100.00"),
    ))
    await db_session.flush()  # runs row lands before FK-dependent rows below
    db_session.add(CandidateRow(
        id="cand-1", run_id="run-t", pro_id="pro_1", status="champion",
        recommendation={"title": "AR nudge", "mechanism": "invoice_delivery"},
    ))
    db_session.add(WinnerRow(
        id="win-t", run_id="run-t", pro_id="pro_1", kind="winner",
        candidate_id="cand-1", evidence=winner_evidence or {"org_id": "882486"},
    ))
    await db_session.flush()  # winners row lands before the measurement's FK
    db_session.add(MeasurementRow(
        id="meas-1", run_id="run-t", winner_id="win-t",
        indicators=[{"metric": "app_return_7d"}],
    ))
    await db_session.commit()


async def test_trickle_push_sends_ready_winner_and_is_idempotent(
    httpx_mock: HTTPXMock, db_session: AsyncSession,
) -> None:
    await seed_ready_winner(db_session, audience_query="audience_v7")
    httpx_mock.add_response(json={
        "batch": "run-t", "rows": [{"row_id": "win-t", "status": "accepted"}],
    })
    assert await push_ready_winners(db_session, STUB_SETTINGS, "run-t", pro_id="pro_1") == 1
    # Second call: the row is already answered, so it is ensured (returns 1)
    # but nothing new goes on the wire — still exactly one POST.
    assert await push_ready_winners(db_session, STUB_SETTINGS, "run-t", pro_id="pro_1") == 1
    assert len(httpx_mock.get_requests()) == 1


async def test_trickle_push_is_scoped_to_the_finished_pro(
    httpx_mock: HTTPXMock, db_session: AsyncSession,
) -> None:
    await seed_ready_winner(db_session, audience_query="audience_v7")
    # A sibling worker finishing a different Pro must not touch pro_1's row.
    assert await push_ready_winners(db_session, STUB_SETTINGS, "run-t", pro_id="pro_2") == 0
    assert len(httpx_mock.get_requests()) == 0


async def test_trickle_push_refuses_stopped_or_failed_run(
    httpx_mock: HTTPXMock, db_session: AsyncSession,
) -> None:
    # Operator kill must keep working: no automatic handoff after a stop.
    await seed_ready_winner(db_session, audience_query="audience_v7", status="stopped")
    assert await push_ready_winners(db_session, STUB_SETTINGS, "run-t", pro_id="pro_1") == 0
    assert len(httpx_mock.get_requests()) == 0


async def test_trickle_push_holds_back_degraded_panel_winners(
    httpx_mock: HTTPXMock, db_session: AsyncSession,
) -> None:
    await seed_ready_winner(
        db_session, audience_query="audience_v7",
        winner_evidence={"org_id": "882486",
                         "panel_disclaimer": {"final": "only 2 of 5 personas qualified"}},
    )
    # Degraded winners wait for the operator's manual POST /handoff...
    assert await push_ready_winners(db_session, STUB_SETTINGS, "run-t", pro_id="pro_1") == 0
    assert len(httpx_mock.get_requests()) == 0
    # ...where they ARE included (include_degraded defaults to True).
    rows = await ready_rows(db_session, "run-t")
    assert [row["row_id"] for row in rows] == ["win-t"]


async def test_trickle_push_refuses_unresolved_audience_lineage(
    httpx_mock: HTTPXMock, db_session: AsyncSession,
) -> None:
    await seed_ready_winner(db_session, audience_query=PENDING_AUDIENCE_QUERY)
    assert await push_ready_winners(db_session, STUB_SETTINGS, "run-t", pro_id="pro_1") == 0
    assert len(httpx_mock.get_requests()) == 0


async def test_ready_rows_raises_on_unresolved_lineage(
    db_session: AsyncSession,
) -> None:
    await seed_ready_winner(db_session, audience_query=PENDING_AUDIENCE_QUERY)
    with pytest.raises(AudienceLineageUnresolved):
        await ready_rows(db_session, "run-t")


async def test_ready_rows_requires_measurement_and_candidate(
    db_session: AsyncSession, seeded_run: None,
) -> None:
    # seeded_run's winner has no candidate/measurement: not ready yet.
    assert await ready_rows(db_session, "run-1") == []


async def test_duplicate_row_id_in_one_call_collapses_to_one_row(
    httpx_mock: HTTPXMock, db_session: AsyncSession, seeded_run: None,
) -> None:
    httpx_mock.add_response(json={
        "batch": "run-1",
        "rows": [{"row_id": "win-1", "status": "accepted"}],
    })
    receipts = await make_client(db_session).handoff("run-1", [ROW, dict(ROW)])

    assert len(receipts) == 2
    assert receipts[0].idempotency_key == receipts[1].idempotency_key
    assert receipts[0].status == receipts[1].status == "accepted"
    assert await handoff_count(db_session, receipts[0].idempotency_key) == 1
