"""Worker process: claim leased jobs and run the pipeline until stopped.

Run with: python -m waypoint.worker
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from waypoint import amplitude_source, iterable_source, queue
from waypoint.calls import FleetSlots, MeteredLLM, RecordedCalls
from waypoint.checkpoints import sweep_if_enabled
from waypoint.db import make_engine, make_session_factory
from waypoint.handoff import lcm_http_client, push_ready_winners
from waypoint.llm import LLMGateway, Pricing, retry_rate_limit
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
from waypoint.scoring import Calibration, load_calibration
from waypoint.settings import Settings
from waypoint.tables import FleetControlRow

log = logging.getLogger("waypoint.worker")

POLL_SECONDS = 2.0
# Must comfortably exceed one n8n context call (up to N8N_TIMEOUT_SECONDS):
# nothing heartbeats during that fetch, and a lease shorter than the call gets
# the job re-claimed mid-fetch and worked twice.
LEASE_SECONDS = 1800
CALIBRATION_PATH = Path(__file__).parents[2] / "data" / "reaction_churn_calibration_cards.json"


async def apply_fleet_settings(session: AsyncSession, settings: Settings) -> None:
    """KILL_SWITCH, LEARNING_KILL_SWITCH, and DAY_COST_USD are env-owned;
    apply them on startup. The two kill switches are independent."""
    fleet = await session.get(FleetControlRow, 1)
    if fleet is None:
        session.add(
            FleetControlRow(
                id=1,
                killed=settings.KILL_SWITCH,
                learning_killed=settings.LEARNING_KILL_SWITCH,
                day_cost_limit=settings.DAY_COST_USD,
            )
        )
    else:
        fleet.killed = settings.KILL_SWITCH
        fleet.learning_killed = settings.LEARNING_KILL_SWITCH
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

        async def fetch() -> httpx.Response:
            response = await client.post(
                f"{str(settings.PERSONA_URL).rstrip('/')}/api/persona-cards",
                json={**PERSONA_PANEL_REQUEST, "segment": segment},
            )
            response.raise_for_status()
            return response

        # The persona-cards service rate-limits aggressively until its quota
        # is raised: back off through 429s instead of burning a job attempt.
        payload = (await retry_rate_limit(fetch, attempts=5, backoff_seconds=3.0)).json()
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


def make_pricing(settings: Settings) -> Pricing:
    """Tier → model. The ranker gets its own tier; empty MODEL_RANKER means
    "share the fast model" (Pricing still validates it against the price table)."""
    return Pricing(
        models={
            "fast": settings.MODEL_FAST,
            "deep": settings.MODEL_DEEP,
            "rank": settings.MODEL_RANKER or settings.MODEL_FAST,
        }
    )


def poller_specs(settings: Settings) -> list[tuple[str, Any, Any]]:
    """(name, make_client, poll) for each outcome poller whose keys are
    configured. A missing key disables that poller with one startup log line;
    the worker runs fine with zero keys configured."""
    specs: list[tuple[str, Any, Any]] = []
    if settings.ITERABLE_API_KEY is not None:
        specs.append(("iterable", iterable_source.make_client, iterable_source.poll_if_enabled))
    else:
        log.info("ITERABLE_API_KEY unset; iterable outcome poller disabled")
    if settings.AMPLITUDE_API_KEY is not None and settings.AMPLITUDE_SECRET_KEY is not None:
        specs.append(
            ("amplitude", amplitude_source.make_client, amplitude_source.poll_if_enabled)
        )
    else:
        log.info("AMPLITUDE_API_KEY/AMPLITUDE_SECRET_KEY unset; amplitude poller disabled")
    return specs


LLMStacks = Callable[[], AbstractAsyncContextManager[tuple[MeteredLLM, AsyncSession]]]


def make_llm_stacks(
    *,
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    pricing: Pricing,
    max_slots: int,
) -> LLMStacks:
    """Factory for independent paid-call stacks, one per concurrent screen.

    Nothing here may be shared between concurrent tasks: the advisory-lock
    slot connection is CONNECTION-scoped (see FleetSlots) and an AsyncSession
    is single-statement-at-a-time. `max_slots` is the SAME cap as the main
    limiter, so the fleet-wide ceiling stays one limit rather than two."""

    @asynccontextmanager
    async def stack() -> AsyncIterator[tuple[MeteredLLM, AsyncSession]]:
        connection = await engine.connect()
        try:
            async with factory() as usage_session, factory() as cache_session:
                yield (
                    MeteredLLM(
                        gateway=LLMGateway(anthropic, usage_session, pricing),
                        records=RecordedCalls(usage_session),
                        slots=FleetSlots(connection, max_slots=max_slots),
                        pricing=pricing,
                        reserve=partial(queue.reserve_cost, usage_session),
                        reconcile=partial(queue.reconcile_cost, usage_session),
                    ),
                    cache_session,
                )
        finally:
            await connection.close()

    return stack


async def _worker_loop(
    worker_id: str,
    *,
    factory: async_sessionmaker[AsyncSession],
    slots: FleetSlots,
    llm_stacks: LLMStacks,
    context: N8NContextClient,
    anthropic: AsyncAnthropic,
    pricing: Pricing,
    persona_source: Callable[[str], Awaitable[list[Persona]]],
    calibration: Calibration,
    create_plan: Any,
    metric_catalog: dict[str, Any],
    maintenance: bool,
    settings: Settings,
    lcm_client: httpx.AsyncClient,
) -> None:
    """One claim→process loop. WORKER_COUNT of these run concurrently in-process;
    each owns a distinct worker_id (for lease ownership) and its own fleet-slot
    connection. Claims use FOR UPDATE SKIP LOCKED, so the loops take distinct
    jobs and process that many Pros in parallel."""
    log.info("worker %s started", worker_id)
    while True:
        async with factory() as session:
            job = await claim_job(session, worker_id, lease_seconds=LEASE_SECONDS)
            await session.commit()
            if job is None:
                if maintenance:
                    # Idle beat, one loop only (idempotent, so N concurrent
                    # sweeps would just be wasted contention): surface jobs that
                    # died with no attempts left, then finalize runs whose jobs
                    # are all terminal — covers reaped runs AND runs stranded by
                    # a crash between the last terminal commit and finalize_run.
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
                    create_plan=create_plan,
                    metric_catalog=metric_catalog,
                    cta_feasibility_hints=settings.CTA_FEASIBILITY_HINTS,
                    worker_id=worker_id,
                    lease_seconds=LEASE_SECONDS,
                    llm_stacks=llm_stacks,
                )
                try:
                    await run_job(job.id, deps)
                except Exception:
                    # The lease expires and another worker resumes from the
                    # checkpoint; attempts-exhausted jobs get reaped as failed.
                    log.exception("job %s crashed; leaving for lease recovery", job.id)
                    await session.rollback()
                    continue
            # Trickle handoff: stream THIS Pro's ready winner to the LCM as it
            # finishes so QA runs concurrently with the run. Pro-scoped, so
            # concurrent loops touch disjoint rows. Best effort —
            # push_ready_winners is idempotent (answered rows are skipped),
            # so the manual POST /handoff retries anything that failed here.
            try:
                async with factory() as handoff_session:
                    sent = await push_ready_winners(
                        handoff_session,
                        settings,
                        job.run_id,
                        pro_id=job.pro_id,
                        client=lcm_client,
                    )
                if sent:
                    log.info(
                        "trickled %d winner row(s) to LCM for run %s pro %s",
                        sent, job.run_id, job.pro_id,
                    )
            except Exception:
                log.warning(
                    "trickle handoff failed for run %s; POST /handoff remains available",
                    job.run_id,
                    exc_info=True,
                )


async def main() -> None:
    from waypoint.measurement import METRIC_CATALOG, select_indicators

    logging.basicConfig(level="INFO")
    settings = Settings.load()
    logging.getLogger().setLevel(settings.LOG_LEVEL)
    engine = make_engine(
        settings.DATABASE_URL.get_secret_value(),
        # Each loop holds one permanent fleet-slot connection plus, while busy,
        # a pipeline + usage session, and during a tied round two concurrent
        # screen stacks of (slot connection + usage + cache session) each.
        # Sized so the steady state fits the pool proper: max_overflow means the
        # old size would not have deadlocked, it would have churned overflow
        # connections and stalled on checkout under load.
        pool_size=settings.WORKER_COUNT * 9 + 2,
    )
    factory = make_session_factory(engine)
    anthropic = AsyncAnthropic(api_key=settings.LLM_API_KEY.get_secret_value())
    pricing = make_pricing(settings)
    llm_stacks = make_llm_stacks(
        engine=engine,
        factory=factory,
        anthropic=anthropic,
        pricing=pricing,
        max_slots=settings.MAX_LLM_IN_FLIGHT,
    )
    persona_source = make_persona_source(settings)
    calibration = load_calibration(CALIBRATION_PATH)
    context = N8NContextClient(
        url=str(settings.N8N_CONTEXT_URL),
        token=settings.N8N_TOKEN.get_secret_value(),
        timeout=settings.N8N_TIMEOUT_SECONDS,
        max_concurrent=settings.N8N_MAX_CONCURRENT,
    )

    # One long-lived LCM transport shared by every loop (like the n8n client):
    # no per-Pro TLS handshake on the trickle path.
    lcm_client = lcm_http_client(settings)

    async with factory() as session:
        await apply_fleet_settings(session, settings)

    base_id = uuid4().hex[:8]

    async def spawn(index: int) -> None:
        # Fleet slot locks are session-level advisory locks: they belong to the
        # CONNECTION, so each concurrent loop needs its OWN connection — sharing
        # one would corrupt the limiter and run concurrent statements on a single
        # connection. A crash releases the slot when the connection dies.
        slots_connection = await engine.connect()
        try:
            await _worker_loop(
                f"worker-{base_id}-{index}",
                factory=factory,
                slots=FleetSlots(slots_connection, max_slots=settings.MAX_LLM_IN_FLIGHT),
                llm_stacks=llm_stacks,
                context=context,
                anthropic=anthropic,
                pricing=pricing,
                persona_source=persona_source,
                calibration=calibration,
                create_plan=select_indicators,
                metric_catalog=METRIC_CATALOG,
                maintenance=(index == 0),
                settings=settings,
                lcm_client=lcm_client,
            )
        finally:
            await slots_connection.close()

    async def checkpoint_loop() -> None:
        # Timed cadence, deliberately NOT tied to worker idleness: a saturated
        # queue must not starve checkpoint resolution. Bounded per sweep;
        # failures log and the next tick retries the same rows.
        while True:
            await asyncio.sleep(settings.CHECKPOINT_SECONDS)
            try:
                async with factory() as session:
                    result = await sweep_if_enabled(
                        session,
                        now=datetime.now(UTC),
                        limit=settings.CHECKPOINT_LIMIT,
                    )
                if result and (result["resolved"] or result["synthesized"]):
                    log.info(
                        "checkpoint sweep resolved=%d synthesized=%d",
                        result["resolved"], result["synthesized"],
                    )
            except Exception:
                log.warning("checkpoint sweep failed; next tick retries", exc_info=True)

    async def poller_loop(name: str, client: httpx.AsyncClient, poll: Any) -> None:
        # Same shape as checkpoint_loop: timed cadence, session-per-tick,
        # gated by the learning kill switch, failures log and the next tick
        # retries the same window (the cursor only advances on success).
        while True:
            await asyncio.sleep(settings.POLL_SECONDS)
            try:
                async with factory() as session:
                    result = await poll(session, client, settings, datetime.now(UTC))
                if result and any(result.values()):
                    log.info("%s poll: %s", name, result)
            except Exception:
                log.warning("%s poll failed; next tick retries", name, exc_info=True)

    pollers = [
        poller_loop(name, make_client(settings), poll)
        for name, make_client, poll in poller_specs(settings)
    ]

    log.info("starting %d worker loop(s)", settings.WORKER_COUNT)
    await asyncio.gather(
        checkpoint_loop(), *pollers, *(spawn(i) for i in range(settings.WORKER_COUNT))
    )


if __name__ == "__main__":
    asyncio.run(main())
