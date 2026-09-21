"""Authenticated Context Workbench routes hosted by the Railway API."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from waypoint import auth
from waypoint.activity import current_activity, lock_fleet
from waypoint.settings import Settings
from waypoint.tables import WorkbenchCatalogVersionRow
from waypoint.workbench import (
    ContextLayerClient,
    N8NContextClient,
    build_audit_inventory,
    context_layer_coverage,
    org_uuid_from_n8n,
    parse_feature_catalog_csv,
    scrub_pii,
    unwrap_source_payload,
)
from waypoint.workbench_api import (
    CatalogValidateRequest,
    PromotionRequest,
    WorkbenchRunRequest,
    _job_payload,
    _status_payload,
    build_promotion_preview,
    execute_run,
)
from waypoint.workbench_jobs import (
    PostgresWorkbenchStore,
    prune_job_result,
    sanitize_job_request,
)

JobExecutor = Callable[..., Awaitable[dict[str, Any]]]
SourceDispatcher = Callable[[str, WorkbenchRunRequest], Awaitable[None]]
_WORKBENCH_SOURCE_MARKER = "workbench-authoring"


class CatalogVersionRequest(BaseModel):
    id: str = Field(min_length=1)
    kind: Literal["context", "feature"]
    name: str = Field(min_length=1)
    entries: list[dict[str, Any]]
    details: dict[str, Any] = Field(default_factory=dict)


class WorkbenchSourceCallback(BaseModel):
    request_id: str
    organization_id: str
    promotion_id: str
    callback_mode: Literal["workbench"]
    rows: list[dict[str, Any]]


def _safe_catalog_version(body: CatalogVersionRequest) -> dict[str, Any]:
    field = "catalog_override" if body.kind == "context" else "feature_catalog_entries"
    entries = sanitize_job_request({field: body.entries}).get(field, [])
    allowed_details = (
        {"prompt", "feature_catalog_version_id", "confidence_threshold", "tag"}
        if body.kind == "context"
        else {"source_filename"}
    )
    metadata, _ledger = scrub_pii({
        "catalog_name": body.name,
        **{
            key: value for key, value in body.details.items() if key in allowed_details
        },
    })
    return {
        "id": body.id,
        "kind": body.kind,
        "name": str(metadata.pop("catalog_name", "") or f"{body.kind} catalog"),
        "entries": entries,
        "details": metadata,
    }


async def _get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as db:
        yield db


SessionDep = Annotated[AsyncSession, Depends(_get_session)]


class HostedWorkbench:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        executor: JobExecutor = execute_run,
        source_dispatcher: SourceDispatcher | None = None,
    ) -> None:
        self.store = PostgresWorkbenchStore(factory)
        self.executor = executor
        self.source_dispatcher = source_dispatcher
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
                body = WorkbenchRunRequest.model_validate(executable_request)
                if (
                    self.source_dispatcher is not None
                    and body.workbench_mode == "authoring"
                    and body.source_mode in {"snowflake", "both"}
                    and not isinstance(job.state.get("inventory"), list)
                ):
                    if job.state.get("phase") != "collecting_sources":
                        await self.store.checkpoint(
                            job_id, {**job.state, "phase": "collecting_sources"}
                        )
                        await self.source_dispatcher(job_id, body)
                    return

                async def checkpoint(state: dict[str, Any]) -> None:
                    await self.store.checkpoint(job_id, state)

                result = await self.executor(
                    body,
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
        settings: Settings = app.state.settings

        async def dispatch(job_id: str, body: WorkbenchRunRequest) -> None:
            if settings.N8N_CONTEXT_URL_WORKBENCH is None:
                raise RuntimeError("N8N_CONTEXT_URL_WORKBENCH is not configured")
            await N8NContextClient(timeout=settings.N8N_TIMEOUT_SECONDS).start(
                body.identifier,
                str(settings.N8N_CONTEXT_URL_WORKBENCH),
                settings.N8N_TOKEN.get_secret_value(),
                request_id=job_id,
                promotion_id=_WORKBENCH_SOURCE_MARKER,
                callback_mode="workbench",
            )

        service = HostedWorkbench(
            app.state.session_factory,
            getattr(app.state, "workbench_executor", execute_run),
            source_dispatcher=dispatch,
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

    @app.post(
        "/api/context-workbench/source-callback",
        dependencies=[Depends(auth.require_n8n_token)],
    )
    async def complete_workbench_source(
        request: Request, body: WorkbenchSourceCallback
    ) -> dict[str, str]:
        service = get_hosted_workbench(request.app)
        job = await service.store.get(body.request_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Workbench source request not found")
        if (
            body.promotion_id != _WORKBENCH_SOURCE_MARKER
            or job.request.get("identifier") != body.organization_id
            or job.request.get("workbench_mode") != "authoring"
            or job.request.get("source_mode") not in {"snowflake", "both"}
        ):
            raise HTTPException(status_code=409, detail="Workbench source request does not match")
        if isinstance(job.state.get("inventory"), list):
            return {"status": "already_completed", "request_id": body.request_id}
        if job.status != "running" or job.state.get("phase") != "collecting_sources":
            raise HTTPException(status_code=409, detail="Workbench source request is not waiting")

        snowflake, pii_ledger = scrub_pii(
            unwrap_source_payload("snowflake", body.rows)
        )
        scrubbed_sources: dict[str, Any] = {"snowflake": snowflake}
        warnings = [
            f"PII gate removed {len(pii_ledger)} Snowflake fields before persistence"
        ] if pii_ledger else []
        coverage_output: dict[str, Any] | None = None
        if job.request.get("source_mode") == "both":
            settings: Settings = request.app.state.settings
            if settings.CONTEXT_LAYER_BASE_URL is None or settings.CONTEXT_LAYER_API_KEY is None:
                warnings.append("Context Layer was not configured; Snowflake inventory was retained")
            else:
                try:
                    org_uuid = org_uuid_from_n8n(body.rows)
                    if org_uuid is None:
                        raise ValueError("Snowflake context is missing a unique org_uuid")
                    context_payload = await ContextLayerClient().fetch(
                        org_uuid,
                        str(settings.CONTEXT_LAYER_BASE_URL),
                        settings.CONTEXT_LAYER_API_KEY.get_secret_value(),
                    )
                    context_layer, context_pii = scrub_pii(context_payload)
                    scrubbed_sources["context_layer"] = context_layer
                    if context_pii:
                        warnings.append(
                            f"PII gate removed {len(context_pii)} Context Layer fields before persistence"
                        )
                    feature_entries = job.request.get("feature_catalog_entries")
                    feature_keys = sorted({
                        str(item.get("feature"))
                        for item in (
                            feature_entries if isinstance(feature_entries, list) else []
                        )
                        if isinstance(item, Mapping)
                        and item.get("feature")
                    })
                    coverage_output = context_layer_coverage(context_payload, feature_keys)
                except Exception as error:  # noqa: BLE001 - retain the successful source
                    warnings.append(f"Context Layer failed: {type(error).__name__}")

        inventory = build_audit_inventory(scrubbed_sources)
        await service.store.checkpoint(body.request_id, {
            **job.state,
            "phase": "audited",
            "inventory": inventory,
            "coverage_output": coverage_output,
            "warnings": warnings,
        })
        service.schedule(body.request_id)
        return {"status": "queued", "request_id": body.request_id}

    @router.get("/status")
    async def status(request: Request, db: SessionDep) -> dict[str, Any]:
        payload = _status_payload()
        if not payload["env_file_exists"]:
            payload["env_file"] = "Railway service environment"
        return {**payload, "activity": await current_activity(db)}

    @router.post("/catalog/validate")
    async def validate_catalog(request: Request, body: CatalogValidateRequest) -> dict[str, Any]:
        try:
            entries = parse_feature_catalog_csv(body.csv_text)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        store = get_hosted_workbench(request.app).store
        safe_entries = _safe_catalog_version(CatalogVersionRequest(
            id="feature-candidate",
            kind="feature",
            name=body.name,
            entries=entries,
            details={"source_filename": body.filename},
        ))["entries"]
        canonical = json.dumps(safe_entries, sort_keys=True, separators=(",", ":"))
        version_id = f"features-{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"
        safe_version = _safe_catalog_version(CatalogVersionRequest(
            id=version_id,
            kind="feature",
            name=body.name,
            entries=safe_entries,
            details={"source_filename": body.filename},
        ))
        version: dict[str, Any] | None
        try:
            version = await store.save_catalog_version(safe_version)
        except ValueError:
            version = await store.read_catalog_version(version_id)
            if version is None:
                raise
        return {
            **version,
            "source_filename": body.filename,
            "csv_text": body.csv_text,
        }

    @router.get("/catalogs")
    async def list_catalogs(
        request: Request,
        kind: Literal["context", "feature"] | None = None,
    ) -> list[dict[str, Any]]:
        return await get_hosted_workbench(request.app).store.list_catalog_versions(kind)

    @router.get("/catalogs/{version_id}")
    async def get_catalog(request: Request, version_id: str) -> dict[str, Any]:
        version = await get_hosted_workbench(request.app).store.read_catalog_version(
            version_id
        )
        if version is None:
            raise HTTPException(status_code=404, detail="Catalog version not found")
        return version

    @router.post("/catalogs", status_code=201)
    async def save_catalog(
        request: Request,
        body: CatalogVersionRequest,
    ) -> dict[str, Any]:
        try:
            return await get_hosted_workbench(request.app).store.save_catalog_version(
                _safe_catalog_version(body)
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

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
        job = await get_hosted_workbench(request.app).store.get(body.evaluation_job_id)
        assert job is not None
        context = await db.get(
            WorkbenchCatalogVersionRow, bundle["context_catalog_version_id"]
        )
        feature = await db.get(
            WorkbenchCatalogVersionRow, bundle["feature_catalog_version_id"]
        )
        if (
            context is None
            or context.kind != "context"
            or dict(context.details or {}).get("recovered_metadata") is True
        ):
            raise HTTPException(status_code=422, detail="Context catalog version is unavailable")
        if (
            feature is None
            or feature.kind != "feature"
            or dict(feature.details or {}).get("recovered_metadata") is True
        ):
            raise HTTPException(status_code=422, detail="Feature catalog version is unavailable")
        if context.entries != job.request.get("catalog_override"):
            raise HTTPException(status_code=409, detail="Context catalog version does not match evaluation")
        if feature.entries != job.request.get("feature_catalog_entries"):
            raise HTTPException(status_code=409, detail="Feature catalog version does not match evaluation")
        if dict(context.details or {}).get("feature_catalog_version_id") != feature.id:
            raise HTTPException(
                status_code=409,
                detail="Context catalog was reviewed with a different feature catalog version",
            )
        await get_hosted_workbench(request.app).store.promote(bundle, session=db)
        await db.commit()
        return payload

    app.include_router(router)
