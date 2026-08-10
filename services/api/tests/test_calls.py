"""Recorded paid-call lifecycle + fleet-wide in-flight limiter."""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.calls import (
    MAX_IN_FLIGHT_LLM_CALLS,
    BudgetExhausted,
    FleetSlots,
    MeteredLLM,
    RecordedCalls,
)
from waypoint.llm import LLMResult, Pricing
from waypoint.tables import LlmCallRow

PRICING = Pricing(
    models={"fast": "model-fast", "deep": "model-deep"},
    usd_per_mtok={
        "model-fast": (Decimal(10), Decimal(20)),
        "model-deep": (Decimal(30), Decimal(60)),
    },
)


class FakeGateway:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    async def complete(
        self, tier, prompt, run_id, stage, system=None, max_tokens=1200, temperature=None
    ) -> LLMResult:
        self.calls.append({"tier": tier, "stage": stage, "temperature": temperature})
        if self.fail:
            raise RuntimeError("injected provider outage")
        return LLMResult(
            text="provider says hi",
            model="model-fast",
            input_tokens=100,
            output_tokens=20,
            cost_usd=Decimal("0.0014"),
            request_id="req_1",
            usage_id="usage_1",
        )


class Ledger:
    """Spy reserve/reconcile pair."""

    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.reserved: list[Decimal] = []
        self.reconciled: list[tuple[Decimal, Decimal]] = []

    async def reserve(self, run_id: str, amount: Decimal) -> bool:
        if self.allow:
            self.reserved.append(amount)
        return self.allow

    async def reconcile(self, run_id: str, reserved: Decimal, actual: Decimal) -> None:
        self.reconciled.append((reserved, actual))


def metered(session: AsyncSession, gateway: FakeGateway, ledger: Ledger) -> MeteredLLM:
    return MeteredLLM(
        gateway=gateway,
        records=RecordedCalls(session),
        slots=None,
        pricing=PRICING,
        reserve=ledger.reserve,
        reconcile=ledger.reconcile,
    )


KEY = "run-1:pro_1:r1:generate"


async def call(m: MeteredLLM, key: str = KEY) -> LLMResult:
    return await m.complete(
        call_key=key,
        tier="fast",
        prompt="one idea please",
        run_id="run-1",
        pro_id="pro_1",
        stage="evolve",
    )


async def row_for(session: AsyncSession, key: str) -> LlmCallRow | None:
    return (
        await session.execute(
            select(LlmCallRow)
            .where(LlmCallRow.call_key == key)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def test_success_walks_the_full_lifecycle(db_session: AsyncSession) -> None:
    gateway, ledger = FakeGateway(), Ledger()
    result = await call(metered(db_session, gateway, ledger))
    assert result.text == "provider says hi"
    row = await row_for(db_session, KEY)
    assert row is not None
    assert row.status == "reconciled"
    assert row.actual_usd == Decimal("0.0014")
    assert row.provider_request_id == "req_1"
    assert row.usage_id == "usage_1"
    assert ledger.reserved and ledger.reserved[0] > Decimal("0.0014")  # worst case
    assert ledger.reconciled == [(ledger.reserved[0], Decimal("0.0014"))]


async def test_duplicate_key_returns_stored_result_with_zero_spend(
    db_session: AsyncSession,
) -> None:
    gateway, ledger = FakeGateway(), Ledger()
    m = metered(db_session, gateway, ledger)
    await call(m)
    again = await call(m)
    assert again.text == "provider says hi"
    assert len(gateway.calls) == 1
    assert len(ledger.reserved) == 1
    assert len(ledger.reconciled) == 1


async def test_budget_refusal_makes_no_call_and_no_row(db_session: AsyncSession) -> None:
    gateway, ledger = FakeGateway(), Ledger(allow=False)
    with pytest.raises(BudgetExhausted):
        await call(metered(db_session, gateway, ledger))
    assert gateway.calls == []
    assert await row_for(db_session, KEY) is None


async def test_provider_failure_leaves_pending_then_abandon_stale(
    db_session: AsyncSession,
) -> None:
    gateway, ledger = FakeGateway(fail=True), Ledger()
    m = metered(db_session, gateway, ledger)
    with pytest.raises(RuntimeError):
        await call(m)
    row = await row_for(db_session, KEY)
    assert row is not None and row.status == "pending"
    assert ledger.reconciled == []  # the worst-case reservation stays
    abandoned = await RecordedCalls(db_session).abandon_stale("run-1", "pro_1")
    assert [r.call_key for r in abandoned] == [KEY]
    assert (await row_for(db_session, KEY)).status == "abandoned"


async def test_commit_result_never_resurrects_an_abandoned_row(
    db_session: AsyncSession,
) -> None:
    records = RecordedCalls(db_session)
    row = await records.begin(KEY, "run-1", "pro_1", "evolve", "model-fast", Decimal(1))
    await records.abandon_stale("run-1", "pro_1")
    committed = await records.commit_result(row, "late text", "u1", Decimal("0.1"), "req")
    assert committed is False
    assert (await row_for(db_session, KEY)).status == "abandoned"


async def test_crash_between_committed_and_reconciled_finishes_only_reconcile(
    db_session: AsyncSession,
) -> None:
    gateway, ledger = FakeGateway(), Ledger()
    records = RecordedCalls(db_session)
    row = await records.begin(KEY, "run-1", "pro_1", "evolve", "model-fast", Decimal(2))
    await records.commit_result(row, "stored text", "u1", Decimal("0.5"), "req")
    # resume: complete() with the same key must not re-call the provider
    result = await call(metered(db_session, gateway, ledger))
    assert result.text == "stored text"
    assert gateway.calls == []
    assert ledger.reserved == []
    assert ledger.reconciled == [(Decimal(2), Decimal("0.5"))]
    assert (await row_for(db_session, KEY)).status == "reconciled"


async def test_limiter_blocks_the_fifth_call_until_a_release(db_engine) -> None:
    connections = [await db_engine.connect() for _ in range(MAX_IN_FLIGHT_LLM_CALLS + 1)]
    try:
        holders = [FleetSlots(c, poll_seconds=0.05) for c in connections]
        slots = [await holders[i].acquire() for i in range(MAX_IN_FLIGHT_LLM_CALLS)]
        assert sorted(slots) == list(range(MAX_IN_FLIGHT_LLM_CALLS))
        fifth = asyncio.create_task(holders[MAX_IN_FLIGHT_LLM_CALLS].acquire())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(fifth), timeout=0.3)
        await holders[0].release(slots[0])
        got = await asyncio.wait_for(fifth, timeout=2.0)
        assert got == slots[0]
    finally:
        for connection in connections:
            await connection.close()


async def test_closing_a_holders_connection_frees_its_slot(db_engine) -> None:
    first = await db_engine.connect()
    slot = await FleetSlots(first, poll_seconds=0.05).acquire()
    await first.close()  # crash stand-in: the lock dies with the connection
    second = await db_engine.connect()
    try:
        got = await asyncio.wait_for(FleetSlots(second, poll_seconds=0.05).acquire(), timeout=2.0)
        assert got == slot
    finally:
        await second.close()
