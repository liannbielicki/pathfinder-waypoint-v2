from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from pathfinder.action_console.models import AudienceBoundary, OrgUuidEvidence
from pathfinder.store.supabase_sink import ACTION_AUDIENCE_INDEX_TABLE, SupabaseSink

# Default fixture locations, resolved relative to the repo root (this file lives at
# src/pathfinder/action_console/audience.py → up three parents == repo root).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_AUDIENCE_PATH = (
    _REPO_ROOT / "data" / "fixtures" / "action_console" / "audience_seed.json"
)
_DEFAULT_FACTOR_LIBRARY_PATH = (
    _REPO_ROOT / "data" / "fixtures" / "action_console" / "factor_library_seed.json"
)

_AUDIENCE_PATH_ENV = "PATHFINDER_ACTION_CONSOLE_AUDIENCE_PATH"
_FACTOR_LIBRARY_PATH_ENV = "PATHFINDER_ACTION_CONSOLE_FACTOR_LIBRARY_PATH"
_AUDIENCE_SOURCE_ENV = "PATHFINDER_ACTION_CONSOLE_AUDIENCE_SOURCE"

# Columns that must never become a selectable factor group.
_IDENTITY_COLUMNS = frozenset({"org_uuid", "org_id"})
_TIMESTAMP_COLUMNS = frozenset({"observed_at"})

# Bounds for treating a discovered column as a bounded categorical/banded factor.
# Below 2 distinct values it carries no signal; above the upper bound it is treated
# as high-cardinality free-text and dropped.
_MIN_DISTINCT = 2
_MAX_DISTINCT = 40

_AUDIENCE_INDEX_FACTOR_KEYS = (
    "cell",
    "plan",
    "lifecycle_bucket",
    "usage_band",
    "csr_ai_status",
    "open_ar_band",
    "cc_status",
    "team_member_status",
    "recent_outreach_status",
    "retention_case_status",
    "jobs_created_7d_band",
    "estimates_created_28d_band",
    "invoices_sent_28d_band",
)

# Local/demo fallback factor groups. Used ONLY when no factor-library fixture is
# available. Discovered Snowflake/library groups take precedence over these.
_STARTER_FACTOR_GROUPS: list[dict[str, Any]] = [
    {"key": "cell", "label": "Cell", "values": ["2A", "4A"]},
    {"key": "plan", "label": "Plan", "values": ["basic", "essentials", "max"]},
    {
        "key": "lifecycle_bucket",
        "label": "Org tenure band",
        "values": ["0-3m", "4-12m", "13-36m", "37m+"],
    },
    {"key": "usage_band", "label": "Usage", "values": ["none", "low", "medium", "high"]},
    {
        "key": "online_booking_status",
        "label": "Online booking",
        "values": ["active", "inactive"],
    },
    {"key": "login_status", "label": "Login", "values": ["recent", "stale", "none"]},
    {
        "key": "billing_status",
        "label": "Billing",
        "values": ["current", "past_due", "failed_payment"],
    },
    {"key": "csr_ai_status", "label": "CSR AI", "values": ["active", "inactive"]},
    {"key": "cc_status", "label": "CC", "values": ["active", "inactive"]},
    {"key": "mobile_app_status", "label": "Mobile app", "values": ["active", "inactive"]},
    {
        "key": "team_member_status",
        "label": "Team members",
        "values": ["solo", "team_added", "team_expanded"],
    },
    {
        "key": "recent_outreach_status",
        "label": "Recent outreach",
        "values": ["none", "email", "call", "sms", "human_touch"],
    },
]


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _resolve_audience_path(path: Path | None) -> Path:
    if path is not None:
        return Path(path)
    env_val = os.environ.get(_AUDIENCE_PATH_ENV)
    if env_val:
        return Path(env_val)
    return _DEFAULT_AUDIENCE_PATH


def _resolve_factor_library_path(path: Path | None) -> Path | None:
    if path is not None:
        return Path(path)
    env_val = os.environ.get(_FACTOR_LIBRARY_PATH_ENV)
    if env_val:
        return Path(env_val)
    if _DEFAULT_FACTOR_LIBRARY_PATH.exists():
        return _DEFAULT_FACTOR_LIBRARY_PATH
    return None


def _audience_source(source: str | None) -> str:
    return (source or os.environ.get(_AUDIENCE_SOURCE_ENV) or "local").strip().lower()


def _starter_factor_groups() -> list[dict[str, Any]]:
    """Normalize starter groups into the library group shape."""
    out: list[dict[str, Any]] = []
    for g in _STARTER_FACTOR_GROUPS:
        out.append(
            {
                "key": g["key"],
                "label": g["label"],
                "source_columns": [],
                "values": [
                    {"value": v, "label": str(v), "org_count": None}
                    for v in g["values"]
                ],
                "source_kind": "starter_fallback",
            }
        )
    return out


def available_factors(
    path: Path | None = None,
    *,
    source: str | None = None,
    sink: Any | None = None,
) -> list[dict[str, Any]]:
    """Return discovered factor groups for the UI drawer.

    Prefers the factor-library fixture (explicit ``path``, the
    ``PATHFINDER_ACTION_CONSOLE_FACTOR_LIBRARY_PATH`` env override, or the default
    fixture) so that discovered Snowflake/materialized-extract groups (e.g.
    ``bookings_7d_band``) surface beyond the starter seed list. Falls back to the
    local starter groups only when no library is available.
    """
    if _audience_source(source) == "supabase":
        actual_sink = sink if sink is not None else SupabaseSink()
        rows = actual_sink.list_action_audience_index({})
        if rows is None:
            return []
        return discover_factor_groups(
            [normalize_audience_index_row(row) for row in rows]
        )

    lib_path = _resolve_factor_library_path(path)
    if lib_path is not None and lib_path.exists():
        data = _load_json(lib_path)
        groups = data.get("groups", []) or []
        if groups:
            return copy.deepcopy(groups)
    return _starter_factor_groups()


def discover_factor_groups(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build factor groups from audience extract rows.

    Flattens each row's ``matched_factors`` (plus any top-level scalar columns) and
    keeps only bounded categorical / banded numeric columns: identity columns
    (``org_uuid``/``org_id``), timestamp columns (``observed_at``), and
    high-cardinality free-text columns are dropped.
    """
    # Collect candidate column -> set of distinct non-null string values.
    candidates: dict[str, set[str]] = {}
    # Preserve first-seen order for stable output.
    order: list[str] = []

    def _consider(col: str, value: Any) -> None:
        if col in _IDENTITY_COLUMNS or col in _TIMESTAMP_COLUMNS:
            return
        if value is None:
            return
        if not isinstance(value, (str, int, float, bool)):
            # Non-scalar (lists/dicts) are not selectable factor values.
            return
        if col not in candidates:
            candidates[col] = set()
            order.append(col)
        candidates[col].add(str(value))

    for row in rows:
        mf = row.get("matched_factors")
        if isinstance(mf, dict):
            for col, value in mf.items():
                _consider(col, value)
        for col, value in row.items():
            if col == "matched_factors":
                continue
            _consider(col, value)

    groups: list[dict[str, Any]] = []
    for col in order:
        values = candidates[col]
        distinct = len(values)
        if distinct < _MIN_DISTINCT or distinct > _MAX_DISTINCT:
            # Too few values (no signal) or high-cardinality free-text → drop.
            continue
        groups.append(
            {
                "key": col,
                "label": col,
                "source_columns": [col],
                "values": [
                    {"value": v, "label": str(v), "org_count": None}
                    for v in sorted(values)
                ],
                "source_kind": "discovered",
            }
        )
    return groups


def derive_audience_id(filters: dict[str, list[str]]) -> str:
    """Stable, order-independent id derived from the selected filters."""
    normalized = {
        str(key): sorted(str(v) for v in (values or []))
        for key, values in filters.items()
    }
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"aud-{digest}"


def _row_matches(matched_factors: dict[str, str], filters: dict[str, list[str]]) -> bool:
    # AND across keys, OR within a key's value list.
    for key, allowed in filters.items():
        if not allowed:
            continue
        actual = matched_factors.get(key)
        if actual is None or actual not in set(allowed):
            return False
    return True


def _label_for(filters: dict[str, list[str]], count: int) -> str:
    if not filters:
        return f"Full audience ({count} orgs)"
    parts = [
        f"{key}={'/'.join(str(v) for v in values)}"
        for key, values in sorted(filters.items())
    ]
    return f"{', '.join(parts)} ({count} orgs)"


def normalize_audience_index_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize one Snowflake/Supabase audience-index row to runtime keys."""

    def _get(key: str) -> Any:
        return raw.get(key, raw.get(key.upper()))

    matched_factors = copy.deepcopy(
        raw.get("matched_factors") or raw.get("MATCHED_FACTORS") or {}
    )
    if not isinstance(matched_factors, dict):
        matched_factors = {}

    normalized: dict[str, Any] = {
        "org_uuid": _get("org_uuid"),
        "org_id": _get("org_id"),
        "evidence_source": _get("evidence_source") or "supabase_audience_index",
        "observed_at": _get("observed_at") or "",
    }
    for key in _AUDIENCE_INDEX_FACTOR_KEYS:
        value = _get(key)
        if value not in (None, ""):
            matched_factors[key] = str(value)
            normalized[key] = str(value)
    normalized["matched_factors"] = {
        str(k): str(v) for k, v in matched_factors.items() if v not in (None, "")
    }
    return normalized


def _evidence_from_index_row(raw: dict[str, Any]) -> OrgUuidEvidence:
    row = normalize_audience_index_row(raw)
    if not row.get("org_uuid"):
        raise ValueError("audience index row missing org_uuid")
    return OrgUuidEvidence(
        org_uuid=str(row["org_uuid"]),
        org_id=None if row.get("org_id") is None else str(row.get("org_id")),
        matched_factors=copy.deepcopy(row.get("matched_factors", {}) or {}),
        evidence_source=str(row.get("evidence_source") or "supabase_audience_index"),
        observed_at=str(row.get("observed_at") or ""),
    )


def _resolve_supabase_audience(
    filters: dict[str, list[str]],
    *,
    sink: Any | None = None,
) -> AudienceBoundary:
    actual_sink = sink if sink is not None else SupabaseSink()
    rows = actual_sink.list_action_audience_index(filters)
    unavailable = rows is None
    matched: list[OrgUuidEvidence] = []
    invalid_row_count = 0
    if rows is not None:
        for row in rows:
            try:
                matched.append(_evidence_from_index_row(row))
            except ValueError:
                invalid_row_count += 1
    count = len(matched)
    manifest: dict[str, Any] = {
        "source_kind": "supabase_audience_index",
        "source_table": ACTION_AUDIENCE_INDEX_TABLE,
        "row_count": count,
    }
    if unavailable:
        manifest["warning"] = "Supabase audience index unavailable"
    if invalid_row_count:
        manifest["invalid_row_count"] = invalid_row_count

    return AudienceBoundary(
        audience_id=derive_audience_id(filters),
        label=_label_for(filters, count),
        filters={k: list(v) for k, v in filters.items()},
        factor_summary=[],
        org_uuid_evidence=matched,
        extract_manifest=manifest,
    )


def resolve_audience(
    filters: dict[str, list[str]],
    *,
    path: Path | None = None,
    source: str | None = None,
    sink: Any | None = None,
) -> AudienceBoundary:
    """Return exact UUID evidence for rows matching ALL selected filters.

    AND semantics across filter keys, OR within a single key's value list. Unknown
    filter values yield an empty evidence list (``exact_uuid_count() == 0``).
    """
    if _audience_source(source) == "supabase":
        return _resolve_supabase_audience(filters, sink=sink)

    source_path = _resolve_audience_path(path)
    data = _load_json(source_path)

    raw_rows = data.get("org_uuid_evidence", []) or []
    matched: list[OrgUuidEvidence] = []
    for raw in raw_rows:
        mf = {
            **(raw.get("matched_factors", {}) or {}),
            "org_uuid": raw.get("org_uuid"),
            "org_id": raw.get("org_id"),
        }
        if _row_matches(mf, filters):
            matched.append(OrgUuidEvidence.from_dict(raw))

    count = len(matched)

    manifest = copy.deepcopy(data.get("extract_manifest", {}) or {})
    manifest["row_count"] = count

    factor_summary = copy.deepcopy(data.get("factor_summary", []) or [])

    return AudienceBoundary(
        audience_id=derive_audience_id(filters),
        label=_label_for(filters, count),
        filters={k: list(v) for k, v in filters.items()},
        factor_summary=factor_summary,
        org_uuid_evidence=matched,
        extract_manifest=manifest,
    )
