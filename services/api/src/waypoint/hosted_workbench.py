"""Authenticated Context Workbench routes hosted by the Railway API."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from waypoint import auth
from waypoint.activity import current_activity, lock_fleet
from waypoint.workbench import parse_feature_catalog_csv
from waypoint.workbench_api import (
    CatalogValidateRequest,
    PromotionRequest,
    WorkbenchRunRequest,
    _job_payload,
    _status_payload,
    build_promotion_preview,
    execute_run,
)
from waypoint.workbench_jobs import PostgresWorkbenchStore, prune_job_result

JobExecutor = Callable[..., Awaitable[dict[str, Any]]]


async def _get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as db:
        yield db


SessionDep = Annotated[AsyncSession, Depends(_get_session)]


class HostedWorkbench:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        executor: JobExecutor = execute_run,
    ) -> None:
        self.store = PostgresWorkbenchStore(factory)
        self.executor = executor
        self.tasks: dict[str, asyncio.Task[None]] = {}

    async def run_job(self, job_id: str) -> None:
        async with self.store.factory() as claim_session:
            await claim_session.execute(
                text("SELECT pg_advisory_lock(hashtext(:job_id))"),
                {"job_id": job_id},
            )
            try:
                job = await self.store.get(job_id)
                if job is None or job.status not in {"queued", "running"}:
                    return
                await self.store.set_status(job_id, "running")
                executable_request = {
                    key: value
                    for key, value in job.request.items()
                    if key not in {"catalog_approved_count", "catalog_pii_removed_count"}
                }

                async def checkpoint(state: dict[str, Any]) -> None:
                    await self.store.checkpoint(job_id, state)

                result = await self.executor(
                    WorkbenchRunRequest.model_validate(executable_request),
                    resume_state=job.state,
                    checkpoint=checkpoint,
                )
                authoring = result.get("outputs", {}).get("authoring", {})
                status = (
                    "needs_review"
                    if int(authoring.get("review_exception_count", 0)) > 0
                    else "completed"
                )
                await self.store.complete(
                    job_id, prune_job_result(result), status=status
                )
            except asyncio.CancelledError:
                await self.store.set_status(job_id, "queued")
                raise
            except Exception as error:  # noqa: BLE001 - durable boundary stores safe text
                await self.store.fail(job_id, str(error))
            finally:
                await claim_session.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:job_id))"),
                    {"job_id": job_id},
                )

    def schedule(self, job_id: str) -> None:
        current = self.tasks.get(job_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self.run_job(job_id))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _task: self.tasks.pop(job_id, None))

    async def resume(self) -> None:
        for job in await self.store.resumable():
            self.schedule(job.id)

    async def shutdown(self) -> None:
        active = list(self.tasks.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)


def get_hosted_workbench(app: FastAPI) -> HostedWorkbench:
    service = getattr(app.state, "workbench_service", None)
    if service is None:
        service = HostedWorkbench(
            app.state.session_factory,
            getattr(app.state, "workbench_executor", execute_run),
        )
        app.state.workbench_service = service
    return service


def install_hosted_workbench(
    app: FastAPI, *, executor: JobExecutor = execute_run
) -> None:
    app.state.workbench_executor = executor
    app.state.workbench_service = None
    router = APIRouter(
        prefix="/api/context-workbench",
        dependencies=[Depends(auth.require_session)],
    )

    @router.get("/status")
    async def status(request: Request, db: SessionDep) -> dict[str, Any]:
        payload = _status_payload()
        if not payload["env_file_exists"]:
            payload["env_file"] = "Railway service environment"
        return {**payload, "activity": await current_activity(db)}

    @router.post("/catalog/validate")
    async def validate_catalog(body: CatalogValidateRequest) -> dict[str, Any]:
        try:
            entries = parse_feature_catalog_csv(body.csv_text)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
        return {
            "id": f"features-{hashlib.sha256(canonical.encode()).hexdigest()[:16]}",
            "name": body.name,
            "source_filename": body.filename,
            "entries": entries,
            "csv_text": body.csv_text,
        }

    @router.post("/jobs", status_code=202)
    async def start_job(
        request: Request,
        body: WorkbenchRunRequest,
        db: SessionDep,
    ) -> dict[str, Any]:
        settings = request.app.state.settings
        await lock_fleet(db, settings)
        activity = await current_activity(db)
        if activity == "waypoint":
            raise HTTPException(status_code=409, detail="A Waypoint run is active")
        if activity == "workbench":
            raise HTTPException(status_code=409, detail="Context Workbench is already running")
        service = get_hosted_workbench(request.app)
        job = await service.store.create(body.model_dump(), session=db)
        await db.commit()
        service.schedule(job.id)
        return _job_payload(job)

    @router.get("/jobs/latest")
    async def latest_job(request: Request) -> dict[str, Any]:
        job = await get_hosted_workbench(request.app).store.latest()
        if job is None:
            raise HTTPException(status_code=404, detail="No Workbench job exists")
        return _job_payload(job)

    @router.get("/jobs/{job_id}")
    async def get_job(request: Request, job_id: str) -> dict[str, Any]:
        job = await get_hosted_workbench(request.app).store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Workbench job not found")
        return _job_payload(job)

    @router.post("/jobs/{job_id}/resume")
    async def resume_job(
        request: Request,
        job_id: str,
        db: SessionDep,
    ) -> dict[str, Any]:
        service = get_hosted_workbench(request.app)
        job = await service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Workbench job not found")
        if job.status not in {"completed", "needs_review"}:
            await lock_fleet(db, request.app.state.settings)
            activity = await current_activity(db)
            if activity == "waypoint":
                raise HTTPException(status_code=409, detail="A Waypoint run is active")
            if activity == "workbench" and job.status not in {"queued", "running"}:
                raise HTTPException(
                    status_code=409,
                    detail="Another Context Workbench job is already running",
                )
            if job.status not in {"queued", "running"}:
                await service.store.set_status(job_id, "queued")
            service.schedule(job_id)
        refreshed = await service.store.get(job_id)
        assert refreshed is not None
        return _job_payload(refreshed)

    async def preview(request: Request, job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        job = await get_hosted_workbench(request.app).store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Evaluation job not found")
        return build_promotion_preview(job)

    @router.post("/promotions/preview")
    async def preview_promotion(request: Request, body: PromotionRequest) -> dict[str, Any]:
        _bundle, payload = await preview(request, body.evaluation_job_id)
        return payload

    @router.post("/promotions")
    async def promote(
        request: Request,
        body: PromotionRequest,
        db: SessionDep,
    ) -> dict[str, Any]:
        await lock_fleet(db, request.app.state.settings)
        activity = await current_activity(db)
        if activity != "idle":
            label = "Waypoint run" if activity == "waypoint" else "Context Workbench job"
            raise HTTPException(status_code=409, detail=f"A {label} is active")
        bundle, payload = await preview(request, body.evaluation_job_id)
        await get_hosted_workbench(request.app).store.promote(bundle, session=db)
        await db.commit()
        return payload

    app.include_router(router)
