"""Local, read-only context inspection primitives."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from waypoint.llm import Pricing, extract_json, retry_rate_limit
from waypoint.prompts import EVOLVE_SYSTEM, evolve_prompt

_SECRET_WORDS = ("key", "token", "secret", "authorization", "password", "credential")
_SAFE_CATALOG_FIELDS = {"canonical_key", "value_category", "related_features", "usefulness_rank", "aggregate_prompt"}
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
            output: dict[str, Any] = {}
            for key, child in item.items():
                child_path = f"{path}.{key}" if path else str(key)
                result = walk(child, child_path, str(key).casefold())
                if result is not _DROP:
                    output[str(key)] = result
            return output
        if isinstance(item, list):
            output = []
            for index, child in enumerate(item):
                result = walk(child, f"{path}[{index}]", leaf)
                if result is not _DROP:
                    output.append(result)
            return output
        if isinstance(item, str):
            if leaf in _METADATA_KEYS:
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
        if leaf not in _METADATA_KEYS and leaf not in _NON_PII_BUSINESS_KEYS and any(word in leaf for word in _PII_WORDS):
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
            raise ValueError("Context Layer response was not an object")
        return payload


class N8NContextClient:
    """Read-only client for the authenticated Snowflake context webhook."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 60.0) -> None:
        self._transport = transport
        self._timeout = httpx.Timeout(timeout, connect=min(timeout, 10.0))

    async def fetch(self, organization_id: str, webhook_url: str, token: str) -> dict[str, Any] | list[Any]:
        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout, follow_redirects=False) as client:
            response = await client.post(webhook_url, headers={"Authorization": f"Bearer {token}"}, json={"organization_id": organization_id})
        if response.status_code != 200:
            raise ValueError(f"Snowflake/n8n returned HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise ValueError("Snowflake/n8n response was not an object or array")
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


def load_fixture(path: Path = _FIXTURE_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("workbench fixture was not an object")
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
    candidates: list[Mapping[str, Any]], catalog: Mapping[str, Mapping[str, Any]]
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
) -> tuple[str, dict[str, Any]]:
    import anthropic

    started = time.perf_counter()
    client = anthropic.AsyncAnthropic(api_key=api_key)
    try:
        response = await retry_rate_limit(
            lambda: client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        )
    finally:
        await client.close()
    usage = response.usage
    text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
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
        raise ValueError("model response was not a candidate array")
    return [dict(item) for item in value if isinstance(item, Mapping)]
