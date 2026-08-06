"""Worker process: claim leased jobs and run the pipeline until stopped.

Run with: python -m waypoint.worker
"""

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

import httpx
from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.db import make_engine, make_session_factory
from waypoint.llm import LLMGateway, Pricing
from waypoint.n8n import N8NContextClient
from waypoint.personas import Persona
from waypoint.pipeline import PipelineDeps, PostgresStore, QueueOps, run_job
from waypoint.queue import claim_job, fail_stale_jobs
from waypoint.scoring import load_calibration
from waypoint.settings import Settings
from waypoint.tables import FleetControlRow

log = logging.getLogger("waypoint.worker")

POLL_SECONDS = 2.0
LEASE_SECONDS = 600
CALIBRATION_PATH = Path(__file__).parents[2] / "data" / "reaction_churn_calibration_cards.json"


async def apply_fleet_settings(session: AsyncSession, settings: Settings) -> None:
    """KILL_SWITCH and DAY_COST_USD are env-owned; apply them on startup."""
    fleet = await session.get(FleetControlRow, 1)
    if fleet is None:
        session.add(FleetControlRow(
            id=1, killed=settings.KILL_SWITCH, day_cost_limit=settings.DAY_COST_USD,
        ))
    else:
        fleet.killed = settings.KILL_SWITCH
        fleet.day_cost_limit = settings.DAY_COST_USD
    await session.commit()


async def load_personas(settings: Settings) -> list[Persona]:
    """Fetch the versioned persona snapshot from the persona service."""
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=False,
        headers={"authorization": f"Bearer {settings.PERSONA_TOKEN.get_secret_value()}"},
    ) as client:
        response = await client.get(str(settings.PERSONA_URL))
        response.raise_for_status()
        payload = response.json()
    return [
        Persona(snapshot_version=payload["snapshot_version"], **item)
        for item in payload["personas"]
    ]


async def main() -> None:
    from waypoint.measurement import METRIC_CATALOG, create_measurement_plan

    logging.basicConfig(level="INFO")
    settings = Settings.load()
    logging.getLogger().setLevel(settings.LOG_LEVEL)
    engine = make_engine(settings.DATABASE_URL.get_secret_value())
    factory = make_session_factory(engine)
    anthropic = AsyncAnthropic(api_key=settings.LLM_API_KEY.get_secret_value())
    pricing = Pricing(models={"fast": settings.MODEL_FAST, "deep": settings.MODEL_DEEP})
    personas = await load_personas(settings)
    calibration = load_calibration(CALIBRATION_PATH)
    context = N8NContextClient(
        url=str(settings.N8N_CONTEXT_URL), token=settings.N8N_TOKEN.get_secret_value()
    )
    worker_id = f"worker-{uuid4().hex[:8]}"
    log.info("worker %s started", worker_id)

    async with factory() as session:
        await apply_fleet_settings(session, settings)

    while True:
        async with factory() as session:
            job = await claim_job(session, worker_id, lease_seconds=LEASE_SECONDS)
            await session.commit()
            if job is None:
                # Idle beat: surface any job that died with no attempts left.
                reaped = await fail_stale_jobs(session)
                if reaped:
                    log.warning("reaped %d attempts-exhausted jobs as failed", reaped)
                await session.commit()
                await asyncio.sleep(POLL_SECONDS)
                continue
            log.info("worker %s claimed job %s (run %s)", worker_id, job.id, job.run_id)
            # The gateway gets its own session so paid-usage rows survive
            # pipeline rollbacks.
            async with factory() as usage_session:
                deps = PipelineDeps(
                    store=PostgresStore(session),
                    llm=LLMGateway(anthropic, usage_session, pricing),
                    context=context,
                    queue=QueueOps(session),
                    personas=personas,
                    calibration=calibration,
                    create_plan=create_measurement_plan,
                    metric_catalog=METRIC_CATALOG,
                    worker_id=worker_id,
                    lease_seconds=LEASE_SECONDS,
                )
                try:
                    await run_job(job.id, deps)
                except Exception:
                    # The lease expires and another worker resumes from the
                    # checkpoint; attempts-exhausted jobs get reaped as failed.
                    log.exception("job %s crashed; leaving for lease recovery", job.id)
                    await session.rollback()


if __name__ == "__main__":
    asyncio.run(main())
