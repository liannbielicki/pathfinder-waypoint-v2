"""Worker process: claim leased jobs and run the pipeline until stopped.

Run with: python -m waypoint.worker
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint import queue
from waypoint.calls import FleetSlots, MeteredLLM, RecordedCalls
from waypoint.db import make_engine, make_session_factory
from waypoint.llm import LLMGateway, Pricing
from waypoint.n8n import N8NContextClient
from waypoint.personas import Persona
from waypoint.pipeline import (
    PipelineDeps,
    PostgresStore,
    QueueOps,
    finalize_stalled_runs,
    run_job,
)
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
        session.add(
            FleetControlRow(
                id=1,
                killed=settings.KILL_SWITCH,
                day_cost_limit=settings.DAY_COST_USD,
            )
        )
    else:
        fleet.killed = settings.KILL_SWITCH
        fleet.day_cost_limit = settings.DAY_COST_USD
    await session.commit()


# ponytail: base request is fixed to the documented default; segment is
# supplied per-Pro at call time. Lift size/seed to Settings only if a second
# value is actually needed.
PERSONA_PANEL_REQUEST = {
    "panel_size": 24,
    "seed": 42,
    "subtype_ids": None,
    "subtype_version": None,
}


async def load_personas(settings: Settings, segment: str) -> list[Persona]:
    """Create a persona panel for `segment` from the persona-cards service."""
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=False,
        headers={"X-API-Key": settings.PERSONA_TOKEN.get_secret_value()},
    ) as client:
        response = await client.post(
            f"{str(settings.PERSONA_URL).rstrip('/')}/api/persona-cards",
            json={**PERSONA_PANEL_REQUEST, "segment": segment},
        )
        response.raise_for_status()
        payload = response.json()
    items = payload["personas"]
    if items:
        # ponytail: heuristic field mapping — the persona-cards service's item
        # shape isn't documented here. Log the real keys once and tighten the
        # picks below if a field lands in the wrong slot.
        log.info("persona-cards item keys: %s", sorted(items[0].keys()))
    return [_adapt_persona(item, payload["subtype_version"], segment) for item in items]


def _adapt_persona(item: dict[str, Any], snapshot_version: str, segment: str) -> Persona:
    """Map a persona-cards item onto waypoint's Persona. `family` and `label`
    fall back through likely names, then to persona_id; every non-id field is
    kept as a feature (scoring reads only the permitted subset)."""
    pid = str(item["persona_id"])
    family = item.get("family") or item.get("subtype_id") or item.get("subtype") or pid
    label = item.get("label") or item.get("name") or item.get("title") or pid
    # The card carries its segment under `segment_key` (e.g. "2A"), not the
    # `segment` name the Pro matches on — so expose it as `segment`, or every
    # card lacks the one shared key and the panel abstains at 0 fit. Fall back
    # to the requested pool segment if a card ever omits segment_key.
    features = {k: v for k, v in item.items() if k != "persona_id"}
    features["segment"] = item.get("segment_key") or segment
    return Persona(
        persona_id=pid,
        family=str(family),
        label=str(label),
        features=features,
        snapshot_version=snapshot_version,
    )


def make_persona_source(settings: Settings) -> Callable[[str], Awaitable[list[Persona]]]:
    """Per-segment persona pools, fetched once per segment and cached for the
    worker's lifetime (segments are stable within a run)."""
    cache: dict[str, list[Persona]] = {}

    async def get_personas(segment: str) -> list[Persona]:
        if segment not in cache:
            cache[segment] = await load_personas(settings, segment)
        return cache[segment]

    return get_personas


async def main() -> None:
    from waypoint.measurement import METRIC_CATALOG, create_measurement_plan

    logging.basicConfig(level="INFO")
    settings = Settings.load()
    logging.getLogger().setLevel(settings.LOG_LEVEL)
    engine = make_engine(settings.DATABASE_URL.get_secret_value())
    factory = make_session_factory(engine)
    anthropic = AsyncAnthropic(api_key=settings.LLM_API_KEY.get_secret_value())
    pricing = Pricing(models={"fast": settings.MODEL_FAST, "deep": settings.MODEL_DEEP})
    persona_source = make_persona_source(settings)
    calibration = load_calibration(CALIBRATION_PATH)
    context = N8NContextClient(
        url=str(settings.N8N_CONTEXT_URL), token=settings.N8N_TOKEN.get_secret_value()
    )
    worker_id = f"worker-{uuid4().hex[:8]}"
    log.info("worker %s started", worker_id)

    async with factory() as session:
        await apply_fleet_settings(session, settings)

    # Fleet slot locks are session-level advisory locks: they belong to the
    # CONNECTION, so the limiter owns one dedicated connection for the worker's
    # lifetime — a crash releases the slot when the connection dies.
    slots_connection = await engine.connect()
    slots = FleetSlots(slots_connection)

    while True:
        async with factory() as session:
            job = await claim_job(session, worker_id, lease_seconds=LEASE_SECONDS)
            await session.commit()
            if job is None:
                # Idle beat: surface any job that died with no attempts left,
                # then finalize every run whose jobs are all terminal — this
                # covers reaped runs AND runs stranded by a crash between the
                # last job's terminal commit and its finalize_run call.
                reaped = await fail_stale_jobs(session)
                await session.commit()
                if reaped:
                    log.warning("reaped %d attempts-exhausted jobs as failed", len(reaped))
                healed = await finalize_stalled_runs(session)
                if healed:
                    log.warning("finalized %d stalled runs", healed)
                await asyncio.sleep(POLL_SECONDS)
                continue
            log.info("worker %s claimed job %s (run %s)", worker_id, job.id, job.run_id)
            # The calls/usage session is separate from the pipeline session so
            # paid facts (usage rows, call records, reservations, reconciles)
            # survive pipeline rollbacks.
            async with factory() as usage_session:
                deps = PipelineDeps(
                    store=PostgresStore(session),
                    llm=MeteredLLM(
                        gateway=LLMGateway(anthropic, usage_session, pricing),
                        records=RecordedCalls(usage_session),
                        slots=slots,
                        pricing=pricing,
                        reserve=partial(queue.reserve_cost, usage_session),
                        reconcile=partial(queue.reconcile_cost, usage_session),
                    ),
                    context=context,
                    queue=QueueOps(session),
                    get_personas=persona_source,
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
