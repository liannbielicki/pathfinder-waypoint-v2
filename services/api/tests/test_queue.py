import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from waypoint.queue import (
    checkpoint_job,
    claim_job,
    enqueue,
    fleet_is_killed,
    reserve_cost,
    set_kill,
)
from waypoint.tables import FleetControlRow, JobRow, RunRow


async def seed_run(session: AsyncSession, run_id: str = "run-1",
                   limit: str = "100.00") -> None:
    session.add(RunRow(
        id=run_id, pro_ids=["pro_1"], audience_query="q", audience_run="r",
        channels=["email"], cost_limit=Decimal(limit),
    ))
    session.add(FleetControlRow(id=1, day_cost_limit=Decimal("1000.00")))
    await session.commit()


async def seed_queued_job(factory: async_sessionmaker[AsyncSession]) -> str:
    async with factory() as session:
        await seed_run(session)
        job_id = await enqueue(session, "run-1", stage="recommend")
        await session.commit()
        return job_id


async def claim_with_new_session(
    factory: async_sessionmaker[AsyncSession], worker_id: str
) -> JobRow | None:
    async with factory() as session:
        job = await claim_job(session, worker_id)
        await session.commit()
        return job


async def test_two_workers_never_claim_the_same_job(db_session_factory) -> None:
    job_id = await seed_queued_job(db_session_factory)
    first, second = await asyncio.gather(
        claim_with_new_session(db_session_factory, "worker-a"),
        claim_with_new_session(db_session_factory, "worker-b"),
    )
    claimed = [job.id for job in (first, second) if job is not None]
    assert claimed == [job_id]


async def test_expired_lease_is_reclaimable(db_session_factory) -> None:
    job_id = await seed_queued_job(db_session_factory)
    async with db_session_factory() as session:
        first = await claim_job(session, "worker-a", lease_seconds=120)
        assert first is not None and first.id == job_id
        # A live lease blocks other workers.
        assert await claim_job(session, "worker-b") is None
        # Simulate lease expiry.
        job = await session.get(JobRow, job_id)
        assert job is not None
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        second = await claim_job(session, "worker-b")
        assert second is not None and second.id == job_id
        assert second.attempts == 2


async def test_kill_switch_blocks_claims(db_session) -> None:
    await seed_run(db_session)
    await enqueue(db_session, "run-1", stage="recommend")
    await set_kill(db_session, True)
    await db_session.commit()
    assert await fleet_is_killed(db_session) is True
    assert await claim_job(db_session, "worker-a") is None


async def test_cost_reservation_never_exceeds_run_limit(db_session) -> None:
    await seed_run(db_session, run_id="run-1", limit="1.00")
    assert await reserve_cost(db_session, "run-1", Decimal("0.75")) is True
    assert await reserve_cost(db_session, "run-1", Decimal("0.26")) is False
    assert await reserve_cost(db_session, "run-1", Decimal("0.25")) is True


async def test_cost_reservation_never_exceeds_day_limit(db_session) -> None:
    db_session.add(RunRow(
        id="run-1", pro_ids=["pro_1"], audience_query="q", audience_run="r",
        channels=["email"], cost_limit=Decimal("100.00"),
    ))
    db_session.add(FleetControlRow(id=1, day_cost_limit=Decimal("1.00")))
    await db_session.commit()
    assert await reserve_cost(db_session, "run-1", Decimal("0.75")) is True
    assert await reserve_cost(db_session, "run-1", Decimal("0.26")) is False


async def test_kill_switch_blocks_cost_reservation(db_session) -> None:
    await seed_run(db_session)
    await set_kill(db_session, True)
    await db_session.commit()
    assert await reserve_cost(db_session, "run-1", Decimal("0.01")) is False


async def test_failed_reservation_reserves_nothing(db_session) -> None:
    await seed_run(db_session, run_id="run-1", limit="1.00")
    assert await reserve_cost(db_session, "run-1", Decimal("2.00")) is False
    await db_session.commit()
    run = await db_session.get(RunRow, "run-1")
    fleet = await db_session.get(FleetControlRow, 1)
    assert run is not None and run.cost_reserved == Decimal(0)
    assert fleet is not None and fleet.day_cost_reserved == Decimal(0)


async def test_checkpoint_persists_stage_payload(db_session) -> None:
    await seed_run(db_session)
    job_id = await enqueue(db_session, "run-1", stage="recommend")
    await db_session.commit()
    await checkpoint_job(db_session, job_id, "context", {"org_count": 1})
    await checkpoint_job(db_session, job_id, "generate", {"candidates": 3})
    await db_session.commit()
    row = (await db_session.execute(
        select(JobRow).where(JobRow.id == job_id)
    )).scalar_one()
    assert row.checkpoint["context"] == {"org_count": 1}
    assert row.checkpoint["generate"] == {"candidates": 3}
