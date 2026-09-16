"""Local-only API for the Context Layer workbench."""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from waypoint.llm import extract_json
from waypoint.workbench import (
    ContextLayerClient,
    N8NContextClient,
    WorkbenchStage,
    build_evolve_prompt,
    build_prompt_context,
    load_catalog,
    parse_candidates,
    redact,
    resolve_product_cards,
    run_model,
    scrub_pii,
    shape,
    unwrap_source_payload,
    workbench_env,
)

_AUTHORING_TOKEN_BUDGET = 70_000
_AI_CATALOG_FIELDS = {
    "key", "canonical_key", "value_category", "related_features",
    "usefulness_rank", "aggregate_prompt",
}


class WorkbenchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str = Field(min_length=1)
    identifier_type: Literal["organization_id", "org_uuid", "pro_uuid"] = "organization_id"
    source_mode: Literal["snowflake", "context_layer", "both"] = "both"
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
    workbench_mode: Literal["runtime", "authoring"] = "runtime"
    authoring_prompt: str = "Create a token-efficient, AI-only variable catalog using key, canonical_key, value_category, related_features, usefulness_rank, and aggregate_prompt. Map each variable only to exact feature keys from the supplied catalog. Do not create human descriptions, time metadata, or recommendations. Return valid JSON only."
    feature_catalog_csv: str | None = None
    catalog_override: list[dict[str, Any]] | None = None
    catalog_version_id: str | None = None
    catalog_version_name: str | None = None
    catalog_version_saved_at: str | None = None


def _stage(name: str, data: Any, started: float, **kwargs: Any) -> WorkbenchStage:
    return WorkbenchStage(
        name=name,
        data=data,
        duration_ms=round((time.perf_counter() - started) * 1000),
        **kwargs,
    )


def authoring_inventory(scrubbed: dict[str, Any]) -> list[dict[str, str]]:
    """Build a compact, non-value inventory for catalog authoring."""
    found: dict[str, str] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).casefold()
                if normalized in {"variable_name", "query_name"} and isinstance(child, str):
                    found.setdefault(child, "unknown")
                elif normalized not in {"query_name", "source_table", "metadata"}:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(scrubbed)
    return [{"key": key, "value_type": value_type} for key, value_type in sorted(found.items())]


def machine_catalog(entries: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Project saved or drafted catalog entries to the AI-only runtime contract."""
    return [
        {key: value for key, value in entry.items() if key in _AI_CATALOG_FIELDS}
        for entry in (entries or [])
        if isinstance(entry, dict) and entry.get("key")
    ]


async def execute_run(body: WorkbenchRunRequest) -> dict[str, Any]:
    configured = workbench_env()
    context_base_url = body.context_base_url or configured["context_base_url"]
    context_api_key = body.context_api_key or configured["context_api_key"]
    ai_api_key = body.ai_api_key or configured["ai_api_key"]
    model = body.model or configured["model"] or "claude-sonnet-5"
    if not ai_api_key:
        raise HTTPException(status_code=422, detail="ANTHROPIC_API_KEY or LLM_API_KEY is required in services/api/.env")
    stages: list[WorkbenchStage] = []
    warnings: list[str] = []
    stages.append(
        WorkbenchStage(
            name="input",
            data=body.model_dump(exclude={"context_api_key", "ai_api_key"}),
        )
    )

    n8n_webhook_url = body.n8n_webhook_url or configured["n8n_webhook_url"]
    n8n_webhook_token = body.n8n_webhook_token or configured["n8n_webhook_token"]
    if body.source_mode in ("context_layer", "both") and (not context_base_url or not context_api_key):
        raise HTTPException(status_code=422, detail="Context Layer mode requires CONTEXT_LAYER_BASE_URL and CONTEXT_LAYER_API_KEY in services/api/.env")
    if body.source_mode in ("snowflake", "both") and (not n8n_webhook_url or not n8n_webhook_token):
        raise HTTPException(status_code=422, detail="Snowflake mode requires N8N_CONTEXT_WEBHOOK_URL and N8N_CONTEXT_WEBHOOK_TOKEN in services/api/.env")

    sources: dict[str, Any] = {}
    source_errors: dict[str, str] = {}
    requests: list[tuple[str, Any]] = []
    if body.source_mode in ("context_layer", "both"):
        requests.append(("context_layer", ContextLayerClient().fetch(body.identifier, context_base_url or "", context_api_key or "")))
    if body.source_mode in ("snowflake", "both"):
        requests.append(("snowflake", N8NContextClient().fetch(body.identifier, n8n_webhook_url or "", n8n_webhook_token or "")))
    import asyncio
    results = await asyncio.gather(*(request for _, request in requests), return_exceptions=True)
    for (source, _), result in zip(requests, results):
        failed = isinstance(result, Exception)
        if failed:
            source_errors[source] = str(result)
        else:
            sources[source] = result
        stages.append(WorkbenchStage(name=f"{source}_context", data=None if failed else shape(result), status="failed" if failed else "succeeded", error=str(result) if failed else None, summary=f"{source} payload shape captured; values withheld"))
    if not sources:
        raise HTTPException(status_code=502, detail="All selected context sources failed")

    started = time.perf_counter()
    raw = {"sources": sources, "source_errors": source_errors}
    # Raw values never leave this process, even in the local browser trace.
    stages.append(_stage("raw_context", shape(raw), started, summary="structure only; raw values are intentionally withheld here; see scrubbed_context for retained non-PII values"))

    started = time.perf_counter()
    normalized: dict[str, Any] = {"source": "combined" if len(sources) > 1 else next(iter(sources)), "grain": "organization", "sources": {source: unwrap_source_payload(source, payload) for source, payload in sources.items()}}
    stages.append(_stage("normalized_context", shape(normalized), started, summary="sources normalized into one trace; values remain withheld until after the PII gate"))

    started = time.perf_counter()
    scrubbed_sources: dict[str, Any] = {}
    pii_ledger: list[dict[str, str]] = []
    for source, payload in normalized["sources"].items():
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
    if not isinstance(scrubbed, dict):
        raise HTTPException(status_code=422, detail="PII gate did not produce an object")
    stages.append(WorkbenchStage(name="scrubbed_context", data=redact(scrubbed)))

    if body.workbench_mode == "authoring":
        feature_catalog = f"\n\nFEATURE CATALOG CSV:\n{body.feature_catalog_csv}" if body.feature_catalog_csv else ""
        saved_catalog = machine_catalog(body.catalog_override)
        inventory = authoring_inventory(scrubbed)
        total_keys = len(inventory)
        inventory_keys = {item["key"] for item in inventory}
        existing_keys = {
            str(item.get("key"))
            for item in saved_catalog
            if item.get("key") and str(item.get("key")) in inventory_keys
        }
        inventory = [item for item in inventory if item["key"] not in existing_keys]
        draft_entries: list[Any] = []
        authoring_tokens = 0
        try:
            pending = list(inventory)
            batch_number = 0
            while pending:
                batch_number += 1
                if authoring_tokens >= _AUTHORING_TOKEN_BUDGET:
                    warnings.append("authoring token budget reached; remaining catalog keys were not drafted")
                    break
                batch = pending[:15]
                authoring_prompt = f"""{body.authoring_prompt}

For each variable key below, return exactly one object with only these fields.
Do not omit any requested key and do not return keys that were not requested:
key, canonical_key, value_category, related_features, usefulness_rank,
aggregate_prompt.
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
`aggregate_prompt` must be null unless usefulness_rank is 4 or 5. For rank 4 or 5,
write exactly one sentence addressed to Claude requesting a cohort-level
statistic computed across comparable Pros, not a per-Pro calculation. The
sentence should explain how the resulting statistic will position each Pro,
for example: "Calculate the matched cohort mean, percentiles, sample size, and
standard deviation for this metric so the runtime can classify where each Pro
sits within that cohort." Do not invent values,
relationships, or product claims. Return JSON only.

VARIABLE INVENTORY BATCH {batch_number} ({len(batch)} keys; no organization values):
{batch}{feature_catalog}"""
                stages.append(WorkbenchStage(name=f"authoring_prompt_{batch_number}", data=authoring_prompt, summary="AI catalog-drafting prompt using scrubbed variable inventory only"))
                remaining_tokens = _AUTHORING_TOKEN_BUDGET - authoring_tokens
                text, metrics = await run_model(authoring_prompt, api_key=ai_api_key, model=model, stage=f"authoring_generation_{batch_number}", system="You draft deterministic catalog metadata for human review. Return only valid JSON.", max_tokens=min(4000, remaining_tokens))
                authoring_tokens += int(metrics.get("output_tokens", 0))
                stages.append(WorkbenchStage(name=f"authoring_response_{batch_number}", data=text, metrics=metrics))
                try:
                    draft = extract_json(text)
                except ValueError as parse_error:
                    repair_prompt = f"""Repair the following malformed catalog response. Return only a valid JSON array. Preserve the intended entries and fields; do not add commentary.

MALFORMED_RESPONSE:
{text}"""
                    repair_text, repair_metrics = await run_model(repair_prompt, api_key=ai_api_key, model=model, stage=f"authoring_repair_{batch_number}", system="You repair malformed JSON. Return only valid JSON.", max_tokens=min(4000, max(1, _AUTHORING_TOKEN_BUDGET - authoring_tokens)))
                    authoring_tokens += int(repair_metrics.get("output_tokens", 0))
                    stages.append(WorkbenchStage(name=f"authoring_repair_{batch_number}", data=repair_text, metrics=repair_metrics, summary=f"JSON repair retry after: {parse_error}"))
                    try:
                        draft = extract_json(repair_text)
                    except ValueError:
                        draft = []
                        for item in batch:
                            single_prompt = f"""{body.authoring_prompt}

Return exactly one valid JSON object for this variable using only these fields:
key, canonical_key, value_category, related_features, usefulness_rank, aggregate_prompt. Do not return markdown or commentary.
VARIABLE: {item}"""
                            single_text, single_metrics = await run_model(single_prompt, api_key=ai_api_key, model=model, stage=f"authoring_single_{batch_number}", system="Return exactly one valid JSON object and nothing else.", max_tokens=min(1200, max(1, _AUTHORING_TOKEN_BUDGET - authoring_tokens)))
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
                draft_entries.extend(draft)
                returned_keys = {
                    str(item.get("key"))
                    for item in draft
                    if isinstance(item, dict) and item.get("key")
                }
                matched_keys = {item["key"] for item in batch} & returned_keys
                if not matched_keys:
                    warnings.append(f"authoring batch {batch_number} returned no requested keys; remaining catalog keys were not drafted")
                    break
                pending = [item for item in pending if item["key"] not in matched_keys]
            stages.append(WorkbenchStage(name="authoring_parse", data=draft_entries, summary="Draft catalog JSON parsed; not approved for runtime"))
        except Exception as error:  # noqa: BLE001 - any model-output failure is reported as a stage
            stages.append(WorkbenchStage(name="authoring_parse", status="failed", error=str(error), summary="AI draft was not valid JSON"))
        drafted_catalog = machine_catalog(draft_entries)
        drafted_keys = {
            str(item["key"])
            for item in drafted_catalog
            if str(item["key"]) in inventory_keys
        }
        completed_keys = len(existing_keys | drafted_keys)
        authoring_output = {
            "status": "draft_only",
            "draft": redact([*saved_catalog, *drafted_catalog]),
            "new_entries": len(drafted_catalog),
            "existing_entries": len(existing_keys),
            "remaining_keys": max(0, total_keys - completed_keys),
            "total_keys": total_keys,
            "completed_keys": completed_keys,
            "output_tokens": authoring_tokens,
            "token_budget": _AUTHORING_TOKEN_BUDGET,
            "budget_reached": authoring_tokens >= _AUTHORING_TOKEN_BUDGET,
            "catalog_version": {
                "id": body.catalog_version_id,
                "name": body.catalog_version_name,
                "saved_at": body.catalog_version_saved_at,
                "prompt": body.authoring_prompt,
            },
        }
        return {"stages": [stage.model_dump() for stage in stages], "warnings": warnings, "outputs": {"authoring": authoring_output}}

    catalog = load_catalog() if body.enrichment == "catalog" else {}
    current_context = {"organization": scrubbed, "variable_catalog": machine_catalog(body.catalog_override), "product_context": "current_v3_catalog" if catalog else None}
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
        except Exception as error:  # noqa: BLE001 - any model-output failure is reported as a stage
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


def create_workbench_app() -> FastAPI:
    app = FastAPI(title="Waypoint Context Workbench", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_methods=["GET", "POST"],
        allow_headers=["content-type"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/context-workbench/run")
    async def run(body: WorkbenchRunRequest) -> dict[str, Any]:
        return await execute_run(body)

    return app


app = create_workbench_app()
