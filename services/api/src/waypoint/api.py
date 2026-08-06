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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from waypoint import auth, queue
from waypoint.db import make_engine, make_session_factory
from waypoint.handoff import HandoffUnavailable, LCMClient
from waypoint.models import HandoffReceipt, MeasurementPlan, RunCreate, RunView
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


class HandoffResponse(BaseModel):
    receipts: list[HandoffReceipt]


def _view(run: RunRow) -> RunView:
    return RunView(
        id=run.id, status=run.status, pro_ids=run.pro_ids,
        audience_query=run.audience_query, audience_run=run.audience_run,
        channels=run.channels, config_version=run.config_version,
        cost_limit_usd=run.cost_limit, cost_reserved_usd=run.cost_reserved,
        cost_spent_usd=run.cost_spent, stop_reason=run.stop_reason,
        created_at=run.created_at,
    )


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
    if await session.get(FleetControlRow, 1) is None:
        session.add(FleetControlRow(
            id=1, killed=settings.KILL_SWITCH, day_cost_limit=settings.DAY_COST_USD,
        ))
    else:
        fleet = await session.get(FleetControlRow, 1)
        assert fleet is not None
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
    async def create_run(request: Request, body: RunCreate, session: SessionDep,
                         _: AuthDep) -> RunView:
        settings: Settings = request.app.state.settings
        await _ensure_fleet(session, settings)
        run = RunRow(
            pro_ids=body.pro_ids, audience_query=body.audience_query,
            audience_run=body.audience_run, channels=body.channels,
            cost_limit=Decimal(settings.RUN_COST_USD),
        )
        session.add(run)
        await session.flush()
        await queue.enqueue(session, run.id, stage="recommend")
        await session.commit()
        return _view(run)

    @app.get("/api/runs/{run_id}", response_model=RunDetail)
    async def run_detail(run_id: str, session: SessionDep, _: AuthDep) -> RunDetail:
        run = await _run_or_404(session, run_id)
        job = (await session.execute(
            select(JobRow).where(JobRow.run_id == run_id)
        )).scalars().first()
        candidates = (await session.execute(
            select(CandidateRow).where(CandidateRow.run_id == run_id)
            .order_by(CandidateRow.created_at, CandidateRow.id)
        )).scalars().all()
        winners = (await session.execute(
            select(WinnerRow).where(WinnerRow.run_id == run_id)
        )).scalars().all()
        measurements = (await session.execute(
            select(MeasurementRow).where(MeasurementRow.run_id == run_id)
        )).scalars().all()
        handoffs = (await session.execute(
            select(HandoffRow).where(HandoffRow.run_id == run_id)
        )).scalars().all()
        return RunDetail(
            **_view(run).model_dump(),
            stages=dict(job.checkpoint) if job is not None else {},
            candidates=[{
                "id": c.id, "pro_id": c.pro_id, "recommendation": c.recommendation,
                "critics": c.critics, "persona_evidence": c.persona_evidence,
                "score": c.score, "status": c.status,
            } for c in candidates],
            winners=[{
                "id": w.id, "pro_id": w.pro_id, "kind": w.kind,
                "candidate_id": w.candidate_id, "rationale": w.rationale,
                "evidence": w.evidence,
            } for w in winners],
            measurements=[{
                "id": m.id, "winner_id": m.winner_id, "indicators": m.indicators,
            } for m in measurements],
            handoffs=[{
                "id": h.id, "idempotency_key": h.idempotency_key, "status": h.status,
                "response": h.response,
            } for h in handoffs],
            killed=await queue.fleet_is_killed(session),
        )

    @app.post("/api/runs/{run_id}/kill", response_model=RunView)
    async def kill_run(run_id: str, session: SessionDep, _: AuthDep) -> RunView:
        run = await _run_or_404(session, run_id)
        run.status = "stopped"
        run.stop_reason = "operator_kill"
        for job in (await session.execute(
            select(JobRow).where(JobRow.run_id == run_id)
        )).scalars():
            job.status = "stopped"
        await session.commit()
        return _view(run)

    @app.post("/api/runs/{run_id}/handoff", response_model=HandoffResponse)
    async def create_handoff(request: Request, run_id: str, session: SessionDep,
                             _: AuthDep) -> HandoffResponse:
        settings: Settings = request.app.state.settings
        run = await _run_or_404(session, run_id)
        winners = (await session.execute(
            select(WinnerRow).where(WinnerRow.run_id == run_id, WinnerRow.kind == "winner")
        )).scalars().all()
        ready: list[tuple[WinnerRow, MeasurementRow, CandidateRow]] = []
        for winner in winners:
            measurement = (await session.execute(
                select(MeasurementRow).where(MeasurementRow.winner_id == winner.id)
            )).scalar_one_or_none()
            candidate = (
                await session.get(CandidateRow, winner.candidate_id)
                if winner.candidate_id else None
            )
            if measurement is not None and candidate is not None:
                ready.append((winner, measurement, candidate))
        if not ready:
            raise HTTPException(
                status_code=409, detail="No persisted winner with a measurement plan"
            )
        client = LCMClient(
            url=str(settings.HANDOFF_URL),
            token=settings.HANDOFF_TOKEN.get_secret_value(),
            session=session,
        )
        lineage = {"audience_query": run.audience_query, "audience_run": run.audience_run}
        receipts = []
        try:
            for winner, measurement, candidate in ready:
                receipts.append(await client.handoff(
                    {
                        "run_id": run_id, "winner_id": winner.id, "pro_id": winner.pro_id,
                        "org_id": winner.evidence.get("org_id", ""),
                        "recommendation": candidate.recommendation,
                        "score": winner.evidence.get("final", {}),
                    },
                    MeasurementPlan.model_validate({"indicators": measurement.indicators}),
                    lineage,
                ))
        except HandoffUnavailable as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        return HandoffResponse(receipts=receipts)

    return app


app = create_app()
