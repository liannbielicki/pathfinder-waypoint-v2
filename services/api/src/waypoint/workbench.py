"""Local, read-only context inspection primitives."""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from waypoint.llm import Pricing, extract_json, retry_rate_limit
from waypoint.prompts import EVOLVE_SYSTEM, evolve_prompt

_SECRET_WORDS = ("key", "token", "secret", "authorization", "password", "credential")
_SAFE_CATALOG_FIELDS = {
    "canonical_key", "value_category", "related_features", "usefulness_rank",
    "aggregate_prompt", "disposition", "review_status", "approval_status",
    "confidence", "uncertainty_reason", "exclusion_reason",
}
_PII_WORDS = (
    "name", "email", "phone", "address", "street", "city", "state", "country", "zip",
    "postal", "uuid", "organization_id", "salesforce", "contact", "lead_id", "pro_id",
)
_METADATA_KEYS = {
    "query_name", "variable_name", "source_table", "source_database", "table_schema",
    "table_name", "column_name", "data_type", "ordinal_position", "comment",
}
_NON_PII_BUSINESS_KEYS = {
    "emails_sent", "emails_opened", "emails_clicked", "emails_bounced", "emails_unsubscribed",
    "current_plan_name", "previous_plan_name", "credit_group_name",
}
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d .()/-]{7,}\d)(?!\d)")
_DATE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[T ]\S+)?$")
_ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_STATE_OR_COUNTRY = re.compile(r"^(?:[A-Z]{2}|US|USA|United States|Canada|Australia|United Kingdom)$", re.IGNORECASE)
_ADDRESS = re.compile(r"\b\d{1,6}\s+[A-Za-z0-9 .'-]+\s+(?:street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln|court|ct|way|highway|hwy)\b", re.IGNORECASE)
_PERSON_NAME = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}\b")
_FIXTURE_PATH = Path(__file__).parents[2] / "tests" / "fixtures" / "context_layer_workbench.json"


def _env(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    env_path = Path(__file__).parents[2] / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() == name:
            return candidate.strip().strip("\"'") or None
    return None


def workbench_env() -> dict[str, str | None]:
    return {
        "context_base_url": _env("CONTEXT_LAYER_BASE_URL"),
        "context_api_key": _env("CONTEXT_LAYER_API_KEY"),
        "n8n_webhook_url": _env("N8N_CONTEXT_WEBHOOK_URL"),
        "n8n_webhook_token": _env("N8N_CONTEXT_WEBHOOK_TOKEN"),
        "ai_api_key": _env("ANTHROPIC_API_KEY") or _env("LLM_API_KEY"),
        "model": _env("WORKBENCH_MODEL") or _env("MODEL_FAST"),
        "fast_model": _env("MODEL_FAST") or "claude-haiku-4-5",
    }


class WorkbenchStage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: str = "succeeded"
    summary: str | None = None
    data: Any = None
    error: str | None = None
    duration_ms: int | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)


def redact(value: Any, *, _key: str = "") -> Any:
    """Return JSON-shaped data with credential-like values removed."""
    if _key.casefold() == "key" or _key.casefold() in _SAFE_CATALOG_FIELDS:
        return value
    if any(word in _key.lower() for word in _SECRET_WORDS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(key): redact(item, _key=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def shape(value: Any) -> Any:
    """Expose structure/presence only, never raw values."""
    if isinstance(value, Mapping):
        return {str(key): shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return {"type": "list", "length": len(value), "items": shape(value[0]) if value else None}
    return {"type": type(value).__name__, "present": value is not None}


def _is_pii_variable_key(value: str) -> bool:
    key = value.casefold()
    if key in _NON_PII_BUSINESS_KEYS or key.startswith("feature_") and key.endswith("_state"):
        return False
    tokens = set(filter(None, re.split(r"[^a-z0-9]+", key)))
    if tokens & {"id", "identifier"}:
        return True
    if tokens & {"person", "customer", "employee", "account", "user", "contact"} and tokens & {"key", "number"}:
        return True
    if tokens & {"dob", "ssn", "passport"}:
        return True
    if "birth" in tokens and ("date" in tokens or "day" in tokens):
        return True
    if "social" in tokens and "security" in tokens:
        return True
    if "driver" in tokens and "license" in tokens:
        return True
    if "tax" in tokens and tokens & {"id", "identifier", "number", "tin"}:
        return True
    if tokens & {"bank", "checking", "savings"} and tokens & {"account", "routing", "number"}:
        return True
    if tokens & {"email", "phone", "address", "street", "city", "country", "zip", "postal", "salesforce", "contact"}:
        return True
    if "uuid" in tokens or key in {"organization_id", "org_id", "pro_id", "lead_id", "state"}:
        return True
    return "name" in tokens and bool(tokens & {"organization", "org", "company", "person", "user", "customer", "contact", "first", "last", "primary"})


def scrub_pii(value: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Apply the app-side PII exclusion criteria to an arbitrary context pack."""
    identity_values = {
        str(item).strip().casefold() for key, item in value.items()
        if str(key).casefold() not in {"query_name", "variable_name"}
        and any(word in str(key).casefold() for word in ("name", "email", "phone", "address"))
        and isinstance(item, str) and item.strip()
    }
    ledger: list[dict[str, str]] = []

    def walk(item: Any, path: str, leaf: str) -> Any:
        if isinstance(item, Mapping):
            mapping_output: dict[str, Any] = {}
            variable_name = next(
                (str(child) for key, child in item.items() if str(key).casefold() == "variable_name"),
                "",
            ).casefold()
            for key, child in item.items():
                child_path = f"{path}.{key}" if path else str(key)
                if (
                    str(key).casefold() == "value"
                    and _is_pii_variable_key(variable_name)
                ):
                    ledger.append({"path": child_path, "category": "variable", "reason": "variable key is classified as PII by the exclusion list"})
                    continue
                result = walk(child, child_path, str(key).casefold())
                if result is not _DROP:
                    mapping_output[str(key)] = result
            return mapping_output
        if isinstance(item, list):
            list_output: list[Any] = []
            for index, child in enumerate(item):
                result = walk(child, f"{path}[{index}]", leaf)
                if result is not _DROP:
                    list_output.append(result)
            return list_output
        if isinstance(item, str):
            if leaf in _METADATA_KEYS:
                return item
            if ".features[" in f".{path.casefold()}" and leaf in {"name", "display_name"}:
                return item
            lowered = item.casefold()
            if _EMAIL.search(item) or (not _DATE.fullmatch(item.strip()) and _PHONE.search(item)) or _ZIP.search(item) or _ADDRESS.search(item):
                ledger.append({"path": path, "category": "pattern", "reason": "value matched an excluded PII pattern"})
                return _DROP
            if any(identity and identity in lowered for identity in identity_values):
                ledger.append({"path": path, "category": "identity_match", "reason": "value matched an excluded identity value"})
                return _DROP
            if _STATE_OR_COUNTRY.fullmatch(item.strip()):
                ledger.append({"path": path, "category": "location", "reason": "value matched an excluded state or country token"})
                return _DROP
            if (
                _PERSON_NAME.search(item)
                and len(item.split()) <= 5
                and not re.search(r"\b(?:and|or|the|of|for|with|is|has|from|to)\b", item, re.IGNORECASE)
            ):
                ledger.append({"path": path, "category": "name_pattern", "reason": "value matched an excluded person-name pattern"})
                return _DROP
        if (
            leaf not in _METADATA_KEYS
            and leaf not in _NON_PII_BUSINESS_KEYS
            and (_is_pii_variable_key(leaf) or any(word in leaf for word in _PII_WORDS))
        ):
            ledger.append({"path": path, "category": "field", "reason": "field is classified as PII by the exclusion list"})
            return _DROP
        return item

    _DROP = object()
    cleaned = walk(value, "", "")
    return (cleaned if isinstance(cleaned, dict) else {}, ledger)


class ContextLayerClient:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._transport = transport
        self._timeout = httpx.Timeout(timeout, connect=min(timeout, 10.0))

    async def fetch(self, organization_uuid: str, base_url: str, api_key: str) -> dict[str, Any]:
        url = f"{base_url.rstrip('/')}/api/context_layer/{organization_uuid}"
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
            follow_redirects=False,
        ) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
        if response.status_code != 200:
            raise ValueError(f"Context Layer returned HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("Context Layer response was not an object")
        return payload


class N8NContextClient:
    """Read-only client for the authenticated Snowflake context webhook."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 240.0) -> None:
        self._transport = transport
        self._timeout_seconds = timeout
        self._timeout = httpx.Timeout(timeout, connect=min(timeout, 10.0))

    async def fetch(self, organization_id: str, webhook_url: str, token: str) -> dict[str, Any] | list[Any]:
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout, follow_redirects=False) as client:
                response = await client.post(webhook_url, headers={"Authorization": f"Bearer {token}"}, json={"organization_id": organization_id})
        except httpx.ConnectTimeout as error:
            raise TimeoutError("Snowflake/n8n could not connect within 10 seconds") from error
        except httpx.ReadTimeout as error:
            raise TimeoutError(
                f"Snowflake/n8n timed out after {self._timeout_seconds:g} seconds"
            ) from error
        except httpx.TimeoutException as error:
            raise TimeoutError(
                f"Snowflake/n8n request timed out ({type(error).__name__})"
            ) from error
        except httpx.RequestError as error:
            raise ConnectionError(
                f"Snowflake/n8n connection failed ({type(error).__name__})"
            ) from error
        if response.status_code != 200:
            raise ValueError(f"Snowflake/n8n returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except json.JSONDecodeError as error:
            raise ValueError("Snowflake/n8n returned invalid JSON") from error
        if not isinstance(payload, (dict, list)):
            raise TypeError("Snowflake/n8n response was not an object or array")
        return payload


def unwrap_source_payload(source: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove the n8n transport envelope before context normalization."""
    if source == "snowflake" and isinstance(payload, list):
        return {"rows": payload}
    if source == "snowflake" and isinstance(payload.get("context"), Mapping):
        context = payload["context"]
        row = context.get("context_row")
        if isinstance(row, Mapping):
            result = dict(row)
            event_context = context.get("event_context")
            if isinstance(event_context, Mapping):
                result.update(event_context)
            return result
        return dict(context)
    return dict(payload)


def _row_value(row: Mapping[str, Any], name: str) -> Any:
    for key, value in row.items():
        if str(key).casefold() == name.casefold():
            return value
    return None


def _row_has(row: Mapping[str, Any], name: str) -> bool:
    return any(str(key).casefold() == name.casefold() for key in row)


def _observed_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def _context_layer_values(payload: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    firmographics = payload.get("firmographics")
    if isinstance(firmographics, Mapping):
        for field, value in firmographics.items():
            values[f"context_layer.firmographics.{field}"] = value
    features = payload.get("features")
    if isinstance(features, list):
        for feature in features:
            if not isinstance(feature, Mapping) or not feature.get("name"):
                continue
            name = str(feature["name"])
            for field, value in feature.items():
                if field != "name":
                    values[f"context_layer.features.{name}.{field}"] = value
    for field, value in payload.items():
        if field not in {"firmographics", "features"} and not isinstance(value, (Mapping, list)):
            values[f"context_layer.{field}"] = value
    return values


def build_audit_inventory(sources: Mapping[str, Any]) -> list[dict[str, str]]:
    """Describe every observed source value without exposing the value itself."""
    inventory: list[dict[str, str]] = []
    for source, payload in sources.items():
        rows = payload.get("rows") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            if source == "context_layer" and isinstance(payload, Mapping):
                for key, value in sorted(_context_layer_values(payload).items()):
                    inventory.append(
                        {
                            "key": key,
                            "source": "context_layer",
                            "source_path": key.removeprefix("context_layer."),
                            "source_query": "",
                            "observed_state": "null" if value is None else "present",
                            "observed_type": _observed_type(value),
                            "basis": "observed",
                        }
                    )
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            key = _row_value(row, "variable_name")
            if not isinstance(key, str) or not key:
                continue
            value = _row_value(row, "value")
            query = _row_value(row, "query_name")
            has_value = _row_has(row, "value")
            inventory.append(
                {
                    "key": key,
                    "source": str(source),
                    "source_path": f"rows[{index}].VALUE",
                    "source_query": str(query or ""),
                    "observed_state": "removed_pii" if not has_value else "null" if value is None else "present",
                    "observed_type": _observed_type(value) if has_value else "unavailable",
                    "basis": "observed",
                }
            )
    return inventory


def parse_feature_catalog_csv(csv_text: str) -> list[dict[str, str]]:
    """Parse a replaceable feature catalog while preserving its supplied columns."""
    reader = csv.reader(io.StringIO(csv_text))
    try:
        supplied_headers = [header.strip().lstrip("\ufeff") for header in next(reader)]
    except StopIteration as error:
        raise ValueError("feature catalog is empty") from error

    normalized_headers = [
        re.sub(r"[^a-z0-9]+", "_", header.casefold()).strip("_")
        for header in supplied_headers
    ]
    feature_columns: list[int] = []
    for accepted_header in ("feature", "feature_key", "display_name"):
        feature_columns = [
            index for index, header in enumerate(normalized_headers) if header == accepted_header
        ]
        if feature_columns:
            break
    if not feature_columns:
        found = ", ".join(supplied_headers) or "none"
        raise ValueError(
            "feature catalog requires a Feature, Feature Key, or Display Name column; "
            f"found: {found}"
        )
    if len(feature_columns) > 1:
        matches = ", ".join(supplied_headers[index] for index in feature_columns)
        raise ValueError(f"feature catalog has multiple possible feature columns: {matches}")

    headers: list[str] = []
    header_counts: dict[str, int] = {}
    for header in supplied_headers:
        count = header_counts.get(header, 0) + 1
        header_counts[header] = count
        headers.append(header if count == 1 else f"{header} ({count})")

    feature_column = feature_columns[0]
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for line_number, values in enumerate(reader, start=2):
        if len(values) > len(headers):
            raise ValueError(f"feature catalog line {line_number} has more values than columns")
        values.extend([""] * (len(headers) - len(values)))
        row = {header: value.strip() for header, value in zip(headers, values, strict=True)}
        feature = values[feature_column].strip()
        if not feature:
            continue
        if feature in seen:
            raise ValueError(f"duplicate feature key {feature} on line {line_number}")
        seen.add(feature)
        entries.append({"feature": feature, **row})
    if not entries:
        raise ValueError("feature catalog contains no feature keys")
    return entries


def context_layer_coverage(
    payload: Mapping[str, Any], feature_keys: list[str]
) -> dict[str, Any]:
    catalog = set(feature_keys)
    features = payload.get("features")
    returned = {
        str(item.get("name"))
        for item in features
        if isinstance(features, list) and isinstance(item, Mapping) and item.get("name")
    } if isinstance(features, list) else set()
    present = sorted(catalog & returned)
    absent = sorted(catalog - returned)
    return {
        "total_catalog_features": len(catalog),
        "present_count": len(present),
        "absent_count": len(absent),
        "present_features": present,
        "absent_features": absent,
        "unmatched_response_features": sorted(returned - catalog),
    }


def validate_catalog_entries(
    entries: list[dict[str, Any]], feature_keys: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Normalize draft catalog entries and enforce exact feature keys."""
    output: list[dict[str, Any]] = []
    warnings: list[str] = []
    allowed_dispositions = {"include", "deprioritize", "exclude"}
    for raw in entries:
        key = str(raw.get("key") or "").strip()
        if not key:
            continue
        related: list[str] = []
        raw_related = raw.get("related_features")
        if isinstance(raw_related, str):
            raw_related = [item.strip() for item in raw_related.split(",")]
        if isinstance(raw_related, list):
            for item in raw_related[:3]:
                feature = str(item).strip()
                if not feature:
                    continue
                if feature in feature_keys:
                    related.append(feature)
                else:
                    warnings.append(f"{key}: removed unverified feature key {feature}")
        try:
            rank = max(1, min(5, int(raw.get("usefulness_rank", 1))))
        except (TypeError, ValueError):
            rank = 1
        disposition = str(raw.get("disposition") or "deprioritize").casefold()
        if disposition not in allowed_dispositions:
            disposition = "deprioritize"
        aggregate_prompt = raw.get("aggregate_prompt") if rank >= 4 else None
        if aggregate_prompt and "cohort" not in str(aggregate_prompt).casefold():
            warnings.append(f"{key}: removed aggregate prompt that was not cohort-level")
            aggregate_prompt = None
        try:
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        uncertainty_reason = str(raw.get("uncertainty_reason") or "").strip()[:240] or None
        explicitly_approved = (
            str(raw.get("approval_status") or "") == "human_approved"
            or str(raw.get("review_status") or "") == "reviewed"
        )
        if disposition == "deprioritize" and explicitly_approved:
            disposition = "include"
        if disposition == "exclude":
            approval_status = "excluded"
        elif disposition == "deprioritize":
            approval_status = "review_required"
        else:
            approval_status = "auto_approved"
        output.append(
            {
                "key": key,
                "canonical_key": str(raw.get("canonical_key") or key).strip(),
                "value_category": str(raw.get("value_category") or "unknown").strip(),
                "related_features": related,
                "usefulness_rank": rank,
                "disposition": disposition,
                "aggregate_prompt": aggregate_prompt,
                "review_status": str(raw.get("review_status") or "draft"),
                "confidence": confidence,
                "uncertainty_reason": uncertainty_reason,
                "approval_status": approval_status,
            }
        )
    canonical_keys: dict[str, list[dict[str, Any]]] = {}
    for entry in output:
        canonical_keys.setdefault(str(entry["canonical_key"]), []).append(entry)
    for canonical, collisions in canonical_keys.items():
        if canonical and len(collisions) > 1:
            warnings.append(f"canonical key {canonical} is used by multiple variables")
            for entry in collisions:
                entry["approval_status"] = "excluded"
                entry["disposition"] = "exclude"
                entry["exclusion_reason"] = "canonical_key_conflict"
                entry["canonical_key_conflict"] = True
    return output, warnings


def prioritize_review_exceptions(
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return every deprioritized entry that still needs human approval."""
    output = [dict(entry) for entry in entries]
    unresolved = [entry for entry in output if entry.get("approval_status") == "review_required"]
    unresolved.sort(
        key=lambda entry: (
            entry.get("disposition") != "include",
            -int(entry.get("usefulness_rank") or 1),
            float(entry.get("confidence") or 0.0),
            not bool(entry.get("canonical_key_conflict")),
            str(entry.get("key") or ""),
        )
    )
    return unresolved, output


def _compact_product_context(
    referenced_features: set[str],
    feature_catalog_entries: list[dict[str, Any]] | None,
) -> dict[str, dict[str, str]]:
    product_context: dict[str, dict[str, str]] = {}
    for entry in feature_catalog_entries or []:
        feature = str(entry.get("feature") or "")
        if feature not in referenced_features:
            continue
        card: dict[str, str] = {}
        product_area = entry.get("Product Area") or entry.get("product_area")
        value_statement = entry.get("Value Statement") or entry.get("description")
        if product_area:
            card["a"] = str(product_area).strip()
        if value_statement:
            card["v"] = str(value_statement).strip()[:240]
        if card:
            product_context[feature] = card
    return dict(sorted(product_context.items()))


def compile_catalog_contract(
    entries: list[dict[str, Any]],
    *,
    feature_catalog_version_id: str | None,
    context_catalog_version_id: str | None,
    feature_catalog_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compile approved catalog rules into an organization-independent baseline."""
    rules: list[dict[str, Any]] = []
    referenced_features: set[str] = set()
    for entry in entries:
        approved = entry.get("approval_status") in {"auto_approved", "human_approved"}
        legacy_approved = entry.get("review_status") == "reviewed"
        if not (approved or legacy_approved) or entry.get("disposition") != "include":
            continue
        key = str(entry.get("key") or "")
        if not key:
            continue
        rule: dict[str, Any] = {
            "k": key,
            "c": str(entry.get("canonical_key") or key),
        }
        category = str(entry.get("value_category") or "").strip()
        if category and category != "unknown":
            rule["t"] = category
        rank = entry.get("usefulness_rank")
        if isinstance(rank, int):
            rule["u"] = rank
        related = entry.get("related_features")
        if isinstance(related, list) and related:
            rule["f"] = [str(item) for item in related]
            referenced_features.update(str(item) for item in related)
        rules.append(rule)
    rules.sort(key=lambda rule: (-int(rule.get("u", 0)), str(rule["k"])))
    context: dict[str, Any] = {"r": rules}
    product_context = _compact_product_context(
        referenced_features, feature_catalog_entries
    )
    if product_context:
        context["pc"] = product_context
    serialized = json.dumps(context, sort_keys=True, separators=(",", ":"))
    return {
        "context": context,
        "feature_catalog_version_id": feature_catalog_version_id,
        "context_catalog_version_id": context_catalog_version_id,
        "metrics": {
            "included_variables": len(rules),
            "characters": len(serialized),
            "bytes": len(serialized.encode("utf-8")),
            "estimated_tokens": math.ceil(len(serialized) / 4),
        },
    }


def compile_context(
    sources: Mapping[str, Any],
    entries: list[dict[str, Any]],
    *,
    feature_catalog_version_id: str | None,
    context_catalog_version_id: str | None,
    feature_catalog_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compile reviewed catalog rules against one scrubbed organization response."""
    values: dict[str, Any] = {}
    snowflake = sources.get("snowflake")
    rows = snowflake.get("rows") if isinstance(snowflake, Mapping) else None
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, Mapping):
                key = _row_value(row, "variable_name")
                if isinstance(key, str) and key and _row_has(row, "value"):
                    values[key] = _row_value(row, "value")
    context_layer = sources.get("context_layer")
    if isinstance(context_layer, Mapping):
        values.update(_context_layer_values(context_layer))
    compiled_values: dict[str, Any] = {}
    nulls: list[str] = []
    features: dict[str, list[str]] = {}
    included = 0
    referenced_features: set[str] = set()
    for entry in entries:
        approved = entry.get("approval_status") in {"auto_approved", "human_approved"}
        legacy_approved = entry.get("review_status") == "reviewed"
        if not (approved or legacy_approved) or entry.get("disposition") != "include":
            continue
        key = str(entry.get("key") or "")
        if key not in values:
            continue
        canonical = str(entry.get("canonical_key") or key)
        included += 1
        if values[key] is None:
            nulls.append(canonical)
        else:
            compiled_values[canonical] = values[key]
        related = entry.get("related_features")
        if isinstance(related, list) and related:
            features[canonical] = [str(item) for item in related]
            referenced_features.update(str(item) for item in related)
    context: dict[str, Any] = {"v": compiled_values}
    if nulls:
        context["n"] = sorted(nulls)
    if features:
        context["f"] = features
    product_context = _compact_product_context(
        referenced_features, feature_catalog_entries
    )
    if product_context:
        context["pc"] = product_context
    serialized = json.dumps(context, sort_keys=True, separators=(",", ":"))
    return {
        "context": context,
        "feature_catalog_version_id": feature_catalog_version_id,
        "context_catalog_version_id": context_catalog_version_id,
        "metrics": {
            "included_variables": included,
            "characters": len(serialized),
            "bytes": len(serialized.encode("utf-8")),
            "estimated_tokens": math.ceil(len(serialized) / 4),
        },
    }


def load_fixture(path: Path = _FIXTURE_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise TypeError("workbench fixture was not an object")
    return payload


def build_product_index(catalog: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "product_key": key,
            "capability": str(entry.get("description", "")),
            "use_case": entry.get("use_case"),
            "eligibility": entry.get("eligibility"),
        }
        for key, entry in sorted(catalog.items())
    ]


def resolve_product_cards(
    candidates: Sequence[Mapping[str, Any]], catalog: Mapping[str, Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    cards: list[dict[str, Any]] = []
    warnings: list[str] = []
    for candidate in candidates:
        mechanism = str(candidate.get("mechanism", "")).strip().lower()
        entry = catalog.get(mechanism)
        if entry is None:
            warnings.append(f"needs_product_context:{mechanism or 'unknown'}")
            continue
        cards.append({"product_key": mechanism, "card": redact(dict(entry))})
    return cards, warnings


def build_prompt_context(context: Mapping[str, Any], catalog: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Keep the broad context and add only a compact product index."""
    proposed = dict(context)
    proposed["product_index"] = build_product_index(catalog)
    return proposed


def load_catalog(path: Path | None = None) -> dict[str, dict[str, str]]:
    catalog_path = path or Path(__file__).parents[2] / "data" / "hcp_feature_catalog.csv"
    import csv

    output: dict[str, dict[str, str]] = {}
    with catalog_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = (row.get("feature") or "").strip()
            if key and key not in output:
                output[key] = {
                    "description": (row.get("description") or "").strip(),
                    "use_case": (row.get("notes") or "").strip(),
                    "eligibility": (row.get("works_on") or "").strip(),
                }
    return output


def build_evolve_prompt(context: Mapping[str, Any], *, count: int, channels: list[str], journey_window: str) -> str:
    return evolve_prompt(
        json.dumps(context, indent=2, sort_keys=True),
        mode="shift",
        best_json=None,
        history_json="[]",
        tried_mechanisms=[],
        channels=channels,
        journey_window=journey_window,
        evidence="No historical outcome evidence supplied.",
        count=count,
    )


async def run_model(
    prompt: str,
    *,
    api_key: str,
    model: str,
    stage: str,
    system: str = EVOLVE_SYSTEM,
    max_tokens: int = 8000,
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None,
) -> tuple[str, dict[str, Any]]:
    import anthropic

    started = time.perf_counter()
    client = anthropic.AsyncAnthropic(api_key=api_key)

    async def create_message() -> Any:
        kwargs: dict[str, Any] = {}
        if effort is not None:
            kwargs["output_config"] = {"effort": effort}
        return await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )

    try:
        response = await retry_rate_limit(create_message)
    finally:
        await client.close()
    usage = response.usage
    text = "".join(str(getattr(block, "text", "")) for block in response.content if getattr(block, "type", "") == "text")
    pricing = Pricing({"fast": model})
    metrics = {
        "stage": stage,
        "model": model,
        "stop_reason": getattr(response, "stop_reason", None),
        "input_tokens": int(usage.input_tokens),
        "output_tokens": int(usage.output_tokens),
        "cost_usd": float(pricing.cost(model, int(usage.input_tokens), int(usage.output_tokens))),
        "duration_ms": round((time.perf_counter() - started) * 1000),
    }
    return text, metrics


def parse_candidates(text: str) -> list[dict[str, Any]]:
    value = extract_json(text)
    if not isinstance(value, list):
        raise TypeError("model response was not a candidate array")
    return [dict(item) for item in value if isinstance(item, Mapping)]
