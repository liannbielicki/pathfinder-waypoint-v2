"""Durable worker queue: leased claims, fleet kill, atomic cost reservation.

All state lives in Postgres. Every operation is safe under concurrent workers:
claims use FOR UPDATE SKIP LOCKED, reservations are guarded single UPDATEs, and
a failed reservation leaves both the run and fleet ledgers untouched.
"""

import json
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.tables import FleetControlRow, JobRow

CLAIM_SQL = text("""
WITH next_job AS (
  SELECT id FROM jobs
  WHERE (status = 'queued' OR (status = 'running' AND lease_until < now()))
    AND attempts < max_attempts
    AND NOT EXISTS (SELECT 1 FROM fleet_control WHERE id = 1 AND killed)
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE jobs
SET status = 'running', worker_id = :worker_id,
    lease_until = now() + make_interval(secs => :lease_seconds),
    attempts = attempts + 1
WHERE id IN (SELECT id FROM next_job)
RETURNING id
""")

RESERVE_RUN_SQL = text("""
UPDATE runs SET cost_reserved = cost_reserved + :amount
WHERE id = :run_id
  AND cost_reserved + :amount <= cost_limit
  AND NOT EXISTS (SELECT 1 FROM fleet_control WHERE id = 1 AND killed)
RETURNING id
""")

RESERVE_DAY_SQL = text("""
UPDATE fleet_control SET day_cost_reserved = day_cost_reserved + :amount
WHERE id = 1 AND NOT killed
  AND day_cost_reserved + :amount <= day_cost_limit
RETURNING id
""")

CHECKPOINT_SQL = text("""
UPDATE jobs
SET checkpoint = checkpoint || jsonb_build_object(CAST(:stage AS text), CAST(:payload AS jsonb))
WHERE id = :job_id
""")


async def enqueue(session: AsyncSession, run_id: str, stage: str) -> str:
    job = JobRow(run_id=run_id, stage=stage)
    session.add(job)
    await session.flush()
    return job.id


async def claim_job(
    session: AsyncSession, worker_id: str, lease_seconds: int = 120
) -> JobRow | None:
    claimed = (
        await session.execute(
            CLAIM_SQL, {"worker_id": worker_id, "lease_seconds": lease_seconds}
        )
    ).first()
    if claimed is None:
        return None
    return (
        await session.execute(
            select(JobRow)
            .where(JobRow.id == claimed.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def checkpoint_job(
    session: AsyncSession, job_id: str, stage: str, payload: dict[str, Any]
) -> None:
    await session.execute(
        CHECKPOINT_SQL, {"job_id": job_id, "stage": stage, "payload": json.dumps(payload)}
    )


async def reserve_cost(session: AsyncSession, run_id: str, amount: Decimal) -> bool:
    """Atomically reserve against both the run and the fleet day budget.

    Reserves nothing at all unless both limits allow the amount.
    """
    savepoint = await session.begin_nested()
    run_ok = (
        await session.execute(RESERVE_RUN_SQL, {"run_id": run_id, "amount": amount})
    ).first()
    if run_ok is None:
        await savepoint.rollback()
        return False
    day_ok = (await session.execute(RESERVE_DAY_SQL, {"amount": amount})).first()
    if day_ok is None:
        await savepoint.rollback()
        return False
    await savepoint.commit()
    return True


async def fleet_is_killed(session: AsyncSession) -> bool:
    fleet = await session.get(FleetControlRow, 1)
    return fleet is not None and fleet.killed


async def set_kill(session: AsyncSession, killed: bool) -> None:
    fleet = await session.get(FleetControlRow, 1)
    if fleet is None:
        fleet = FleetControlRow(id=1, killed=killed)
        session.add(fleet)
    else:
        fleet.killed = killed
    await session.flush()
