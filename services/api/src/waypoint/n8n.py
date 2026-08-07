"""Client for the existing n8n/Snowflake org-context flow (contract org-context-v2).

n8n holds the Snowflake credential; Waypoint holds none. The contract between
the two is exactly three things: a `contract_version` literal both sides agree
on, a closed allowlist of band-valued field names, and the rule that values are
bands/states (never raw amounts, counts, dates, or identifiers). Waypoint keeps
only allowlisted fields — dropping everything else, which is the PII guard — and
tolerates missing ones. So the Snowflake query can change freely as long as it
stamps CONTRACT_VERSION and emits only allowlisted band columns:

  * rebin/rewrite an existing field  -> no Waypoint change
  * add a field                      -> add its name to ALLOWED_FIELDS here
  * remove a field                   -> no Waypoint change (degrades to None)

Redirects are refused so the bearer token is never forwarded.
"""

import logging
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

log = logging.getLogger("waypoint.n8n")

CONTRACT_VERSION = "org-context-v2"

# The closed allowlist: only these band/state fields (plus org_uuid and
# contract_version) may cross the boundary. The client drops anything else.
ALLOWED_FIELDS = (
    "feature_adoption_band", "plan_gap_band", "ltv_score_band", "open_ar_band",
    "ar_aging_band", "jobs_created_28d_band", "estimates_created_28d_band",
    "invoices_sent_28d_band", "outreach_count_28d_band", "sms_consent_state",
    "email_consent_state", "feature_online_booking_state",
    "feature_premium_reviews_state", "feature_sales_proposal_state",
    "feature_service_agreements_state", "feature_hcp_assist_state",
    "feature_quickbooks_state", "feature_voip_state", "feature_card_on_file_state",
    "feature_time_tracking_state", "feature_flat_rate_pricing_state",
    "recommended_focus", "recommended_focus_value_band",
    "recommended_focus_retention_lift_band", "top_unused_paid_feature",
    "vertical", "plan_tier", "org_size_band", "tenure_band", "segment",
)

# v2 band field -> the permitted persona-match feature key it maps onto
# (personas.PERMITTED_MATCH_FEATURES). segment is the load-bearing key: real
# persona-cards items are flat and share only segment with a Pro, so without it
# every fit is 0.0. v2 has no lifecycle_stage / features_active_count source,
# so those keys stay absent.
_MATCH_FEATURE_MAP = {
    "segment": "segment",
    "plan_tier": "plan",
    "tenure_band": "tenure_bucket",
    "org_size_band": "org_size_bucket",
    "vertical": "trade_bucket",
    "open_ar_band": "open_ar_band",
}


class ContextUnavailable(Exception):
    """The context flow could not produce a valid batch. Explicit, never empty."""


class OrgBrief(BaseModel):
    # Band-only by design; every field optional so a dropped column degrades to
    # None instead of crashing. extra="forbid" is safe because the client
    # projects each wire row to the allowlist before constructing.
    model_config = ConfigDict(extra="forbid")

    org_uuid: str
    segment: str | None = None
    feature_adoption_band: str | None = None
    plan_gap_band: str | None = None
    ltv_score_band: str | None = None
    open_ar_band: str | None = None
    ar_aging_band: str | None = None
    jobs_created_28d_band: str | None = None
    estimates_created_28d_band: str | None = None
    invoices_sent_28d_band: str | None = None
    outreach_count_28d_band: str | None = None
    sms_consent_state: str | None = None
    email_consent_state: str | None = None
    feature_online_booking_state: str | None = None
    feature_premium_reviews_state: str | None = None
    feature_sales_proposal_state: str | None = None
    feature_service_agreements_state: str | None = None
    feature_hcp_assist_state: str | None = None
    feature_quickbooks_state: str | None = None
    feature_voip_state: str | None = None
    feature_card_on_file_state: str | None = None
    feature_time_tracking_state: str | None = None
    feature_flat_rate_pricing_state: str | None = None
    recommended_focus: str | None = None
    recommended_focus_value_band: str | None = None
    recommended_focus_retention_lift_band: str | None = None
    top_unused_paid_feature: str | None = None
    vertical: str | None = None
    plan_tier: str | None = None
    org_size_band: str | None = None
    tenure_band: str | None = None

    @property
    def pro_id(self) -> str:
        # Waypoint keys a run by the id it submitted; for org context that id IS
        # the org_uuid. Exposing it as pro_id keeps every downstream
        # `brief.pro_id` working unchanged.
        return self.org_uuid

    def match_feature_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for source, key in _MATCH_FEATURE_MAP.items():
            value = getattr(self, source)
            if value is not None:
                out[key] = value
        return out

    def calibration_cell(self) -> str | None:
        # ponytail: calibration cards are keyed `segment|plan|tenure` in v1
        # vocabulary; org-context-v2 has no segment and different tenure bands,
        # so no cell matches and scoring falls back to the global baseline.
        # Rebuild the calibration cards against v2 cells to restore per-cell
        # baselines.
        return None


class OrgContextBatch(BaseModel):
    # Internal envelope (the wire format is a bare array of rows). Kept so the
    # pipeline's `batch.organizations` stays unchanged.
    model_config = ConfigDict(extra="forbid")

    contract_version: str
    organizations: list[OrgBrief]


def _brief_from_row(row: dict[str, Any]) -> OrgBrief:
    """Project one wire row onto the allowlist: verify the version, keep only
    allowlisted fields (dropping any stray column — the PII guard), tolerate
    absent ones."""
    if not isinstance(row, dict):
        raise TypeError("context row is not an object")
    version = row.get("contract_version")
    if version != CONTRACT_VERSION:
        raise ValueError(f"expected contract {CONTRACT_VERSION!r}, got {version!r}")
    if "org_uuid" not in row:
        raise ValueError("context row missing org_uuid")
    # Restores the v1 "unexpected column" tripwire as a signal (not a hard
    # fail): the projection below still drops anything off the allowlist.
    dropped = set(row) - set(ALLOWED_FIELDS) - {"org_uuid", "contract_version"}
    if dropped:
        log.debug("dropped non-allowlisted context fields: %s", sorted(dropped))
    projected: dict[str, Any] = {"org_uuid": row["org_uuid"]}
    projected.update({k: row[k] for k in ALLOWED_FIELDS if k in row})
    return OrgBrief(**projected)


class N8NContextClient:
    def __init__(
        self,
        url: str,
        token: str,
        batch_size: int = 5,
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # ponytail: 5-id batches match the existing n8n validate-node cap.
        self.url = url
        self.batch_size = batch_size
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"authorization": f"Bearer {token}"},
        )

    async def fetch(self, pro_ids: list[str]) -> OrgContextBatch:
        # pro_ids are org UUIDs; the flow validates them as such.
        organizations: list[OrgBrief] = []
        for start in range(0, len(pro_ids), self.batch_size):
            chunk = pro_ids[start : start + self.batch_size]
            try:
                response = await self._client.post(self.url, json={"org_uuids": chunk})
            except httpx.HTTPError as error:
                raise ContextUnavailable(f"n8n context flow unreachable: {error}") from error
            if response.status_code != 200:
                raise ContextUnavailable(
                    f"n8n context flow returned {response.status_code} for {len(chunk)} ids"
                )
            rows = response.json()
            if not isinstance(rows, list):
                raise ContextUnavailable("n8n context response was not a list of rows")
            try:
                organizations.extend(_brief_from_row(row) for row in rows)
            except (ValueError, KeyError, TypeError) as error:
                raise ContextUnavailable(f"n8n context contract violation: {error}") from error
        return OrgContextBatch(contract_version=CONTRACT_VERSION, organizations=organizations)
