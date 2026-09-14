"""Full-funnel counts come from Waypoint's own tables — no Slack, no LCM DB."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.funnel import detail, summary, worklist
from waypoint.models import TouchOutcomeIn
from waypoint.outcomes import ingest
from waypoint.tables import CandidateRow, HandoffRow, RunRow, WinnerRow


async def _run_with_verdicts(session: AsyncSession) -> RunRow:
    """A run of 5 Pros: 2 winners (one shipped and sent, one shipped and QA-dropped),
    1 no_action, 1 abstained, 1 still undecided."""
    run = RunRow(pro_ids=[f"pro_{i}" for i in range(5)], audience_query="q",
                 audience_run="r", channels=["sms"], journey_window="churn_risk")
    session.add(run)
    await session.flush()
    for i, kind in enumerate(["winner", "winner", "no_action", "abstained"]):
        candidate = None
        if kind == "winner":
            candidate = CandidateRow(
                run_id=run.id, pro_id=f"pro_{i}", status="champion",
                recommendation={"title": "t", "mechanism": f"mech_{i}", "actions": ["a"],
                                "pro_facing_concept": f"theme {i}", "manager_rationale": "m",
                                "channel": "sms", "risk": ""},
            )
            session.add(candidate)
            await session.flush()
        winner = WinnerRow(run_id=run.id, pro_id=f"pro_{i}", kind=kind, rationale="m",
                           candidate_id=candidate.id if candidate else None)
        session.add(winner)
        await session.flush()
        if kind == "winner":
            session.add(HandoffRow(run_id=run.id, winner_id=winner.id,
                                   idempotency_key=f"{run.id}:{winner.id}",
                                   payload={}, status="accepted"))
    await session.commit()
    return run


async def test_the_funnel_counts_every_stage(db_session: AsyncSession) -> None:
    run = await _run_with_verdicts(db_session)
    # Only pro_0 earns a send event; pro_1 was handed off but QA dropped it.
    await ingest(db_session, [TouchOutcomeIn(
        run_id=run.id, pro_id="pro_0", source="iterable_n8n", routing="route-to-pro",
        channel="sms", send_status="confirmed",
        sent_at=datetime.now(UTC) - timedelta(days=4),
        first_return_at=datetime.now(UTC) - timedelta(days=1),
    )])

    report = await summary(db_session, days=7)
    row = next(r for r in report["runs"] if r["run_id"] == run.id)
    assert row["audience"] == 5
    assert row["winner"] == 2
    assert row["no_action"] == 1
    assert row["abstained"] == 1
    assert row["undecided"] == 1          # never reached a verdict
    assert row["handed_off"] == 2
    assert row["intake_accepted"] == 2
    assert row["sent"] == 1
    assert row["qa_dropped"] == 1         # handed off, never observed sending
    assert row["returned"]["7d"] == {"measured": 1, "returned": 1}


async def test_a_guardrailed_send_is_not_counted_as_sent(db_session: AsyncSession) -> None:
    # The funnel must agree with the evidence gate: a touch delivered to an
    # internal inbox is not a touch the Pro received.
    run = await _run_with_verdicts(db_session)
    await ingest(db_session, [TouchOutcomeIn(
        run_id=run.id, pro_id="pro_0", source="iterable_n8n", routing="guardrail",
        channel="sms", send_status="confirmed",
        sent_at=datetime.now(UTC) - timedelta(days=4),
        first_return_at=datetime.now(UTC) - timedelta(days=1),
    )])
    row = next(r for r in (await summary(db_session, days=7))["runs"] if r["run_id"] == run.id)
    assert row["sent"] == 0
    assert row["qa_dropped"] == 2
    assert row["returned"] == {}


async def test_detail_carries_the_theme_and_the_natural_key(db_session: AsyncSession) -> None:
    run = await _run_with_verdicts(db_session)
    rows = await detail(db_session, days=7)
    mine = {r["pro_id"]: r for r in rows if r["run_id"] == run.id}
    assert len(mine) == 4
    assert mine["pro_0"]["verdict"] == "winner"
    assert mine["pro_0"]["theme"] == "theme 0"
    assert mine["pro_0"]["mechanism"] == "mech_0"
    assert mine["pro_0"]["intake_status"] == "accepted"
    # abstained Pros carry no theme and were never handed off
    assert mine["pro_3"]["verdict"] == "abstained"
    assert mine["pro_3"]["handed_off"] is False


async def test_a_run_outside_the_window_is_actually_excluded(
    db_session: AsyncSession,
) -> None:
    # The previous version of this test asserted only report["days"] == 1, which
    # a wrong sign, a wrong unit, or a flipped comparison would all have passed.
    # The 90d-horizon work list depends on this window being right.
    run = await _run_with_verdicts(db_session)
    run.created_at = datetime.now(UTC) - timedelta(days=30)
    await db_session.commit()

    assert not [r for r in (await summary(db_session, days=7))["runs"] if r["run_id"] == run.id]
    assert [r for r in (await summary(db_session, days=60))["runs"] if r["run_id"] == run.id]
    assert not [r for r in await detail(db_session, days=7) if r["run_id"] == run.id]
    assert [r for r in await detail(db_session, days=60) if r["run_id"] == run.id]


async def test_funnel_requires_auth(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/funnel")).status_code == 401
    assert (await client.get("/api/funnel/worklist")).status_code == 401
    await client.post("/api/auth/login", json={"password": "operator-password"})
    response = await client.get("/api/funnel")
    assert response.status_code == 200
    assert "totals" in response.json()


@pytest.mark.parametrize("days", [0, 181, -1])
async def test_the_window_is_bounded(auth_client: httpx.AsyncClient, days: int) -> None:
    assert (await auth_client.get(f"/api/funnel?days={days}")).status_code == 422


async def test_totals_survive_a_quiet_window_and_carry_the_horizons(
    db_session: AsyncSession,
) -> None:
    empty = await summary(db_session, days=1)
    # A caller reading totals must not KeyError on a day with no runs.
    assert empty["totals"]["audience"] == 0
    assert empty["totals"]["returned"] == {}

    run = await _run_with_verdicts(db_session)
    await ingest(db_session, [TouchOutcomeIn(
        run_id=run.id, pro_id="pro_0", source="iterable_n8n", routing="route-to-pro",
        channel="sms", send_status="confirmed",
        sent_at=datetime.now(UTC) - timedelta(days=4),
        first_return_at=datetime.now(UTC) - timedelta(days=1),
    )])
    # `returned` is a dict, so an isinstance(int) sweep dropped it from totals —
    # the one number the funnel exists to produce.
    assert (await summary(db_session, days=7))["totals"]["returned"]["7d"] == {
        "measured": 1, "returned": 1
    }


async def test_qa_dropped_counts_only_rows_lcm_accepted(db_session: AsyncSession) -> None:
    # A pending row is written BEFORE the POST to LCM, and a rejected row was
    # never QA'd by a human. Counting either as "QA dropped it" makes an intake
    # bug look like editorial judgement.
    run = RunRow(pro_ids=["pro_0"], audience_query="q", audience_run="r",
                 channels=["sms"], journey_window="churn_risk")
    db_session.add(run)
    await db_session.flush()
    winner = WinnerRow(run_id=run.id, pro_id="pro_0", kind="winner", rationale="m")
    db_session.add(winner)
    await db_session.flush()
    db_session.add(HandoffRow(run_id=run.id, winner_id=winner.id,
                              idempotency_key=f"{run.id}:{winner.id}",
                              payload={}, status="rejected"))
    await db_session.commit()

    row = next(r for r in (await summary(db_session, days=7))["runs"] if r["run_id"] == run.id)
    assert row["handed_off"] == 1
    assert row["intake_rejected"] == 1
    assert row["qa_dropped"] == 0


async def test_an_unexpected_verdict_kind_is_reported_not_swallowed(
    db_session: AsyncSession,
) -> None:
    # WinnerRow.kind is free text. A novel kind used to be subtracted from
    # `undecided` (driving it negative) while appearing under no key at all.
    run = RunRow(pro_ids=["pro_0", "pro_1"], audience_query="q", audience_run="r",
                 channels=["sms"], journey_window="churn_risk")
    db_session.add(run)
    await db_session.flush()
    db_session.add(WinnerRow(run_id=run.id, pro_id="pro_0", kind="winner", rationale="m"))
    db_session.add(WinnerRow(run_id=run.id, pro_id="pro_1", kind="some_new_kind", rationale="m"))
    await db_session.commit()

    row = next(r for r in (await summary(db_session, days=7))["runs"] if r["run_id"] == run.id)
    assert row["some_new_kind"] == 1
    assert row["undecided"] == 0


async def test_the_worklist_carries_no_themes_or_org_ids(db_session: AsyncSession) -> None:
    # n8n persists node output in plaintext execution history, so the automation
    # token must not be able to export the recommendation catalogue.
    run = await _run_with_verdicts(db_session)
    rows = await worklist(db_session, days=7)
    mine = [r for r in rows if r["run_id"] == run.id]
    assert {tuple(sorted(r)) for r in mine} == {("pro_id", "run_id")}
    # Only shipped winners — no_action and abstained Pros were never sent.
    assert {r["pro_id"] for r in mine} == {"pro_0", "pro_1"}


async def test_the_full_funnel_needs_operator_auth_not_the_machine_token(
    token_client: httpx.AsyncClient,
) -> None:
    bearer = {"authorization": "Bearer tok-good"}
    assert (await token_client.get("/api/funnel", headers=bearer)).status_code == 401
    assert (await token_client.get("/api/funnel?detail=true", headers=bearer)).status_code == 401
    assert (await token_client.get("/api/funnel/worklist", headers=bearer)).status_code == 200
    # ...and an operator cookie still opens the rich view.
    await token_client.post("/api/auth/login", json={"password": "operator-password"})
    assert (await token_client.get("/api/funnel?detail=true")).status_code == 200
