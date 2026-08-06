import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from waypoint.queue import (
    checkpoint_job,
    claim_job,
    enqueue,
    fail_stale_jobs,
    fleet_is_killed,
    heartbeat_job,
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


async def test_heartbeat_extends_the_lease_for_the_owner(db_session) -> None:
    await seed_run(db_session)
    job_id = await enqueue(db_session, "run-1", stage="recommend")
    await db_session.commit()
    job = await claim_job(db_session, "worker-a", lease_seconds=60)
    assert job is not None
    old_lease = job.lease_until
    assert await heartbeat_job(db_session, job_id, "worker-a", lease_seconds=600) is True
    await db_session.refresh(job)
    assert job.lease_until is not None and old_lease is not None
    assert job.lease_until > old_lease


async def test_heartbeat_fails_when_ownership_was_lost(db_session) -> None:
    await seed_run(db_session)
    job_id = await enqueue(db_session, "run-1", stage="recommend")
    await db_session.commit()
    job = await claim_job(db_session, "worker-a")
    assert job is not None
    job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    reclaimed = await claim_job(db_session, "worker-b")
    assert reclaimed is not None
    # The original worker must notice it no longer owns the job.
    assert await heartbeat_job(db_session, job_id, "worker-a", lease_seconds=600) is False


async def test_day_budget_rolls_over_to_a_new_day(db_session) -> None:
    db_session.add(RunRow(
        id="run-1", pro_ids=["pro_1"], audience_query="q", audience_run="r",
        channels=["email"], cost_limit=Decimal("100.00"),
    ))
    db_session.add(FleetControlRow(
        id=1, day="2020-01-01", day_cost_limit=Decimal("1.00"),
        day_cost_reserved=Decimal("1.00"),
    ))
    await db_session.commit()
    # Yesterday's exhausted budget must not brick today.
    assert await reserve_cost(db_session, "run-1", Decimal("0.75")) is True
    fleet = await db_session.get(FleetControlRow, 1)
    await db_session.refresh(fleet)
    assert fleet.day_cost_reserved == Decimal("0.75")
    assert fleet.day != "2020-01-01"
    # And the fresh day still enforces its own limit.
    assert await reserve_cost(db_session, "run-1", Decimal("0.26")) is False


async def test_reservation_refuses_a_stopped_run(db_session) -> None:
    await seed_run(db_session)
    run = await db_session.get(RunRow, "run-1")
    assert run is not None
    run.status = "stopped"
    await db_session.commit()
    assert await reserve_cost(db_session, "run-1", Decimal("0.01")) is False


async def test_attempts_exhausted_jobs_are_reaped_as_failed(db_session) -> None:
    await seed_run(db_session)
    job_id = await enqueue(db_session, "run-1", stage="recommend")
    await db_session.commit()
    job = await db_session.get(JobRow, job_id)
    assert job is not None
    job.status = "running"
    job.attempts = job.max_attempts
    job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    assert await claim_job(db_session, "worker-a") is None  # unclaimable
    reaped = await fail_stale_jobs(db_session)
    await db_session.commit()
    assert reaped == 1
    await db_session.refresh(job)
    assert job.status == "failed"
    run = await db_session.get(RunRow, "run-1")
    assert run is not None
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.stop_reason == "attempts_exhausted"


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
