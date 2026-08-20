"""FastAPI composition: authenticated run, status, kill, evidence, and handoff routes.

Starting a run returns 202 immediately; workers do the paid work. The UI polls
durable state. Health exposes nothing but liveness.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from waypoint import auth, queue
from waypoint.db import make_engine, make_session_factory
from waypoint.handoff import (
    AudienceLineageUnresolved,
    HandoffUnavailable,
    make_lcm_client,
    ready_rows,
)
from waypoint.loop import LoopConfig
from waypoint.models import (
    TERMINAL_RUN_STATUSES,
    HandoffReceipt,
    RunCreate,
    RunView,
    TouchOutcomeIn,
)
from waypoint.outcomes import ingest as ingest_outcomes_batch
from waypoint.settings import Settings
from waypoint.tables import (
    CandidateRow,
    FleetControlRow,
    HandoffRow,
    JobRow,
    MeasurementRow,
    RunRow,
    WinnerRow,
)


class LoginRequest(BaseModel):
    password: str


class RunDetail(RunView):
    stages: dict[str, Any]
    candidates: list[dict[str, Any]]
    winners: list[dict[str, Any]]
    measurements: list[dict[str, Any]]
    handoffs: list[dict[str, Any]]
    killed: bool
    agents_in_flight: int  # per-Pro jobs a worker is actively leasing right now


class HandoffResponse(BaseModel):
    receipts: list[HandoffReceipt]


def _view(run: RunRow, spent: Decimal | None = None) -> RunView:
    return RunView(
        id=run.id,
        status=run.status,
        pro_ids=run.pro_ids,
        audience_query=run.audience_query,
        audience_run=run.audience_run,
        channels=run.channels,
        config_version=run.config_version,
        loop_config=dict(run.loop_config or {}),
        cost_limit_usd=run.cost_limit,
        cost_reserved_usd=run.cost_reserved,
        cost_spent_usd=run.cost_spent if spent is None else spent,
        stop_reason=run.stop_reason,
        created_at=run.created_at,
        journey_window=run.journey_window,
    )


async def _spent(session: AsyncSession, run: RunRow) -> Decimal:
    """Real spend: usage rows, floored by the run ledger — an abandoned call's
    worst-case conversion has no usage row and must still be visible."""
    from waypoint.tables import UsageRow

    total = (
        await session.execute(
            select(func.coalesce(func.sum(UsageRow.cost_usd), 0)).where(UsageRow.run_id == run.id)
        )
    ).scalar_one()
    return max(Decimal(total or 0), run.cost_spent or Decimal(0))


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    if app.state.settings is None:
        app.state.settings = Settings.load()
    if app.state.session_factory is None:
        engine = make_engine(app.state.settings.DATABASE_URL.get_secret_value())
        app.state.session_factory = make_session_factory(engine)
    yield


async def _get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(_get_session)]
AuthDep = Annotated[None, Depends(auth.require_session)]


async def _ensure_fleet(session: AsyncSession, settings: Settings) -> None:
    """The KILL_SWITCH env value is authoritative — it must engage (and clear)
    the shared kill state on the existing row, not just at first creation."""
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


async def _run_or_404(session: AsyncSession, run_id: str) -> RunRow:
    run = await session.get(RunRow, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def create_app(
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    app = FastAPI(title="Pathfinder Waypoint V2", version="1.0.0", lifespan=_lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/auth/login")
    async def login(request: Request, response: Response, body: LoginRequest) -> dict[str, str]:
        auth.login(request.app.state.settings, response, body.password)
        return {"status": "ok"}

    @app.post("/api/runs", status_code=202, response_model=RunView)
    async def create_run(
        request: Request, body: RunCreate, session: SessionDep, _: AuthDep
    ) -> RunView:
        settings: Settings = request.app.state.settings
        await _ensure_fleet(session, settings)
        fleet = await session.get(FleetControlRow, 1)
        assert fleet is not None
        defaults = dict(fleet.loop_defaults or {})
        try:
            config = LoopConfig.from_mapping({**defaults, **(body.loop_config or {})})
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if body.loop_config:
            # A confirmed edit becomes the persisted default for next time.
            fleet.loop_defaults = config.to_dict()
        # Dedupe (order-preserving): duplicates would violate
        # uq_jobs_run_stage_pro and 500 after the run row was created.
        pro_ids = list(dict.fromkeys(body.pro_ids))
        run = RunRow(
            pro_ids=pro_ids,
            audience_query=body.audience_query,
            audience_run=body.audience_run,
            channels=body.channels,
            journey_window=body.journey_window,
            loop_config=config.to_dict(),  # immutable per-run snapshot
            cost_limit=Decimal(settings.RUN_COST_USD),
        )
        session.add(run)
        await session.flush()
        for pro_id in pro_ids:
            await queue.enqueue(session, run.id, stage="pro", pro_id=pro_id)
        await session.commit()
        return _view(run)

    @app.get("/api/fleet/settings")
    async def fleet_settings(request: Request, session: SessionDep, _: AuthDep) -> dict[str, Any]:
        settings: Settings = request.app.state.settings
        await _ensure_fleet(session, settings)
        fleet = await session.get(FleetControlRow, 1)
        assert fleet is not None
        effective = LoopConfig.from_mapping(dict(fleet.loop_defaults or {}))
        await session.commit()
        return {
            "loop_defaults": effective.to_dict(),
            "max_in_flight_llm_calls": settings.MAX_LLM_IN_FLIGHT,
        }

    @app.get("/api/runs/{run_id}", response_model=RunDetail)
    async def run_detail(run_id: str, session: SessionDep, _: AuthDep) -> RunDetail:
        run = await _run_or_404(session, run_id)
        jobs = (
            (await session.execute(select(JobRow).where(JobRow.run_id == run_id))).scalars().all()
        )
        # Agents in flight: per-Pro jobs a worker is actively leasing right now
        # (running with a live lease). func.now() is DB-side so it matches the
        # claim SQL and sidesteps client/column tz mismatch.
        agents_in_flight = (
            await session.execute(
                select(func.count())
                .select_from(JobRow)
                .where(
                    JobRow.run_id == run_id,
                    JobRow.status == "running",
                    JobRow.lease_until > func.now(),
                )
            )
        ).scalar_one()
        # A stage shows done only when EVERY per-Pro job checkpointed it — an
        # honest floor; a half-done stage never shows a checkmark.
        stages: dict[str, Any] = {}
        if jobs:
            shared = set(jobs[0].checkpoint)
            for job in jobs[1:]:
                shared &= set(job.checkpoint)
            stages = {stage: jobs[0].checkpoint[stage] for stage in shared}
        candidates = (
            (
                await session.execute(
                    select(CandidateRow)
                    .where(CandidateRow.run_id == run_id)
                    .order_by(CandidateRow.created_at, CandidateRow.id)
                )
            )
            .scalars()
            .all()
        )
        winners = (
            (await session.execute(select(WinnerRow).where(WinnerRow.run_id == run_id)))
            .scalars()
            .all()
        )
        measurements = (
            (await session.execute(select(MeasurementRow).where(MeasurementRow.run_id == run_id)))
            .scalars()
            .all()
        )
        handoffs = (
            (await session.execute(select(HandoffRow).where(HandoffRow.run_id == run_id)))
            .scalars()
            .all()
        )
        return RunDetail(
            **_view(run, spent=await _spent(session, run)).model_dump(),
            stages=stages,
            candidates=[
                {
                    "id": c.id,
                    "pro_id": c.pro_id,
                    "recommendation": c.recommendation,
                    "critics": c.critics,
                    "persona_evidence": c.persona_evidence,
                    "score": c.score,
                    "status": c.status,
                    "round": c.round,
                }
                for c in candidates
            ],
            winners=[
                {
                    "id": w.id,
                    "pro_id": w.pro_id,
                    "kind": w.kind,
                    "candidate_id": w.candidate_id,
                    "rationale": w.rationale,
                    "evidence": w.evidence,
                    "warm_start_eligible": w.warm_start_eligible,
                    "validation_status": w.validation_status,
                    "fingerprint_version": w.fingerprint_version,
                }
                for w in winners
            ],
            measurements=[
                {
                    "id": m.id,
                    "winner_id": m.winner_id,
                    "indicators": m.indicators,
                }
                for m in measurements
            ],
            handoffs=[
                {
                    "id": h.id,
                    "idempotency_key": h.idempotency_key,
                    "status": h.status,
                    "response": h.response,
                }
                for h in handoffs
            ],
            killed=await queue.fleet_is_killed(session),
            agents_in_flight=agents_in_flight,
        )

    @app.post("/api/runs/{run_id}/kill", response_model=RunView)
    async def kill_run(run_id: str, session: SessionDep, _: AuthDep) -> RunView:
        run = await _run_or_404(session, run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            # A terminal run is immutable; killing it would rewrite history.
            raise HTTPException(status_code=409, detail=f"run is already {run.status}")
        run.status = "stopped"
        run.stop_reason = "operator_kill"
        for job in (await session.execute(select(JobRow).where(JobRow.run_id == run_id))).scalars():
            if job.status not in ("done", "failed", "stopped"):
                job.status = "stopped"
        await session.commit()
        return _view(run, spent=await _spent(session, run))

    @app.post("/api/outcomes", status_code=202)
    async def ingest_outcomes(
        body: list[TouchOutcomeIn], session: SessionDep, _: AuthDep
    ) -> dict[str, int]:
        """Observed messaging/app-usage outcomes, keyed by recommendation_id.
        See waypoint.outcomes for the attribution/backfill/idempotency logic."""
        return await ingest_outcomes_batch(session, body)

    @app.post("/api/runs/{run_id}/handoff", response_model=HandoffResponse)
    async def create_handoff(
        request: Request, run_id: str, session: SessionDep, _: AuthDep
    ) -> HandoffResponse:
        settings: Settings = request.app.state.settings
        await _run_or_404(session, run_id)
        try:
            # The lineage guard lives in ready_rows so every handoff caller
            # inherits it; refusing beats handing off a winner sourced from an
            # unverified audience.
            rows = await ready_rows(session, run_id)
        except AudienceLineageUnresolved as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if not rows:
            raise HTTPException(
                status_code=409, detail="No persisted winner with a measurement plan"
            )
        client = make_lcm_client(settings, session)
        # Pathfinder Intake API: no PII, one POST per batch (never per row).
        try:
            receipts = await client.handoff(run_id, rows)
        except HandoffUnavailable as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        finally:
            await client.aclose()
        return HandoffResponse(receipts=receipts)

    return app


app = create_app()
