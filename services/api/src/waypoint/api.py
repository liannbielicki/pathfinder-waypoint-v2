"""FastAPI composition: authenticated run, status, kill, evidence, and handoff routes.

Starting a run returns 202 immediately; workers do the paid work. The UI polls
durable state. Health exposes nothing but liveness.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from waypoint import auth, queue
from waypoint import funnel as funnel_report
from waypoint.activity import current_activity, lock_fleet
from waypoint.call_todos import list_calls, update_call
from waypoint.db import make_engine, make_session_factory
from waypoint.exposures import register as register_exposures_batch
from waypoint.handoff import (
    AudienceLineageUnresolved,
    HandoffUnavailable,
    make_lcm_client,
    ready_rows,
)
from waypoint.hosted_workbench import (
    JobExecutor,
    get_hosted_workbench,
    install_hosted_workbench,
)
from waypoint.loop import LoopConfig
from waypoint.models import (
    TERMINAL_RUN_STATUSES,
    CallItem,
    CallUpdate,
    ContextSource,
    ExposureIn,
    HandoffReceipt,
    RunCreate,
    RunView,
    TouchOutcomeIn,
)
from waypoint.outcomes import ingest as ingest_outcomes_batch
from waypoint.settings import Settings
from waypoint.staging_context import compile_staging_brief
from waypoint.tables import (
    CandidateRow,
    ContextPromotionRow,
    EvolveRoundRow,
    FleetControlRow,
    HandoffRow,
    JobRow,
    MeasurementRow,
    RunRow,
    WinnerRow,
    WorkbenchCatalogVersionRow,
)
from waypoint.workbench import ContextLayerClient, org_uuid_from_n8n
from waypoint.workbench_api import execute_run


def _staging_context_available(settings: Settings) -> bool:
    return all(
        (
            settings.N8N_CONTEXT_URL_WORKBENCH,
            settings.CONTEXT_LAYER_BASE_URL,
            settings.CONTEXT_LAYER_API_KEY,
        )
    )


async def _staging_context_summary(
    session: AsyncSession, settings: Settings
) -> dict[str, Any] | None:
    if not _staging_context_available(settings):
        return None
    promotion = (
        await session.execute(
            select(ContextPromotionRow).where(ContextPromotionRow.active.is_(True))
        )
    ).scalar_one_or_none()
    if promotion is None:
        return None
    bundle = dict(promotion.bundle or {})
    context_id = str(bundle.get("context_catalog_version_id") or "")
    feature_id = str(bundle.get("feature_catalog_version_id") or "")
    context = await session.get(WorkbenchCatalogVersionRow, context_id)
    feature = await session.get(WorkbenchCatalogVersionRow, feature_id)
    if (
        context is None
        or context.kind != "context"
        or feature is None
        or feature.kind != "feature"
    ):
        return None
    rules = bundle.get("rules")
    return {
        "promotion_id": promotion.id,
        "context_catalog_version_id": context_id,
        "context_catalog_name": context.name,
        "feature_catalog_version_id": feature_id,
        "feature_catalog_name": feature.name,
        "included_variables": len(rules) if isinstance(rules, list) else 0,
        "created_at": promotion.activated_at.isoformat(),
    }


class LoginRequest(BaseModel):
    password: str


class StagingContextCallback(BaseModel):
    request_id: str
    organization_id: str
    promotion_id: str
    rows: list[dict[str, Any]]


class RunDetail(RunView):
    stages: dict[str, Any]
    rounds: list[dict[str, Any]]  # per-Pro evolve ledger: loop progress for the console
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
        context_source=cast(ContextSource, run.context_source),
        include_features_not_in_current_plan=run.include_features_not_in_current_plan,
        model_tier=cast(Any, run.model_tier),
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
    workbench = get_hosted_workbench(app)
    await workbench.resume()
    try:
        yield
    finally:
        await workbench.shutdown()


async def _get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(_get_session)]
AuthDep = Annotated[None, Depends(auth.require_session)]
# The outcome automation's two endpoints: write outcomes, and read the bare
# work list. Both accept the scoped OUTCOMES_TOKEN as well as an operator
# cookie. Every other endpoint is operator-only.
OutcomeAuthDep = Annotated[None, Depends(auth.require_session_or_outcomes_token)]
N8NAuthDep = Annotated[None, Depends(auth.require_n8n_token)]


async def _ensure_fleet(session: AsyncSession, settings: Settings) -> None:
    """The KILL_SWITCH / LEARNING_KILL_SWITCH env values are authoritative —
    they must engage (and clear) the shared kill state on the existing row,
    not just at first creation. The two switches are independent."""
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


async def _run_or_404(session: AsyncSession, run_id: str) -> RunRow:
    run = await session.get(RunRow, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def create_app(
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    workbench_executor: JobExecutor | None = None,
) -> FastAPI:
    app = FastAPI(title="Pathfinder Waypoint V2", version="1.0.0", lifespan=_lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    install_hosted_workbench(app, executor=workbench_executor or execute_run)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/context/staging/callback")
    async def complete_staging_context(
        request: Request,
        body: StagingContextCallback,
        session: SessionDep,
        _: N8NAuthDep,
    ) -> dict[str, str]:
        job = await session.get(JobRow, body.request_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Staging context request not found")
        run = await session.get(RunRow, job.run_id)
        if (
            run is None
            or run.context_source != "staging"
            or job.stage != "pro"
            or job.pro_id != body.organization_id
            or run.audience_query != f"workbench:{body.promotion_id}"
        ):
            raise HTTPException(status_code=409, detail="Staging context request does not match")
        if "staging_context" in job.checkpoint:
            return {"status": "already_completed", "request_id": body.request_id}
        if job.status in {"done", "failed", "stopped"} or run.status in TERMINAL_RUN_STATUSES:
            return {"status": "ignored", "request_id": body.request_id}
        requested = job.checkpoint.get("staging_request")
        if not isinstance(requested, dict) or requested.get("promotion_id") != body.promotion_id:
            raise HTTPException(status_code=409, detail="Staging context request is not waiting")

        promotion = await session.get(ContextPromotionRow, body.promotion_id)
        if promotion is None:
            raise HTTPException(status_code=409, detail="Staging context promotion is missing")
        promotion_bundle = dict(promotion.bundle or {})
        include_features_not_in_current_plan = run.include_features_not_in_current_plan
        # Do not hold a database transaction or connection open while calling
        # the external Context Layer service.
        await session.rollback()
        settings: Settings = request.app.state.settings
        if settings.CONTEXT_LAYER_BASE_URL is None or settings.CONTEXT_LAYER_API_KEY is None:
            raise HTTPException(status_code=503, detail="Context Layer is not configured")
        try:
            org_uuid = org_uuid_from_n8n(body.rows)
            if org_uuid is None:
                raise ValueError("Snowflake context is missing a unique org_uuid")
            context_layer = await ContextLayerClient().fetch(
                org_uuid,
                str(settings.CONTEXT_LAYER_BASE_URL),
                settings.CONTEXT_LAYER_API_KEY.get_secret_value(),
            )
            brief = compile_staging_brief(
                body.organization_id,
                body.rows,
                context_layer,
                promotion_bundle,
                include_features_not_in_current_plan=include_features_not_in_current_plan,
            )
        except Exception as error:
            raise HTTPException(
                status_code=502,
                detail=f"Staging context could not be compiled ({type(error).__name__})",
            ) from error

        job = (
            await session.execute(
                select(JobRow)
                .where(JobRow.id == body.request_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if "staging_context" in job.checkpoint:
            await session.rollback()
            return {"status": "already_completed", "request_id": body.request_id}
        run = (
            await session.execute(
                select(RunRow)
                .where(RunRow.id == job.run_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if job.status in {"done", "failed", "stopped"} or run.status in TERMINAL_RUN_STATUSES:
            await session.rollback()
            return {"status": "ignored", "request_id": body.request_id}
        if job.status != "waiting":
            await session.rollback()
            raise HTTPException(status_code=409, detail="Staging context request is not waiting")
        requested = job.checkpoint.get("staging_request")
        if not isinstance(requested, dict) or requested.get("promotion_id") != body.promotion_id:
            await session.rollback()
            raise HTTPException(status_code=409, detail="Staging context request does not match")
        checkpoint = dict(job.checkpoint)
        checkpoint["staging_context"] = {
            "promotion_id": body.promotion_id,
            "brief": {
                **brief.model_dump(mode="json", exclude_none=True),
                "curated_context": brief.curated_context,
            },
        }
        job.checkpoint = checkpoint
        job.status = "queued"
        job.worker_id = None
        job.lease_until = None
        await session.commit()
        return {"status": "queued", "request_id": body.request_id}

    @app.post("/api/auth/login")
    async def login(request: Request, response: Response, body: LoginRequest) -> dict[str, str]:
        settings: Settings = request.app.state.settings
        auth.verify_password(settings, body.password)
        async with request.app.state.session_factory() as session:
            await lock_fleet(session, settings)
            if await current_activity(session) == "workbench":
                raise HTTPException(status_code=409, detail="Context Workbench is running")
            await session.commit()
        auth.login(settings, response, body.password)
        return {"status": "ok"}

    @app.post("/api/runs", status_code=202, response_model=RunView)
    async def create_run(
        request: Request, body: RunCreate, session: SessionDep, _: AuthDep
    ) -> RunView:
        settings: Settings = request.app.state.settings
        if body.context_source == "staging" and any(
            len(identifier) not in {5, 6} or not identifier.isdigit()
            for identifier in body.pro_ids
        ):
            raise HTTPException(
                status_code=422,
                detail="Staging context requires five- or six-digit organization IDs",
            )
        await _ensure_fleet(session, settings)
        fleet = await lock_fleet(session, settings)
        if await current_activity(session) == "workbench":
            raise HTTPException(status_code=409, detail="Context Workbench is running")
        if body.context_source == "staging":
            staging_context = await _staging_context_summary(session, settings)
            if staging_context is None:
                raise HTTPException(
                    status_code=422,
                    detail="Staging context is unavailable because its sources or active Workbench catalog are missing",
                )
            if body.context_promotion_id != staging_context["promotion_id"]:
                raise HTTPException(
                    status_code=409,
                    detail="The active Staging context changed; review it and start the run again",
                )
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
            audience_query=(
                f"workbench:{body.context_promotion_id}"
                if body.context_source == "staging"
                else body.audience_query
            ),
            audience_run=body.audience_run,
            channels=body.channels,
            journey_window=body.journey_window,
            context_source=body.context_source,
            include_features_not_in_current_plan=body.include_features_not_in_current_plan,
            model_tier=body.model_tier,
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
        staging_context = await _staging_context_summary(session, settings)
        return {
            "loop_defaults": effective.to_dict(),
            "max_in_flight_llm_calls": settings.MAX_LLM_IN_FLIGHT,
            "models": {"fast": settings.MODEL_FAST, "deep": settings.MODEL_DEEP},
            "staging_context_available": staging_context is not None,
            "staging_context": staging_context,
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
        rounds = (
            (
                await session.execute(
                    select(EvolveRoundRow)
                    .where(EvolveRoundRow.run_id == run_id)
                    .order_by(EvolveRoundRow.pro_id, EvolveRoundRow.round)
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
            rounds=[
                {
                    "pro_id": r.pro_id,
                    "round": r.round,
                    "mechanism": r.mechanism,
                    "outcome": r.outcome,
                    "score_pp": r.score_pp,
                    "ranking": r.ranking,
                }
                for r in rounds
            ],
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

    def _window(days: int) -> int:
        if not 1 <= days <= 180:
            raise HTTPException(status_code=422, detail="days must be 1-180")
        return days

    @app.get("/api/funnel")
    async def funnel(
        session: SessionDep, _: AuthDep, days: int = 7, detail: bool = False
    ) -> dict[str, Any]:
        """Audience -> verdict -> LCM intake -> sent -> returned, from our own
        tables. OPERATOR auth: `detail=true` returns themes, mechanisms and
        org_ids, which the automation token has no business exporting — the
        machine gets /api/funnel/worklist instead."""
        _window(days)
        if detail:
            return {"days": days, "pros": await funnel_report.detail(session, days)}
        return await funnel_report.summary(session, days)

    @app.get("/api/funnel/worklist")
    async def funnel_worklist(
        session: SessionDep, _: OutcomeAuthDep, days: int = 7
    ) -> dict[str, Any]:
        """The shipped touches as bare (run_id, pro_id) pairs — the n8n flow's
        work list. Shares the outcomes token because it is the same automation,
        and carries no theme/mechanism/org_id for the reason in funnel.worklist."""
        _window(days)
        return {"days": days, "pros": await funnel_report.worklist(session, days)}

    @app.post("/api/outcomes", status_code=202)
    async def ingest_outcomes(
        body: list[TouchOutcomeIn], session: SessionDep, _: OutcomeAuthDep
    ) -> dict[str, int]:
        """Observed outcomes keyed by a canonical winner or neutral exposure id.
        LCM Personalization intake acknowledgement is not send confirmation;
        see waypoint.outcomes for attribution and idempotency rules."""
        return await ingest_outcomes_batch(session, body)

    @app.post("/api/exposures", status_code=202)
    async def ingest_exposures(
        body: list[ExposureIn], session: SessionDep, _: AuthDep
    ) -> dict[str, int]:
        """Canonical exposure registration, including neutral/control (arm B)
        exposures with no WinnerRow. Winner-linked identity is derived from
        the winner; identity is immutable after registration."""
        return await register_exposures_batch(session, body)

    @app.get("/api/calls", response_model=list[CallItem])
    async def calls(session: SessionDep, _: AuthDep) -> list[CallItem]:
        """Call-channel winners across runs: the operators' own work list.
        These never reach LCM; Pathfinder staff place the calls."""
        return await list_calls(session)

    @app.patch("/api/calls/{winner_id}", response_model=CallItem)
    async def patch_call(
        winner_id: str, body: CallUpdate, session: SessionDep, _: AuthDep
    ) -> CallItem:
        if await update_call(session, winner_id, body.status, body.note) is None:
            raise HTTPException(status_code=404, detail="No call-channel winner with that id")
        return next(c for c in await list_calls(session) if c.winner_id == winner_id)

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
