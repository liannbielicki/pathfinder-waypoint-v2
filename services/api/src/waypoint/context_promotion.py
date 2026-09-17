"""Immutable Context Workbench promotions consumed by everyday Waypoint."""

from __future__ import annotations

import csv
import io
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

DEFAULT_PROMOTION_ROOT = Path(__file__).parents[2] / ".workbench" / "promotions"
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_FEATURE_FIELDS = {
    "feature",
    "Product Area",
    "product_area",
    "category",
    "Value Statement",
    "description",
}


def _approved_include(entry: Mapping[str, Any]) -> bool:
    approved = entry.get("approval_status") in {"auto_approved", "human_approved"}
    return entry.get("disposition") == "include" and (
        approved or entry.get("review_status") == "reviewed"
    )


def build_promotion_bundle(
    entries: Sequence[Mapping[str, Any]],
    *,
    feature_catalog_entries: Sequence[Mapping[str, Any]],
    promotion_id: str,
    context_catalog_version_id: str,
    feature_catalog_version_id: str,
    created_at: str,
) -> dict[str, Any]:
    rules = []
    for entry in entries:
        if not _approved_include(entry):
            continue
        source_key = str(entry.get("key") or "").strip()
        canonical_key = str(entry.get("canonical_key") or source_key).strip()
        if not source_key or not canonical_key:
            continue
        related = entry.get("related_features")
        rules.append({
            "source_key": source_key,
            "canonical_key": canonical_key,
            "source_table": str(entry.get("source_table") or "UNKNOWN").strip() or "UNKNOWN",
            "cohort_aggregate_prompt": str(entry.get("aggregate_prompt") or "").strip(),
            "related_features": sorted({str(item) for item in related})
            if isinstance(related, list)
            else [],
        })
    rules.sort(key=lambda rule: str(rule["canonical_key"]))
    return {
        "id": promotion_id,
        "created_at": created_at,
        "context_catalog_version_id": context_catalog_version_id,
        "feature_catalog_version_id": feature_catalog_version_id,
        "rules": rules,
        "feature_catalog": [
            {key: value for key, value in entry.items() if key in _SAFE_FEATURE_FIELDS}
            for entry in feature_catalog_entries
            if entry.get("feature")
        ],
    }


def promotion_csv(bundle: Mapping[str, Any]) -> str:
    output = io.StringIO(newline="")
    headers = ["canonical_key", "source_table", "cohort_aggregate_prompt"]
    writer = csv.DictWriter(output, fieldnames=headers, lineterminator="\n")
    writer.writeheader()
    rules = bundle.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if isinstance(rule, Mapping):
                writer.writerow({header: str(rule.get(header) or "") for header in headers})
    return output.getvalue()


def _feature_cards(feature_catalog: object) -> dict[str, dict[str, str]]:
    cards: dict[str, dict[str, str]] = {}
    if not isinstance(feature_catalog, list):
        return cards
    for entry in feature_catalog:
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("feature") or "")
        if not key:
            continue
        card: dict[str, str] = {}
        area = entry.get("Product Area") or entry.get("product_area")
        value = entry.get("Value Statement") or entry.get("description")
        if area:
            card["a"] = str(area).strip()
        if value:
            card["v"] = str(value).strip()[:240]
        cards[key] = card
    return dict(sorted(cards.items()))


def compile_promoted_context(
    values: Mapping[str, Any], bundle: Mapping[str, Any]
) -> dict[str, Any]:
    compiled: dict[str, Any] = {}
    nulls: list[str] = []
    features: dict[str, list[str]] = {}
    rules = bundle.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, Mapping):
                continue
            canonical = str(rule.get("canonical_key") or "")
            source = str(rule.get("source_key") or canonical)
            value_key = canonical if canonical in values else source
            if not canonical or value_key not in values:
                continue
            value = values[value_key]
            if value is None:
                nulls.append(canonical)
            else:
                compiled[canonical] = value
            related = rule.get("related_features")
            if isinstance(related, list) and related:
                exact = [str(item) for item in related]
                features[canonical] = exact
    context: dict[str, Any] = {"v": dict(sorted(compiled.items()))}
    if nulls:
        context["n"] = sorted(nulls)
    if features:
        context["f"] = dict(sorted(features.items()))
    cards = _feature_cards(bundle.get("feature_catalog"))
    if cards:
        context["pc"] = cards
    return context


class PromotionStore:
    def __init__(self, root: Path = DEFAULT_PROMOTION_ROOT) -> None:
        self.root = root

    def _path(self, promotion_id: str) -> Path:
        if not _SAFE_ID.fullmatch(promotion_id):
            raise ValueError("promotion id contains unsupported characters")
        return self.root / f"{promotion_id}.json"

    def _write_json(self, path: Path, value: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")))
        os.replace(temporary, path)

    def read(self, promotion_id: str) -> dict[str, Any] | None:
        path = self._path(promotion_id)
        if not path.exists():
            return None
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise TypeError("promotion bundle is not an object")
        return value

    def read_active(self) -> dict[str, Any] | None:
        pointer = self.root / "active.json"
        if not pointer.exists():
            return None
        value = json.loads(pointer.read_text())
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise TypeError("active promotion pointer is invalid")
        return self.read(value["id"])

    def promote(self, bundle: Mapping[str, Any]) -> None:
        promotion_id = str(bundle.get("id") or "")
        path = self._path(promotion_id)
        incoming = dict(bundle)
        existing = self.read(promotion_id)
        if existing is not None and existing != incoming:
            raise ValueError("promotion id already exists with different content")
        if existing is None:
            self._write_json(path, incoming)
        self._write_json(self.root / "active.json", {"id": promotion_id})
