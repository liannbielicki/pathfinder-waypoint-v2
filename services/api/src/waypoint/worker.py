"""Worker process: claim leased jobs and run the pipeline until stopped.

Run with: python -m waypoint.worker
"""

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

import httpx
from anthropic import AsyncAnthropic

from waypoint.db import make_engine, make_session_factory
from waypoint.llm import LLMGateway, Pricing
from waypoint.n8n import N8NContextClient
from waypoint.personas import Persona
from waypoint.pipeline import PipelineDeps, PostgresStore, QueueOps, run_job
from waypoint.queue import claim_job
from waypoint.scoring import load_calibration
from waypoint.settings import Settings

log = logging.getLogger("waypoint.worker")

POLL_SECONDS = 2.0
CALIBRATION_PATH = Path(__file__).parents[2] / "data" / "reaction_churn_calibration_cards.json"


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
    # Lands in the measurement task; lazy import keeps the module boundary clean.
    from waypoint.measurement import (  # type: ignore[import-untyped]
        METRIC_CATALOG,
        create_measurement_plan,
    )

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

    while True:
        async with factory() as session:
            job = await claim_job(session, worker_id)
            await session.commit()
            if job is None:
                await asyncio.sleep(POLL_SECONDS)
                continue
            log.info("worker %s claimed job %s (run %s)", worker_id, job.id, job.run_id)
            deps = PipelineDeps(
                store=PostgresStore(session),
                llm=LLMGateway(anthropic, session, pricing),
                context=context,
                queue=QueueOps(session),
                personas=personas,
                calibration=calibration,
                create_plan=create_measurement_plan,
                metric_catalog=METRIC_CATALOG,
            )
            try:
                await run_job(job.id, deps)
            except Exception:
                # The lease expires and another worker resumes from the checkpoint.
                log.exception("job %s crashed; leaving for lease recovery", job.id)
                await session.rollback()


if __name__ == "__main__":
    asyncio.run(main())
