"""Runtime Staging context compiled from Workbench-owned sources."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from waypoint.context_promotion import compile_promoted_context
from waypoint.n8n import CONTRACT_VERSION, ContextUnavailable, OrgBrief, OrgContextBatch
from waypoint.workbench import (
    ContextLayerClient,
    context_layer_values,
    unwrap_source_payload,
)
from waypoint.workbench import (
    N8NContextClient as WorkbenchN8NClient,
)

log = logging.getLogger("waypoint.staging_context")
_MISSING = object()
_AUTHORITATIVE_FIRMOGRAPHIC_SOURCES = {
    "context_layer.firmographics.industry",
    "context_layer.firmographics.segment",
}
_AUTHORITATIVE_FIRMOGRAPHIC_KEYS = {"industry", "segment"}
_CORE_PLAN_NAMES = {
    "Basic": "Core SaaS Basic",
    "Essentials": "Core SaaS Essentials",
    "MAX": "Core SaaS MAX",
    "MAX+": "Core SaaS MAX+",
    "MAX++": "Core SaaS MAX++ [LEGACY]",
}
_SAFE_SOURCE_ERROR = re.compile(
    r"(?:Snowflake/n8n|Context Layer) returned HTTP [1-5][0-9]{2}"
    r"|Snowflake/n8n (?:timed out after|could not connect within) "
    r"[0-9]+(?:\.[0-9]+)? seconds"
    r"|Snowflake/n8n (?:request timed out|connection failed) "
    r"\([A-Za-z][A-Za-z0-9_]*\)"
    r"|Snowflake/n8n returned invalid JSON"
)


def _source_failure(source: str, error: BaseException) -> ContextUnavailable:
    detail = str(error)
    if not _SAFE_SOURCE_ERROR.fullmatch(detail):
        detail = type(error).__name__
    return ContextUnavailable(f"staging {source} source failed ({detail})")


def _row_value(row: Mapping[str, Any], name: str, default: Any = None) -> Any:
    return next(
        (value for key, value in row.items() if str(key).casefold() == name.casefold()),
        default,
    )


def _source_table(row: Mapping[str, Any]) -> str:
    table = _row_value(row, "source_table")
    metadata = _row_value(row, "metadata")
    if not table and isinstance(metadata, Mapping):
        table = _row_value(metadata, "source_table") or _row_value(metadata, "table_name")
    return str(table or "")


def _snowflake_rows(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, (dict, list)):
        raise TypeError("response was not an object or array")
    rows = unwrap_source_payload("snowflake", payload).get("rows")
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise TypeError("response did not contain a row array")
    return rows


def _authoritative_firmographics(payload: Mapping[str, Any]) -> dict[str, str]:
    firmographics = payload.get("firmographics")
    if not isinstance(firmographics, Mapping):
        raise ContextUnavailable("staging context_layer is missing firmographics.segment")

    raw_segment = firmographics.get("segment")
    if not isinstance(raw_segment, str):
        raise ContextUnavailable("staging context_layer is missing firmographics.segment")
    segment = raw_segment.strip().upper()
    if re.fullmatch(r"[1-4][A-D]", segment) is None:
        raise ContextUnavailable("staging context_layer returned invalid firmographics.segment")

    raw_industry = firmographics.get("industry")
    if not isinstance(raw_industry, str) or not raw_industry.strip():
        raise ContextUnavailable("staging context_layer is missing firmographics.industry")
    return {"segment": segment, "industry": raw_industry.strip()}


def _authoritative_core_plan(rows: list[Mapping[str, Any]]) -> str | None:
    found: set[str] = set()
    for row in rows:
        variable = _row_value(row, "variable_name")
        value = _row_value(row, "value", _MISSING)
        raw: object = _MISSING
        if isinstance(variable, str) and variable.casefold() == "core_saas_plan_level":
            raw = value
        elif (
            isinstance(variable, str)
            and variable.casefold() == "org_snapshot"
            and isinstance(value, Mapping)
        ):
            raw = _row_value(value, "core_saas_plan_level", _MISSING)
        if isinstance(raw, str) and raw.strip():
            found.add(raw.strip())
    if len(found) != 1:
        return None
    return _CORE_PLAN_NAMES.get(found.pop(), "UNMAPPED")


def _without_authoritative_rules(bundle: Mapping[str, Any]) -> Mapping[str, Any]:
    rules = bundle.get("rules")
    if not isinstance(rules, list):
        return bundle
    filtered = [
        rule
        for rule in rules
        if not isinstance(rule, Mapping)
        or (
            str(rule.get("source_key") or "").strip().casefold()
            not in _AUTHORITATIVE_FIRMOGRAPHIC_SOURCES
            and str(rule.get("canonical_key") or "").strip().casefold()
            not in _AUTHORITATIVE_FIRMOGRAPHIC_KEYS
        )
    ]
    return {**bundle, "rules": filtered}


def _compile_values(
    rows: list[Mapping[str, Any]],
    context_values: Mapping[str, Any],
    bundle: Mapping[str, Any],
) -> tuple[dict[str, Any], list[Mapping[str, Any]], dict[str, int], list[str]]:
    rules = bundle.get("rules")
    if not isinstance(rules, list):
        raise TypeError("promotion rules were not an array")

    snowflake_by_key: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        key = _row_value(row, "variable_name")
        if isinstance(key, str) and key:
            snowflake_by_key[key].append(row)

    candidates: dict[str, list[Any]] = defaultdict(list)
    matched_rules: list[Mapping[str, Any]] = []
    missing = ambiguous = matched = 0
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        source_key = str(rule.get("source_key") or "")
        canonical = str(rule.get("canonical_key") or "")
        if not source_key or not canonical:
            continue

        value: Any = _MISSING
        if source_key.startswith("context_layer."):
            value = context_values.get(source_key, _MISSING)
        else:
            matches = snowflake_by_key.get(source_key, [])
            lineage = str(rule.get("source_table") or "UNKNOWN")
            if lineage != "UNKNOWN":
                matches = [row for row in matches if _source_table(row) == lineage]
            if len(matches) == 1:
                value = _row_value(matches[0], "value", _MISSING)
            elif len(matches) > 1:
                ambiguous += 1

        if value is _MISSING:
            missing += 1
            continue
        # Runtime context is deliberately scalar. Approved object/array rows
        # can still contain an entire Snowflake record, so they never cross the
        # compact persisted boundary.
        if value is not None and not isinstance(value, (str, int, float, bool)):
            missing += 1
            continue
        matched += 1
        candidates[canonical].append(value)
        matched_rules.append(rule)

    values: dict[str, Any] = {}
    conflicts = 0
    conflict_keys: list[str] = []
    for canonical, found in candidates.items():
        first = found[0]
        if any(value != first for value in found[1:]):
            conflicts += 1
            conflict_keys.append(canonical)
            continue
        values[canonical] = first

    received = len(rows) + len(context_values)
    return (
        values,
        [rule for rule in matched_rules if str(rule.get("canonical_key") or "") in values],
        {
            "received": received,
            "matched": matched,
            "discarded": max(received - matched, 0),
            "missing": missing,
            "ambiguous": ambiguous,
            "conflicts": conflicts,
        },
        sorted(conflict_keys),
    )


def compile_staging_brief(
    organization_id: str,
    snowflake_result: Any,
    context_layer_result: Mapping[str, Any],
    bundle: Mapping[str, Any],
    *,
    include_features_not_in_current_plan: bool = False,
) -> OrgBrief:
    """Deterministically reduce raw sources to the promoted runtime contract."""
    try:
        rows = _snowflake_rows(snowflake_result)
    except (TypeError, ValueError) as error:
        raise ContextUnavailable(
            f"staging snowflake contract violation ({type(error).__name__})"
        ) from error

    firmographics = _authoritative_firmographics(context_layer_result)
    runtime_bundle = _without_authoritative_rules(bundle)
    values, matched_rules, counts, conflict_keys = _compile_values(
        rows, context_layer_values(context_layer_result), runtime_bundle
    )
    if not values:
        raise ContextUnavailable("staging context matched zero approved variables")
    values.update(firmographics)
    core_plan = _authoritative_core_plan(rows)
    if core_plan is not None:
        values["core_saas_plan"] = core_plan

    log.info(
        "staging context compiled promotion=%s received=%d matched=%d "
        "discarded=%d missing=%d ambiguous=%d conflicts=%d",
        str(bundle.get("id") or "unknown"),
        counts["received"],
        counts["matched"],
        counts["discarded"],
        counts["missing"],
        counts["ambiguous"],
        counts["conflicts"],
    )
    typed = {
        key: value
        for key, value in values.items()
        if key in OrgBrief.model_fields
        and key not in {"org_uuid", "curated_context"}
        and isinstance(value, str)
    }
    matched_bundle = {**runtime_bundle, "rules": matched_rules}
    curated_context = compile_promoted_context(
        values,
        matched_bundle,
        include_features_not_in_current_plan=include_features_not_in_current_plan,
        apply_plan_availability=True,
    )
    compiled_values = curated_context.get("v")
    if not isinstance(compiled_values, dict):
        compiled_values = {}
    authoritative = dict(firmographics)
    if core_plan is not None:
        authoritative["core_saas_plan"] = core_plan
    curated_context["v"] = dict(sorted({**compiled_values, **authoritative}.items()))
    if conflict_keys:
        curated_context["conflicts"] = conflict_keys
    return OrgBrief(
        org_uuid=organization_id,
        org_id=organization_id,
        pro_uuid=_contact_pro(rows),
        **typed,
        curated_context=curated_context,
        contact_candidates=_contact_candidates(rows),
    )


def _contact_pro(rows: list[Mapping[str, Any]]) -> str | None:
    """The org's founding admin, emitted by the flow's `waypoint_contact_pro`
    node. An identifier for the LCM handoff, never context: it bypasses the
    promotion allowlist and is not placed in the prompt packet."""
    for row in rows:
        if str(_row_value(row, "query_name") or "") == "waypoint_contact_pro":
            value = _row_value(row, "value")
            return str(value) if value else None
    return None


def _contact_candidates(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]] | None:
    """Rows from the flow's `waypoint_contact_candidate` block: identifiers and
    booleans for the contact plan. Bypass promotion like `_contact_pro`. None
    means the flow predates the block (legacy plan), not "no admins"."""
    found: list[dict[str, Any]] = []
    for row in rows:
        if str(_row_value(row, "query_name") or "") != "waypoint_contact_candidate":
            continue
        value = _row_value(row, "value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, Mapping):
            found.append(dict(value))
    return found or None


class WorkbenchStagingContextClient:
    def __init__(
        self,
        *,
        workbench_url: str,
        n8n_token: str,
        context_layer_url: str,
        context_layer_key: str,
        promotion_loader: Callable[[], Awaitable[dict[str, Any] | None]],
        max_concurrent: int = 3,
        n8n_timeout: float = 240.0,
        snowflake: Any | None = None,
        context_layer: Any | None = None,
    ) -> None:
        self.workbench_url = workbench_url
        self.n8n_token = n8n_token
        self.context_layer_url = context_layer_url
        self.context_layer_key = context_layer_key
        self.promotion_loader = promotion_loader
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self.snowflake = snowflake or WorkbenchN8NClient(timeout=n8n_timeout)
        self.context_layer = context_layer or ContextLayerClient()

    async def start(
        self, organization_id: str, request_id: str, promotion_id: str
    ) -> None:
        if re.fullmatch(r"\d{5,6}", organization_id) is None:
            raise ContextUnavailable(
                "staging context requires a five- or six-digit organization ID"
            )
        if not promotion_id:
            raise ContextUnavailable("staging context promotion artifact has no id")
        try:
            await self.snowflake.start(
                organization_id,
                self.workbench_url,
                self.n8n_token,
                request_id=request_id,
                promotion_id=promotion_id,
            )
        except Exception as error:
            raise _source_failure("snowflake", error) from error

    async def fetch(self, organization_ids: list[str]) -> OrgContextBatch:
        if any(
            re.fullmatch(r"\d{5,6}", identifier) is None
            for identifier in organization_ids
        ):
            raise ContextUnavailable(
                "staging context requires a five- or six-digit organization ID"
            )
        bundle = await self.promotion_loader()
        if bundle is None:
            raise ContextUnavailable("staging context promotion artifact is missing")
        promotion_id = str(bundle.get("id") or "")
        if not promotion_id:
            raise ContextUnavailable("staging context promotion artifact has no id")

        organizations = await asyncio.gather(
            *(self._fetch_one(identifier, bundle) for identifier in organization_ids)
        )
        return OrgContextBatch(
            contract_version=CONTRACT_VERSION,
            organizations=list(organizations),
            audience_query_version=f"workbench:{promotion_id}",
        )

    async def _fetch_one(self, organization_id: str, bundle: Mapping[str, Any]) -> OrgBrief:
        async with self._semaphore:
            results = await asyncio.gather(
                self.snowflake.fetch(organization_id, self.workbench_url, self.n8n_token),
                self.context_layer.fetch(
                    organization_id, self.context_layer_url, self.context_layer_key
                ),
                return_exceptions=True,
            )
        snowflake_result: Any = results[0]
        context_layer_result: Any = results[1]

        if isinstance(snowflake_result, BaseException):
            raise _source_failure("snowflake", snowflake_result) from snowflake_result
        if isinstance(context_layer_result, BaseException):
            raise _source_failure("context_layer", context_layer_result) from context_layer_result

        if not isinstance(context_layer_result, Mapping):
            raise ContextUnavailable("staging context_layer contract violation (TypeError)")
        return compile_staging_brief(
            organization_id,
            snowflake_result,
            context_layer_result,
            bundle,
        )
