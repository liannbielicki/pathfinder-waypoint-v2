"""Context Workbench execution shared by local and hosted API routes."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator

from waypoint.context_promotion import (
    DEFAULT_PROMOTION_ROOT,
    PromotionStore,
    build_promotion_bundle,
    promotion_csv,
)
from waypoint.llm import extract_json
from waypoint.workbench import (
    ContextLayerClient,
    N8NContextClient,
    WorkbenchStage,
    build_audit_inventory,
    build_evolve_prompt,
    build_prompt_context,
    compile_catalog_contract,
    compile_context,
    context_layer_coverage,
    is_pii_variable_key,
    load_catalog,
    org_uuid_from_n8n,
    parse_candidates,
    parse_feature_catalog_csv,
    prioritize_review_exceptions,
    redact,
    resolve_product_cards,
    run_model,
    scrub_pii,
    shape,
    unwrap_source_payload,
    validate_catalog_entries,
    workbench_env,
)
from waypoint.workbench_jobs import (
    WorkbenchJob,
    WorkbenchJobStore,
    prune_job_result,
    sanitize_job_request,
)

_AUTHORING_TOKEN_BUDGET = 150_000
_AUTHORING_MAX_TOKENS = 20_000
_AUTHORING_BATCH_SIZE = 15
_AUTHORING_MAX_ATTEMPTS = 3
_AUTHORING_SELECTION_POLICY = """VARIABLE SELECTION POLICY:
- Compare variables within the same metric family before assigning ranks or dispositions.
- Prefer T28 as the standard recent-behavior window. T30 is acceptable when T28
  is unavailable or measures something materially different.
- Set T1 and T7 variants to `exclude`; they are too short and usually duplicate
  the durable signal available from T28.
- Set T90 variants to `deprioritize` by default. Include T90 only when it adds a
  materially different long-term trend, baseline, or rare high-value signal that
  T28 cannot provide.
- Include count and amount variants together only when frequency and financial
  magnitude would independently change the diagnosis or recommendation. Otherwise,
  include the single most decision-useful representative and exclude redundant variants.
- When the variable name and observed type do not establish which family member is
  more useful, use `deprioritize` and state the uncertainty; do not invent a distinction.
- Keep every source key as its own metadata object. Use disposition to narrow the
  runtime context instead of deleting or merging source variables."""
_REQUIRED_AUTHORING_FIELDS = {
    "key", "canonical_key", "value_category", "related_features",
    "usefulness_rank", "disposition", "aggregate_prompt", "confidence",
    "uncertainty_reason",
}
_AI_CATALOG_FIELDS = {
    "key", "canonical_key", "value_category", "related_features",
    "usefulness_rank", "aggregate_prompt", "disposition", "review_status",
    "confidence", "uncertainty_reason", "approval_status", "exclusion_reason",
}


class WorkbenchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(min_length=1)
    identifier_type: Literal["organization_id"] = "organization_id"
    source_mode: Literal["snowflake", "context_layer", "both"] = "snowflake"
    context_base_url: str | None = None
    context_api_key: str | None = None
    n8n_webhook_url: str | None = None
    n8n_webhook_token: str | None = None
    ai_api_key: str | None = None
    model: str | None = None
    context_policy: Literal["baseline", "proposed", "compare"] = "compare"
    enrichment: Literal["none", "catalog"] = "catalog"
    channels: list[str] = Field(default_factory=lambda: ["email", "sms"])
    journey_window: str = "churn_risk_open"
    candidate_count: int = Field(default=3, ge=1, le=5)
    workbench_mode: Literal["runtime", "authoring", "compile", "evaluate"] = "runtime"
    authoring_prompt: str = "Create a token-efficient, AI-only variable catalog using key, canonical_key, value_category, related_features, usefulness_rank, and aggregate_prompt. Map each variable only to exact feature keys from the supplied catalog. Do not create human descriptions, time metadata, or recommendations. Return valid JSON only."
    feature_catalog_csv: str | None = None
    feature_catalog_entries: list[dict[str, Any]] | None = None
    feature_catalog_version_id: str | None = None
    catalog_override: list[dict[str, Any]] | None = None
    catalog_version_id: str | None = None
    catalog_version_name: str | None = None
    catalog_version_saved_at: str | None = None

    @field_validator("identifier")
    @classmethod
    def validate_organization_id(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) not in {5, 6} or not normalized.isdigit():
            raise ValueError("identifier must be a five- or six-digit organization ID")
        return normalized


class CatalogValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    csv_text: str = Field(min_length=1)


class PromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluation_job_id: str = Field(min_length=1)


def _status_payload() -> dict[str, Any]:
    configured = workbench_env()
    env_path = Path(__file__).parents[2] / ".env"
    return {
        "env_file": str(env_path),
        "env_file_exists": env_path.exists(),
        "configured": {
            "snowflake": bool(configured["n8n_webhook_url"] and configured["n8n_webhook_token"]),
            "context_layer": bool(configured["context_base_url"] and configured["context_api_key"]),
            "ai": bool(configured["ai_api_key"]),
            "model": bool(configured["model"]),
        },
    }


def _stage(name: str, data: Any, started: float, **kwargs: Any) -> WorkbenchStage:
    return WorkbenchStage(
        name=name,
        data=data,
        duration_ms=round((time.perf_counter() - started) * 1000),
        **kwargs,
    )


def machine_catalog(
    entries: list[dict[str, Any]] | None, *, trusted_approval: bool = True
) -> list[dict[str, Any]]:
    """Project saved or drafted catalog entries to the AI-only runtime contract."""
    fields = _AI_CATALOG_FIELDS if trusted_approval else _REQUIRED_AUTHORING_FIELDS
    return [
        {key: value for key, value in entry.items() if key in fields}
        for entry in (entries or [])
        if isinstance(entry, dict) and entry.get("key")
    ]


def prepare_promotion_entries(
    raw_entries: list[dict[str, Any]], feature_keys: set[str]
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """PII-gate reviewed entries before validation without dropping other information."""
    catalog_entries = [
        entry for entry in raw_entries if isinstance(entry, dict) and entry.get("key")
    ]
    approved = [
        entry for entry in catalog_entries if str(entry.get("disposition")) == "include"
    ]
    safe_entries = [
        entry
        for entry in catalog_entries
        if not is_pii_variable_key(str(entry.get("key") or ""))
        and not is_pii_variable_key(str(entry.get("canonical_key") or ""))
    ]
    pii_removed = sum(
        1
        for entry in approved
        if is_pii_variable_key(str(entry.get("key") or ""))
        or is_pii_variable_key(str(entry.get("canonical_key") or ""))
    )
    validated, warnings = validate_catalog_entries(
        machine_catalog(safe_entries), feature_keys
    )
    entries = [
        {**entry, "source_table": str(original.get("source_table") or "")}
        for entry, original in zip(validated, safe_entries, strict=True)
    ]
    return entries, {
        "approved": len(approved),
        "pii_removed": pii_removed,
    }, warnings


def _compact_feature_catalog(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Keep only the feature evidence needed for exact authoring-time mapping."""
    compact: list[dict[str, str]] = []
    for entry in entries:
        feature = str(entry.get("feature") or "").strip()
        if not feature:
            continue
        item = {"feature": feature}
        product_area = entry.get("Product Area") or entry.get("product_area") or entry.get("category")
        description = entry.get("Value Statement") or entry.get("description")
        if product_area:
            item["product_area"] = str(product_area).strip()
        if description:
            item["description"] = str(description).strip()
        compact.append(item)
    return compact


async def execute_run(
    body: WorkbenchRunRequest,
    *,
    resume_state: Mapping[str, Any] | None = None,
    checkpoint: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
) -> dict[str, Any]:
    configured = workbench_env()
    context_base_url = body.context_base_url or configured["context_base_url"]
    context_api_key = body.context_api_key or configured["context_api_key"]
    ai_api_key = body.ai_api_key or configured["ai_api_key"]
    model = body.model or configured["model"] or "claude-sonnet-5"
    if body.workbench_mode != "compile" and not ai_api_key:
        raise HTTPException(status_code=422, detail="ANTHROPIC_API_KEY or LLM_API_KEY is required in services/api/.env")
    stages: list[WorkbenchStage] = []
    warnings = [
        str(item) for item in (
            resume_state.get("warnings", []) if isinstance(resume_state, Mapping) else []
        )
    ]
    resumed_inventory = (
        resume_state.get("inventory") if isinstance(resume_state, Mapping) else None
    )
    inventory_resume = (
        body.workbench_mode == "authoring"
        and isinstance(resumed_inventory, list)
        and all(isinstance(item, dict) for item in resumed_inventory)
    )
    resumed_sources = (
        resume_state.get("scrubbed_sources")
        if isinstance(resume_state, Mapping)
        else None
    )
    source_resume = (
        body.workbench_mode == "evaluate"
        and isinstance(resumed_sources, Mapping)
        and bool(resumed_sources)
    )
    resumed = inventory_resume or source_resume
    sanitized_request = sanitize_job_request(body.model_dump())
    safe_catalog_override = sanitized_request.get("catalog_override")
    stages.append(
        WorkbenchStage(
            name="input",
            data=sanitized_request,
        )
    )

    if body.feature_catalog_entries is not None:
        feature_catalog_entries = body.feature_catalog_entries
    elif body.feature_catalog_csv:
        feature_catalog_entries = parse_feature_catalog_csv(body.feature_catalog_csv)
    else:
        feature_catalog_entries = [
            {"feature": key, **entry} for key, entry in load_catalog().items()
        ]
    feature_catalog_entries = sanitize_job_request({
        "feature_catalog_entries": feature_catalog_entries
    }).get("feature_catalog_entries", [])
    feature_keys = {
        str(entry.get("feature")) for entry in feature_catalog_entries if entry.get("feature")
    }

    if body.workbench_mode == "compile":
        validated, validation_warnings = validate_catalog_entries(
            machine_catalog(safe_catalog_override), feature_keys
        )
        warnings.extend(validation_warnings)
        compiled = compile_catalog_contract(
            validated,
            feature_catalog_version_id=body.feature_catalog_version_id,
            context_catalog_version_id=body.catalog_version_id,
            feature_catalog_entries=feature_catalog_entries,
        )
        stages.append(WorkbenchStage(
            name="compiled_context",
            data=redact(compiled),
            summary="approved include rules compiled without source collection or AI",
        ))
        return {
            "stages": [stage.model_dump() for stage in stages],
            "warnings": warnings,
            "outputs": {"compiled": compiled},
        }

    n8n_webhook_url = body.n8n_webhook_url or configured["n8n_webhook_url"]
    n8n_webhook_token = body.n8n_webhook_token or configured["n8n_webhook_token"]
    if not resumed and body.source_mode in ("context_layer", "both") and (not context_base_url or not context_api_key):
        raise HTTPException(status_code=422, detail="Context Layer mode requires CONTEXT_LAYER_BASE_URL and CONTEXT_LAYER_API_KEY in services/api/.env")
    if not resumed and body.source_mode in ("snowflake", "both") and (not n8n_webhook_url or not n8n_webhook_token):
        raise HTTPException(status_code=422, detail="Snowflake mode requires N8N_CONTEXT_URL_WORKBENCH and N8N_TOKEN in services/api/.env")

    sources: dict[str, Any] = {}
    source_errors: dict[str, str] = {}
    if not resumed and body.source_mode in ("snowflake", "both"):
        started = time.perf_counter()
        try:
            result = await N8NContextClient().fetch(
                body.identifier, n8n_webhook_url or "", n8n_webhook_token or ""
            )
            sources["snowflake"] = result
            row_count = len(result) if isinstance(result, list) else None
            stages.append(_stage("snowflake_context", shape(result), started, summary="experimental variable inventory captured; values withheld", metrics={"row_count": row_count}))
        except Exception as error:  # noqa: BLE001 - source boundary reports safe failure state
            source_errors["snowflake"] = str(error)
            stages.append(_stage("snowflake_context", None, started, status="failed", error=str(error)))
    if not resumed and body.source_mode in ("context_layer", "both"):
        started = time.perf_counter()
        context_identifier = body.identifier
        if body.source_mode == "both":
            context_identifier = org_uuid_from_n8n(sources.get("snowflake")) or ""
        try:
            if not context_identifier:
                raise ValueError("Context Layer requires an ORG_UUID; the n8n result did not provide one")
            result = await ContextLayerClient().fetch(
                context_identifier, context_base_url or "", context_api_key or ""
            )
            sources["context_layer"] = result
            stages.append(_stage("context_layer_context", shape(result), started, summary="full organization feature payload captured; values withheld"))
        except Exception as error:  # noqa: BLE001 - source boundary reports safe failure state
            source_errors["context_layer"] = str(error)
            stages.append(_stage("context_layer_context", None, started, status="failed", error=str(error)))
    if not resumed and not sources:
        raise HTTPException(
            status_code=502,
            detail={"message": "All selected context sources failed", "sources": source_errors},
        )

    scrubbed_sources: dict[str, Any] = {}
    if inventory_resume:
        assert isinstance(resumed_inventory, list)
        inventory = [dict(item) for item in resumed_inventory]
        audit_output = {
            "inventory": inventory,
            "total_variables": len(inventory),
            "basis": {"source_evidence": "checkpoint", "semantic_judgments": "inferred", "population_metrics": "unavailable"},
        }
        coverage_output = (
            resume_state.get("coverage_output")
            if isinstance(resume_state, Mapping)
            else None
        )
        stages.append(WorkbenchStage(
            name="source_checkpoint",
            data={"inventory_count": len(inventory)},
            summary="resumed from the durable scrubbed inventory without refetching sources",
        ))
    elif source_resume:
        assert isinstance(resumed_sources, Mapping)
        scrubbed, resumed_pii = scrub_pii({"sources": dict(resumed_sources)})
        resumed_payload = scrubbed.get("sources")
        if not isinstance(resumed_payload, dict) or not resumed_payload:
            raise HTTPException(status_code=502, detail="Scrubbed source checkpoint is empty")
        scrubbed_sources = resumed_payload
        if resumed_pii:
            warnings.append(
                f"PII gate removed {len(resumed_pii)} checkpoint fields before evaluation"
            )
        inventory = build_audit_inventory(scrubbed_sources)
        audit_output = {
            "inventory": inventory,
            "total_variables": len(inventory),
            "basis": {"source_evidence": "checkpoint", "semantic_judgments": "inferred", "population_metrics": "unavailable"},
        }
        coverage_output = (
            resume_state.get("coverage_output")
            if isinstance(resume_state, Mapping)
            else None
        )
        stages.append(WorkbenchStage(
            name="source_checkpoint",
            data={"sources": sorted(scrubbed_sources)},
            summary="resumed from PII-scrubbed callback sources without refetching",
        ))
    else:
        started = time.perf_counter()
        raw = {"sources": sources, "source_errors": source_errors}
        stages.append(_stage("raw_context", shape(raw), started, summary="structure only; raw values are intentionally withheld here; see scrubbed_context for retained non-PII values"))

        started = time.perf_counter()
        normalized_sources = {
            source: unwrap_source_payload(source, payload) for source, payload in sources.items()
        }
        normalized = {"source": "combined" if len(sources) > 1 else next(iter(sources)), "grain": "organization", "sources": normalized_sources}
        stages.append(_stage("normalized_context", shape(normalized), started, summary="sources normalized into one trace; values remain withheld until after the PII gate"))

        started = time.perf_counter()
        pii_ledger: list[dict[str, str]] = []
        for source, payload in normalized_sources.items():
            cleaned, ledger = scrub_pii(payload)
            scrubbed_sources[source] = cleaned
            pii_ledger.extend({**item, "path": f"sources.{source}.{item['path']}"} for item in ledger)
        scrubbed = {"sources": scrubbed_sources}
        stages.append(
            _stage(
                "pii_gate",
                {"removed": pii_ledger, "removed_count": len(pii_ledger)},
                started,
                summary="mandatory fail-closed PII removal completed",
            )
        )
        stages.append(WorkbenchStage(name="scrubbed_context", data=redact(scrubbed)))

        inventory = build_audit_inventory(scrubbed_sources)
        audit_output = {
            "inventory": inventory,
            "total_variables": len(inventory),
            "basis": {"source_evidence": "observed", "semantic_judgments": "inferred", "population_metrics": "unavailable"},
        }
        coverage_output = None
        context_payload = sources.get("context_layer")
        if isinstance(context_payload, dict):
            coverage_output = context_layer_coverage(context_payload, sorted(feature_keys))
            stages.append(WorkbenchStage(name="context_layer_coverage", data=coverage_output))

    if body.workbench_mode == "authoring":
        assert ai_api_key is not None
        feature_catalog = "\n\nVERIFIED FEATURE CATALOG:\n" + json.dumps(
            _compact_feature_catalog(feature_catalog_entries), separators=(",", ":")
        )
        unique_inventory: dict[str, dict[str, Any]] = {}
        for item in inventory:
            if item["observed_state"] != "removed_pii":
                unique_inventory.setdefault(item["key"], item)
        safe_inventory = list(unique_inventory.values())
        resumed_entries = (
            resume_state.get("entries", []) if isinstance(resume_state, Mapping) else []
        )
        saved_catalog, saved_warnings = validate_catalog_entries(
            [
                *machine_catalog(safe_catalog_override),
                *machine_catalog(resumed_entries if isinstance(resumed_entries, list) else []),
            ],
            feature_keys,
        )
        warnings.extend(saved_warnings)
        saved_by_key = {str(item["key"]): item for item in saved_catalog}
        saved_catalog = list(saved_by_key.values())
        total_keys = len(safe_inventory)
        inventory_keys = {item["key"] for item in safe_inventory}
        existing_keys = {
            str(item.get("key"))
            for item in saved_catalog
            if item.get("key") and str(item.get("key")) in inventory_keys
        }
        safe_inventory = [item for item in safe_inventory if item["key"] not in existing_keys]
        draft_entries: list[Any] = []
        authoring_tokens = int(
            resume_state.get("output_tokens", 0)
            if isinstance(resume_state, Mapping)
            else 0
        )
        resumed_attempts = (
            resume_state.get("attempts", {}) if isinstance(resume_state, Mapping) else {}
        )
        attempts = {
            str(key): int(value)
            for key, value in (
                resumed_attempts.items() if isinstance(resumed_attempts, Mapping) else []
            )
        }
        revised_keys = {
            str(key) for key in (
                resume_state.get("revised_keys", [])
                if isinstance(resume_state, Mapping)
                else []
            )
        }

        resumed_pending = (
            resume_state.get("pending_queue") if isinstance(resume_state, Mapping) else None
        )
        safe_by_key = {str(item["key"]): item for item in safe_inventory}
        if isinstance(resumed_pending, list):
            ordered_inventory = [
                safe_by_key[str(key)] for key in resumed_pending if str(key) in safe_by_key
            ]
        else:
            ordered_inventory = safe_inventory
        pending_batches = [
            ordered_inventory[index:index + _AUTHORING_BATCH_SIZE]
            for index in range(0, len(ordered_inventory), _AUTHORING_BATCH_SIZE)
        ]

        async def save_checkpoint(
            entries: list[dict[str, Any]], *, phase: str, pending: int
        ) -> None:
            if checkpoint is None:
                return
            result = checkpoint({
                "entries": entries,
                "revised_keys": sorted(revised_keys),
                "output_tokens": authoring_tokens,
                "phase": phase,
                "pending_keys": pending,
                "pending_queue": [
                    str(item["key"]) for batch in pending_batches for item in batch
                ],
                "attempts": dict(attempts),
                "inventory": inventory,
                "coverage_output": coverage_output,
                "warnings": warnings,
            })
            if inspect.isawaitable(result):
                await result

        def current_checkpoint_catalog() -> list[dict[str, Any]]:
            catalog, _ = validate_catalog_entries([
                *machine_catalog(saved_catalog),
                *machine_catalog(draft_entries, trusted_approval=False),
            ], feature_keys)
            return catalog

        try:
            await save_checkpoint(
                saved_catalog,
                phase="audited",
                pending=sum(len(batch) for batch in pending_batches),
            )
            batch_number = 0
            while pending_batches:
                batch_number += 1
                if authoring_tokens >= _AUTHORING_TOKEN_BUDGET:
                    warnings.append("authoring token budget reached; remaining catalog keys were not drafted")
                    break
                batch = pending_batches.pop(0)
                for item in batch:
                    attempts[item["key"]] = attempts.get(item["key"], 0) + 1
                authoring_prompt = f"""{body.authoring_prompt}

For each variable key below, return exactly one object with only these fields.
Do not omit any requested key and do not return keys that were not requested:
key, canonical_key, value_category, related_features, usefulness_rank,
disposition, aggregate_prompt, confidence, uncertainty_reason.
`key` must exactly match the input. `canonical_key` must be short, stable, and
token-efficient; remove source namespaces and join-only suffixes such as
`.ORGANIZATION_ID`, but do not merge distinct concepts. `value_category` must
be a compact machine category such as `feature_adoption`, `product_engagement`,
`activity`, `lifecycle`, `risk`, `revenue`, `payments`, `communication`,
`plan`, `profile`, `operations`, or `outcome`. `related_features` must contain
only exact feature keys from the supplied feature catalog, with no more than
three entries ordered by likely relevance; use [] when unverified.
`usefulness_rank` is an integer from 1 (little value for understanding product
engagement or journey position) to 5 (high value for understanding product
engagement or journey position). This is a variable-selection rank, not a
claim about the individual Pro.
`disposition` must be `include`, `deprioritize`, or `exclude`. Prefer inclusion
only when the variable can materially help identify a core issue or useful
solution. This is an inferred draft for human review, never an observed fact.
{_AUTHORING_SELECTION_POLICY}
`aggregate_prompt` must be null unless usefulness_rank is 4 or 5. For rank 4 or 5,
write exactly one sentence addressed to Claude requesting a cohort-level
statistic computed across comparable Pros, not a per-Pro calculation. The
sentence should explain how the resulting statistic will position each Pro,
for example: "Calculate the matched cohort mean, percentiles, sample size, and
standard deviation for this metric so the runtime can classify where each Pro
sits within that cohort." Do not invent values,
relationships, or product claims. Return JSON only.
`confidence` must be a number from 0.00 through 1.00 expressing your confidence
in the complete metadata object. `uncertainty_reason` must be one concise sentence
when confidence is below 0.80 and null otherwise.

VARIABLE INVENTORY BATCH {batch_number} ({len(batch)} keys; no organization values):
{batch}{feature_catalog}"""
                stages.append(WorkbenchStage(name=f"authoring_prompt_{batch_number}", data=authoring_prompt, summary="AI catalog-drafting prompt using scrubbed variable inventory only"))
                remaining_tokens = _AUTHORING_TOKEN_BUDGET - authoring_tokens
                text, metrics = await run_model(authoring_prompt, api_key=ai_api_key, model=model, stage=f"authoring_generation_{batch_number}", system="You draft deterministic catalog metadata for human review. Return only valid JSON.", max_tokens=min(_AUTHORING_MAX_TOKENS, remaining_tokens), effort="low")
                authoring_tokens += int(metrics.get("output_tokens", 0))
                stages.append(WorkbenchStage(name=f"authoring_response_{batch_number}", data=text, metrics=metrics))
                if metrics.get("stop_reason") == "max_tokens":
                    if len(batch) > 1:
                        midpoint = len(batch) // 2
                        pending_batches[0:0] = [batch[:midpoint], batch[midpoint:]]
                        warnings.append(
                            f"authoring batch {batch_number} hit max_tokens; split and requeued"
                        )
                    else:
                        key = str(batch[0]["key"])
                        if attempts[key] < _AUTHORING_MAX_ATTEMPTS:
                            pending_batches.append(batch)
                            warnings.append(
                                f"{key}: hit max_tokens; requeued attempt {attempts[key] + 1}"
                            )
                        else:
                            warnings.append(
                                f"{key}: hit max_tokens after {attempts[key]} attempts"
                            )
                    await save_checkpoint(
                        current_checkpoint_catalog(),
                        phase="drafting",
                        pending=sum(len(item) for item in pending_batches),
                    )
                    continue
                try:
                    draft = extract_json(text)
                except ValueError as parse_error:
                    repair_prompt = f"""Repair the following malformed catalog response. Return only a valid JSON array. Preserve the intended entries and fields; do not add commentary.

MALFORMED_RESPONSE:
{text}"""
                    repair_text, repair_metrics = await run_model(repair_prompt, api_key=ai_api_key, model=model, stage=f"authoring_repair_{batch_number}", system="You repair malformed JSON. Return only valid JSON.", max_tokens=min(4000, max(1, _AUTHORING_TOKEN_BUDGET - authoring_tokens)), effort="low")
                    authoring_tokens += int(repair_metrics.get("output_tokens", 0))
                    stages.append(WorkbenchStage(name=f"authoring_repair_{batch_number}", data=repair_text, metrics=repair_metrics, summary=f"JSON repair retry after: {parse_error}"))
                    if repair_metrics.get("stop_reason") == "max_tokens" and len(batch) > 1:
                        midpoint = len(batch) // 2
                        pending_batches[0:0] = [batch[:midpoint], batch[midpoint:]]
                        warnings.append(
                            f"authoring repair {batch_number} hit max_tokens; split and requeued"
                        )
                        await save_checkpoint(
                            current_checkpoint_catalog(),
                            phase="drafting",
                            pending=sum(len(item) for item in pending_batches),
                        )
                        continue
                    try:
                        draft = extract_json(repair_text)
                    except ValueError:
                        draft = []
                        for item in batch:
                            single_prompt = f"""{body.authoring_prompt}

Return exactly one valid JSON object for this variable using only these fields:
key, canonical_key, value_category, related_features, usefulness_rank, disposition,
aggregate_prompt, confidence, uncertainty_reason. Do not return markdown or commentary.
{_AUTHORING_SELECTION_POLICY}
VARIABLE: {item}"""
                            single_text, single_metrics = await run_model(single_prompt, api_key=ai_api_key, model=model, stage=f"authoring_single_{batch_number}", system="Return exactly one valid JSON object and nothing else.", max_tokens=min(1200, max(1, _AUTHORING_TOKEN_BUDGET - authoring_tokens)), effort="low")
                            authoring_tokens += int(single_metrics.get("output_tokens", 0))
                            try:
                                single_draft = extract_json(single_text)
                            except ValueError:
                                continue
                            draft.extend(single_draft if isinstance(single_draft, list) else [single_draft])
                if isinstance(draft, dict):
                    draft = next((draft[key] for key in ("entries", "catalog", "items") if isinstance(draft.get(key), list)), [draft])
                if not isinstance(draft, list):
                    raise TypeError("authoring response was not a catalog list")
                requested = {item["key"]: item for item in batch}
                complete = [
                    item
                    for item in draft
                    if isinstance(item, dict)
                    and str(item.get("key")) in requested
                    and _REQUIRED_AUTHORING_FIELDS <= item.keys()
                ]
                draft_entries.extend(complete)
                complete_keys = {str(item["key"]) for item in complete}
                retry = [
                    item
                    for key, item in requested.items()
                    if key not in complete_keys and attempts[key] < _AUTHORING_MAX_ATTEMPTS
                ]
                exhausted = [
                    key
                    for key in requested
                    if key not in complete_keys and attempts[key] >= _AUTHORING_MAX_ATTEMPTS
                ]
                if retry:
                    pending_batches.append(retry)
                    warnings.append(
                        f"authoring batch {batch_number} requeued {len(retry)} incomplete variables"
                    )
                if exhausted:
                    warnings.append(
                        f"authoring batch {batch_number} left {len(exhausted)} variables incomplete after {_AUTHORING_MAX_ATTEMPTS} attempts"
                    )
                await save_checkpoint(
                    current_checkpoint_catalog(),
                    phase="drafting",
                    pending=sum(len(pending_batch) for pending_batch in pending_batches),
                )
            stages.append(WorkbenchStage(name="authoring_parse", data=draft_entries, summary="Draft catalog JSON parsed; not approved for runtime"))
        except Exception as error:  # noqa: BLE001 - model boundary becomes a visible draft failure
            stages.append(WorkbenchStage(name="authoring_parse", status="failed", error=str(error), summary="AI draft was not valid JSON"))
        drafted_catalog, validation_warnings = validate_catalog_entries(
            machine_catalog(draft_entries, trusted_approval=False), feature_keys
        )
        warnings.extend(validation_warnings)
        catalog_by_key = {
            str(item["key"]): item for item in [*saved_catalog, *drafted_catalog]
        }
        revision_attempted = 0
        low_confidence = [
            item for item in catalog_by_key.values()
            if float(item.get("confidence") or 0.0) < 0.8
            and item.get("disposition") != "exclude"
            and str(item["key"]) not in revised_keys
        ]
        for start in range(0, len(low_confidence), _AUTHORING_BATCH_SIZE):
            if authoring_tokens >= _AUTHORING_TOKEN_BUDGET:
                warnings.append("authoring token budget reached before confidence revision completed")
                break
            revision_batch = low_confidence[start:start + _AUTHORING_BATCH_SIZE]
            revision_keys = {str(item["key"]) for item in revision_batch}
            evidence = [unique_inventory[key] for key in revision_keys if key in unique_inventory]
            revision_prompt = f"""REVISE LOW-CONFIDENCE CATALOG METADATA

Revise every supplied metadata object once. Return exactly one complete JSON object
per exact key using only: key, canonical_key, value_category, related_features,
usefulness_rank, disposition, aggregate_prompt, confidence, uncertainty_reason.
Use only exact feature keys from the verified catalog. Do not invent values or
product behavior. Confidence is 0.00 through 1.00; uncertainty_reason is required
below 0.80 and null otherwise. Return JSON only.
{_AUTHORING_SELECTION_POLICY}

CURRENT METADATA:
{json.dumps(revision_batch, separators=(",", ":"))}

VARIABLE EVIDENCE (no organization values):
{json.dumps(evidence, separators=(",", ":"))}{feature_catalog}"""
            text, metrics = await run_model(
                revision_prompt,
                api_key=ai_api_key,
                model=model,
                stage=f"authoring_confidence_revision_{start // _AUTHORING_BATCH_SIZE + 1}",
                system="You revise uncertain catalog metadata once. Return only valid JSON.",
                max_tokens=min(
                    _AUTHORING_MAX_TOKENS,
                    _AUTHORING_TOKEN_BUDGET - authoring_tokens,
                ),
                effort="low",
            )
            authoring_tokens += int(metrics.get("output_tokens", 0))
            revision_attempted += len(revision_batch)
            revised_keys.update(revision_keys)
            try:
                revised = extract_json(text)
            except ValueError as error:
                warnings.append(f"confidence revision returned invalid JSON: {error}")
                revised = []
            if isinstance(revised, dict):
                revised = next(
                    (
                        revised[key]
                        for key in ("entries", "catalog", "items")
                        if isinstance(revised.get(key), list)
                    ),
                    [revised],
                )
            revision_items = revised if isinstance(revised, list) else []
            complete_revisions = [
                item for item in revision_items
                if isinstance(item, dict)
                and str(item.get("key")) in revision_keys
                and _REQUIRED_AUTHORING_FIELDS <= item.keys()
            ]
            validated_revisions, revision_warnings = validate_catalog_entries(
                machine_catalog(complete_revisions, trusted_approval=False), feature_keys
            )
            warnings.extend(revision_warnings)
            for item in validated_revisions:
                catalog_by_key[str(item["key"])] = item
            await save_checkpoint(
                list(catalog_by_key.values()),
                phase="revising",
                pending=max(0, len(low_confidence) - start - len(revision_batch)),
            )

        review_exceptions, final_catalog = prioritize_review_exceptions(
            list(catalog_by_key.values())
        )
        drafted_keys = {
            str(item["key"])
            for item in final_catalog
            if str(item["key"]) in inventory_keys
        }
        completed_keys = len(existing_keys | drafted_keys)
        await save_checkpoint(
            final_catalog,
            phase="needs_review" if review_exceptions else "complete",
            pending=0,
        )
        authoring_output = {
            "status": "draft_only",
            "draft": redact(final_catalog),
            "new_entries": len(drafted_catalog),
            "existing_entries": len(existing_keys),
            "remaining_keys": max(0, total_keys - completed_keys),
            "total_keys": total_keys,
            "completed_keys": completed_keys,
            "output_tokens": authoring_tokens,
            "token_budget": _AUTHORING_TOKEN_BUDGET,
            "budget_reached": authoring_tokens >= _AUTHORING_TOKEN_BUDGET,
            "revision_attempted": revision_attempted,
            "review_exceptions": redact(review_exceptions),
            "review_exception_count": len(review_exceptions),
            "confidence_threshold": 0.8,
            "catalog_version": {
                "id": body.catalog_version_id,
                "name": body.catalog_version_name,
                "saved_at": body.catalog_version_saved_at,
                "prompt": body.authoring_prompt,
            },
            "feature_catalog_version_id": body.feature_catalog_version_id,
        }
        return {
            "stages": [stage.model_dump() for stage in stages],
            "warnings": warnings,
            "outputs": {
                "audit": audit_output,
                "context_layer_coverage": coverage_output,
                "authoring": authoring_output,
            },
        }

    if body.workbench_mode == "evaluate":
        assert ai_api_key is not None
        validated, validation_warnings = validate_catalog_entries(
            machine_catalog(safe_catalog_override), feature_keys
        )
        warnings.extend(validation_warnings)
        curated = compile_context(
            scrubbed_sources,
            validated,
            feature_catalog_version_id=body.feature_catalog_version_id,
            context_catalog_version_id=body.catalog_version_id,
            feature_catalog_entries=feature_catalog_entries,
        )
        baseline_entries = [
            {
                "key": item["key"],
                "canonical_key": item["key"],
                "disposition": "include",
                "review_status": "reviewed",
            }
            for item in inventory
            if item.get("observed_state") != "removed_pii"
        ]
        baseline = compile_context(
            scrubbed_sources,
            baseline_entries,
            feature_catalog_version_id=None,
            context_catalog_version_id=None,
        )
        contexts = {
            "baseline": baseline["context"],
            "curated": curated["context"],
        }
        evaluation: dict[str, Any] = {}
        for arm, context in contexts.items():
            prompt = build_evolve_prompt(
                context,
                count=body.candidate_count,
                channels=body.channels,
                journey_window=body.journey_window,
            )
            text, metrics = await run_model(
                prompt,
                api_key=ai_api_key,
                model=model,
                stage=f"evaluation_{arm}",
            )
            candidates = parse_candidates(text)
            serialized = json.dumps(context, sort_keys=True, separators=(",", ":"))
            evaluation[arm] = {
                "context": context,
                "prompt": prompt,
                "candidates": candidates,
                "metrics": {
                    **metrics,
                    "context_characters": len(serialized),
                    "context_estimated_tokens": (len(serialized) + 3) // 4,
                },
            }
            stages.append(WorkbenchStage(
                name=f"evaluation_{arm}",
                data={"candidates": candidates},
                metrics=metrics,
                summary="Waypoint evolve prompt generated ideas without sending outreach",
            ))

        judge_inputs = {
            arm: {
                "prompt": result["prompt"],
                "candidates": result["candidates"],
                "metrics": result["metrics"],
            }
            for arm, result in evaluation.items()
        }
        judge_prompt = f"""Compare two Waypoint context tests for the same organization.
Read each exact generation prompt, generated ideas, and measured metrics. Each prompt already contains its scrubbed input context.
Choose the arm with more grounded, useful, actionable ideas for helping the Pro return to and use the app.
Do not reward verbosity. Suggest no more than three small changes to the curated context contract.
Return one JSON object only:
{{"winner":"baseline|curated|tie","reason":"short reason","suggested_changes":["change"]}}

BASELINE:
{json.dumps(judge_inputs["baseline"], separators=(",", ":"))}

CURATED:
{json.dumps(judge_inputs["curated"], separators=(",", ":"))}
"""
        judge_text, judge_metrics = await run_model(
            judge_prompt,
            api_key=ai_api_key,
            model=str(configured["fast_model"] or "claude-haiku-4-5"),
            stage="evaluation_judge",
            system="You compare two recommendation-generation results. Return only valid JSON.",
            max_tokens=1200,
        )
        judge = extract_json(judge_text)
        if not isinstance(judge, dict) or judge.get("winner") not in {"baseline", "curated", "tie"}:
            raise ValueError("evaluation judge returned an invalid verdict")
        changes = judge.get("suggested_changes")
        if not isinstance(changes, list):
            changes = []
        evaluation["judge"] = {
            "winner": judge["winner"],
            "reason": str(judge.get("reason") or "")[:500],
            "suggested_changes": [str(item)[:300] for item in changes[:3]],
        }
        evaluation["judge_metrics"] = judge_metrics
        stages.append(WorkbenchStage(
            name="evaluation_judge",
            data=evaluation["judge"],
            metrics=judge_metrics,
            summary="low-cost model compared baseline and curated results",
        ))
        return {
            "stages": [stage.model_dump() for stage in stages],
            "warnings": warnings,
            "outputs": {"evaluation": evaluation},
        }

    catalog = load_catalog() if body.enrichment == "catalog" else {}
    assert ai_api_key is not None
    current_context = {"organization": scrubbed, "variable_catalog": machine_catalog(safe_catalog_override), "product_context": "current_v3_catalog" if catalog else None}
    proposed_context = build_prompt_context(current_context, catalog) if catalog else current_context
    stages.append(WorkbenchStage(name="current_context", data=redact(current_context)))
    stages.append(WorkbenchStage(name="proposed_context", data=redact(proposed_context)))

    policies = [body.context_policy] if body.context_policy != "compare" else ["baseline", "proposed"]
    outputs: dict[str, Any] = {}
    for policy in policies:
        context = current_context if policy == "baseline" else proposed_context
        prompt = build_evolve_prompt(
            context,
            count=body.candidate_count,
            channels=body.channels,
            journey_window=body.journey_window,
        )
        stages.append(WorkbenchStage(name=f"{policy}_prompt", data=prompt, summary=f"{policy} exact V3 prompt"))
        try:
            text, metrics = await run_model(
                prompt,
                api_key=ai_api_key,
                model=model,
                stage=f"{policy}_generation",
            )
            stages.append(WorkbenchStage(name=f"{policy}_response", data=text, metrics=metrics))
            candidates = parse_candidates(text)
            stages.append(WorkbenchStage(name=f"{policy}_parsed_candidates", data=candidates, summary="candidate JSON parsed"))
            cards, card_warnings = resolve_product_cards(candidates, catalog)
            warnings.extend(f"{policy}:{warning}" for warning in card_warnings)
            outputs[policy] = {"candidates": candidates, "product_cards": cards}
        except Exception as error:  # noqa: BLE001 - model boundary becomes a visible runtime failure
            stages.append(
                WorkbenchStage(
                    name=f"{policy}_parse",
                    status="failed",
                    error=str(error),
                    summary="Model call completed, but its output was not valid candidate JSON.",
                )
            )
            outputs[policy] = {"candidates": [], "product_cards": []}

    stages.append(WorkbenchStage(name="candidate_diagnostics", data=outputs))
    return {"stages": [stage.model_dump() for stage in stages], "warnings": warnings, "outputs": outputs}


def _job_payload(job: WorkbenchJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "state": job.state,
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def build_promotion_preview(job: WorkbenchJob) -> tuple[dict[str, Any], dict[str, Any]]:
    evaluation = (job.result or {}).get("outputs", {}).get("evaluation")
    if (
        job.status != "completed"
        or job.request.get("workbench_mode") != "evaluate"
        or not isinstance(evaluation, dict)
    ):
        raise HTTPException(status_code=409, detail="Complete the context test before promotion")
    raw_entries = job.request.get("catalog_override")
    feature_entries = job.request.get("feature_catalog_entries")
    context_version_id = str(job.request.get("catalog_version_id") or "")
    feature_version_id = str(job.request.get("feature_catalog_version_id") or "")
    if not isinstance(raw_entries, list) or not isinstance(feature_entries, list):
        raise HTTPException(
            status_code=422,
            detail="Evaluation job is missing versioned catalog inputs",
        )
    if not context_version_id or not feature_version_id:
        raise HTTPException(status_code=422, detail="Evaluation job is missing catalog version IDs")

    feature_keys = {
        str(entry.get("feature"))
        for entry in feature_entries
        if isinstance(entry, dict) and entry.get("feature")
    }
    entries, counts, _warnings = prepare_promotion_entries(raw_entries, feature_keys)
    counts["approved"] = int(
        job.request.get("catalog_approved_count", counts["approved"])
    )
    counts["pii_removed"] = int(
        job.request.get("catalog_pii_removed_count", counts["pii_removed"])
    )
    fingerprint = json.dumps(
        {"evaluation_job_id": job.id, "policy": "pii-first-canonical-v1"},
        sort_keys=True,
        separators=(",", ":"),
    )
    promotion_id = f"promotion-{hashlib.sha256(fingerprint.encode()).hexdigest()[:16]}"
    bundle = build_promotion_bundle(
        entries,
        feature_catalog_entries=feature_entries,
        promotion_id=promotion_id,
        context_catalog_version_id=context_version_id,
        feature_catalog_version_id=feature_version_id,
        created_at=job.updated_at,
    )
    counts["retained"] = len(bundle["rules"])
    counts["duplicates_merged"] = max(
        0, counts["approved"] - counts["pii_removed"] - counts["retained"]
    )
    bundle["counts"] = counts
    if not bundle["rules"]:
        raise HTTPException(status_code=422, detail="Promotion requires an approved Include variable")
    return bundle, {
        "id": promotion_id,
        "included_variables": len(bundle["rules"]),
        "counts": counts,
        "csv": promotion_csv(bundle),
    }


def create_workbench_app(
    *,
    job_db_path: Path | None = None,
    promotion_root: Path | None = None,
    job_executor: Callable[..., Awaitable[dict[str, Any]]] = execute_run,
) -> FastAPI:
    store = WorkbenchJobStore(
        job_db_path or Path(__file__).parents[2] / ".workbench" / "jobs.sqlite3"
    )
    promotions = PromotionStore(promotion_root or DEFAULT_PROMOTION_ROOT)
    tasks: dict[str, asyncio.Task[None]] = {}

    async def run_job(job_id: str) -> None:
        job = store.get(job_id)
        if job is None:
            return
        store.set_status(job_id, "running")
        try:
            executable_request = {
                key: value
                for key, value in job.request.items()
                if key not in {"catalog_approved_count", "catalog_pii_removed_count"}
            }
            async def checkpoint(state: dict[str, Any]) -> None:
                store.checkpoint(job_id, state)

            result = await job_executor(
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
            store.complete(job_id, prune_job_result(result), status=status)
        except asyncio.CancelledError:
            store.set_status(job_id, "queued")
            raise
        except Exception as error:  # noqa: BLE001 - durable job boundary records safe error
            store.fail(job_id, str(error))

    def schedule(job_id: str) -> None:
        current = tasks.get(job_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(run_job(job_id))
        tasks[job_id] = task
        task.add_done_callback(lambda _task: tasks.pop(job_id, None))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        for job in store.resumable():
            schedule(job.id)
        yield
        active = list(tasks.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    app = FastAPI(
        title="Waypoint Context Workbench",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/context-workbench/status")
    async def status() -> dict[str, Any]:
        return _status_payload()

    @app.post("/api/context-workbench/catalog/validate")
    async def validate_catalog(body: CatalogValidateRequest) -> dict[str, Any]:
        try:
            entries = parse_feature_catalog_csv(body.csv_text)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
        version_id = f"features-{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"
        return {
            "id": version_id,
            "name": body.name,
            "source_filename": body.filename,
            "created_at": datetime.now(UTC).isoformat(),
            "entries": entries,
            "csv_text": body.csv_text,
        }

    def promotion_preview(evaluation_job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        job = store.get(evaluation_job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Evaluation job not found")
        return build_promotion_preview(job)

    @app.post("/api/context-workbench/promotions/preview")
    async def preview_promotion(body: PromotionRequest) -> dict[str, Any]:
        _bundle, payload = promotion_preview(body.evaluation_job_id)
        return payload

    @app.post("/api/context-workbench/promotions")
    async def promote(body: PromotionRequest) -> dict[str, Any]:
        bundle, payload = promotion_preview(body.evaluation_job_id)
        promotions.promote(bundle)
        return payload

    @app.post("/api/context-workbench/run")
    async def run(body: WorkbenchRunRequest) -> dict[str, Any]:
        return await execute_run(body)

    @app.post("/api/context-workbench/jobs", status_code=202)
    async def start_job(body: WorkbenchRunRequest) -> dict[str, Any]:
        job = store.create(body.model_dump())
        schedule(job.id)
        return _job_payload(job)

    @app.get("/api/context-workbench/jobs/latest")
    async def latest_job() -> dict[str, Any]:
        job = store.latest()
        if job is None:
            raise HTTPException(status_code=404, detail="No Workbench job exists")
        return _job_payload(job)

    @app.get("/api/context-workbench/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Workbench job not found")
        return _job_payload(job)

    @app.post("/api/context-workbench/jobs/{job_id}/resume")
    async def resume_job(job_id: str) -> dict[str, Any]:
        job = store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Workbench job not found")
        if job.status not in {"completed", "needs_review"}:
            store.set_status(job_id, "queued")
            schedule(job_id)
        refreshed = store.get(job_id)
        assert refreshed is not None
        return _job_payload(refreshed)

    return app


app = create_workbench_app()
