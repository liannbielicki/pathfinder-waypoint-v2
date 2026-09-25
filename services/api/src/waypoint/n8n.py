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

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from waypoint.context_promotion import PromotionStore, compile_promoted_context
from waypoint.workbench import scrub_pii

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
    "lifecycle_stage", "health_grade", "upsell_grade", "churn_risk_state",
    "payments_28d_band", "email_engagement_state",
    "marketing_campaigns_28d_band", "leads_created_28d_band",
    "jobs_reviewed_28d_band", "last_job_activity_band", "wisetack_state",
    "mrr_band", "platform_usage_band",
    "suggested_channel",
)

# v2 band field -> the permitted persona-match feature key it maps onto
# (personas.PERMITTED_MATCH_FEATURES). segment is the load-bearing key: real
# persona-cards items are flat and share only segment with a Pro, so without it
# every fit is 0.0. features_active_count still has no source, so that key
# stays absent.
_MATCH_FEATURE_MAP = {
    "segment": "segment",
    "lifecycle_stage": "lifecycle_stage",
    "plan_tier": "plan",
    "tenure_band": "tenure_bucket",
    "org_size_band": "org_size_bucket",
    "vertical": "trade_bucket",
    "open_ar_band": "open_ar_band",
}


class ContextUnavailable(Exception):
    """The context flow could not produce a valid batch. Explicit, never empty."""


class ContextConfigurationError(ContextUnavailable):
    """The configured context endpoint cannot satisfy the runtime contract."""


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
    lifecycle_stage: str | None = None
    health_grade: str | None = None
    upsell_grade: str | None = None
    churn_risk_state: str | None = None
    payments_28d_band: str | None = None
    email_engagement_state: str | None = None
    marketing_campaigns_28d_band: str | None = None
    leads_created_28d_band: str | None = None
    jobs_reviewed_28d_band: str | None = None
    last_job_activity_band: str | None = None
    wisetack_state: str | None = None
    mrr_band: str | None = None
    platform_usage_band: str | None = None
    suggested_channel: str | None = None
    curated_context: dict[str, Any] | None = Field(default=None, exclude=True)
    # Contact-plan inputs (one dict per active admin), never context: excluded
    # from model_dump so neither prompt path can serialize them.
    contact_candidates: list[dict[str, Any]] | None = Field(default=None, exclude=True)
    # Identifiers, not context: the numeric org id the run was keyed by and the
    # contact pro the context flow resolved for it (founding admin). The LCM
    # handoff sends pro_uuid; Iterable/Amplitude report on it.
    pro_uuid: str | None = None
    org_id: str | None = None

    @property
    def pro_id(self) -> str:
        # Waypoint keys a run by the id it submitted; for org context that id IS
        # the org_uuid. Exposing it as pro_id keeps every downstream
        # `brief.pro_id` working unchanged.
        return self.org_uuid

    def match_feature_map(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for source, key in _MATCH_FEATURE_MAP.items():
            value = getattr(self, source)
            if value is not None:
                out[key] = value
        for field_name in type(self).model_fields:
            if not field_name.startswith("feature_") or not field_name.endswith("_state"):
                continue
            state = getattr(self, field_name)
            if state is None:
                continue
            key = field_name.removeprefix("feature_").removesuffix("_state")
            key = {"online_booking": "booking"}.get(key, key)
            if state == "not_attached":
                out[f"{key}_attached"] = False
            elif state in {"attached_active", "attached_unused", "attached_usage_unknown"}:
                out[f"{key}_attached"] = True
        return out

    def suggested_outreach_channel(self) -> str | None:
        curated = (self.curated_context or {}).get("v") or {}
        value = curated.get("suggested_channel", self.suggested_channel)
        normalized = str(value or "").strip().lower()
        return normalized if normalized in {"sms", "email", "call"} else None

    def calibration_cell(self) -> str | None:
        """Disabled: the live tenure vocabulary can never match the cards.

        The cards key on `segment|plan|tenure` with tenure values
        "0-3m" / "4-12m" / "13-36m" / "37m+". The live org-context-v2 flow
        (see the `CASE` on `oi.tenure_mo` in
        n8n/waypoint-context-snowflake-v1.json) emits
        "under_1y" / "1_2y" / "2_4y" / "over_4y" instead. The two vocabularies
        do not overlap, so composing a key here would always miss and silently
        fall back to the global baseline anyway — indistinguishable from this
        `None`, but looking fixed when it isn't.

        ponytail: reconcile the vocabulary at the flow, not by translating in
        Python here — there is no exact mapping ("under_1y" spans both "0-3m"
        and "4-12m", whose baselines differ materially). Revisit once the flow
        emits the cards' bands directly.

        Whoever restores this must also revisit `KEEP_DELTA_PP` (0.6) and
        `MIN_REDUCTION_FLOOR_PP` (1.0): both are fixed pp constants tuned
        against the single global baseline. At r=4.8, 23 of the 166 cells have
        two reaction steps worth less than 0.6pp (minimum 0.044pp, cell
        `1D|max|37m+`), and 11 of the 166 cells cannot reach the 1.0pp floor
        at all even with a perfect 7/7/7 panel. Restoring per-cell baselines
        without re-tuning both constants reproduces the exact silent-failure
        class this change set removed.
        """
        return None


class OrgContextBatch(BaseModel):
    # Internal envelope (the wire format is a bare array of rows). Kept so the
    # pipeline's `batch.organizations` stays unchanged.
    model_config = ConfigDict(extra="forbid")

    contract_version: str
    organizations: list[OrgBrief]
    # Version stamp the flow's SQL-building code node emits (e.g. audience_v8);
    # None when the flow predates the stamp. Run metadata, not an org field.
    audience_query_version: str | None = None


def _brief_from_row(
    row: dict[str, Any],
    promotion: Mapping[str, Any] | None = None,
    *,
    promotion_required: bool = False,
) -> OrgBrief:
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
    aliases = {"RECOMMENDED_ACTION", "recommended_action"}
    dropped = set(row) - set(ALLOWED_FIELDS) - aliases - {"org_uuid", "contract_version"}
    if dropped:
        log.debug("dropped non-allowlisted context fields: %s", sorted(dropped))
    projected: dict[str, Any] = {"org_uuid": row["org_uuid"]}
    projected.update({k: row[k] for k in ALLOWED_FIELDS if k in row})
    for alias in ("RECOMMENDED_ACTION", "recommended_action"):
        if alias in row and "suggested_channel" not in projected:
            projected["suggested_channel"] = row[alias]
    bundle = promotion
    if promotion_required and bundle is None:
        raise ValueError("staging context promotion artifact is missing")
    if bundle is not None:
        by_case = {str(key).casefold(): value for key, value in row.items()}
        promoted_values: dict[str, Any] = {}
        rules = bundle.get("rules")
        if isinstance(rules, list):
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                canonical = str(rule.get("canonical_key") or "")
                if canonical.casefold() in by_case:
                    safe_value, pii_ledger = scrub_pii({
                        canonical: by_case[canonical.casefold()]
                    })
                    if not pii_ledger and canonical in safe_value:
                        promoted_values[canonical] = safe_value[canonical]
                    else:
                        log.warning(
                            "dropped promoted value that failed the PII gate: %s",
                            canonical,
                        )
        projected["curated_context"] = compile_promoted_context(
            promoted_values, bundle
        )
    return OrgBrief(**projected)


# Identifier fields the flow echoes back on each row (execution 36657272):
# the resolved dashed uuid, the submitted pro_<hex> Iterable id, and the
# numeric org id (Snowflake emits it in either case).
_ROW_ID_KEYS = ("org_uuid", "pro_uuid", "organization_id", "ORGANIZATION_ID")


def _canon(identifier: str) -> str:
    """Canonical form shared by the three id spellings the flow accepts
    (numeric org id, pro_<hex32>, dashed uuid): lowercase, no pro_ prefix,
    no dashes."""
    return identifier.strip().lower().removeprefix("pro_").replace("-", "")


# Transient upstream trouble (gateway timeouts from a slow Snowflake query,
# rate limiting anywhere in the flow) — retried with backoff before a job
# attempt is burned. Anything else non-200 is treated as real.
_RETRIABLE_STATUSES = (429, 502, 503, 504)


class N8NContextClient:
    def __init__(
        self,
        url: str,
        token: str,
        batch_size: int = 5,
        timeout: float = 900.0,
        max_concurrent: int = 3,
        attempts: int = 3,
        backoff_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
        promotion_store: PromotionStore | None = None,
        promotion_loader: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
    ) -> None:
        # ponytail: 5-id batches match the existing n8n validate-node cap.
        self.url = url
        self.batch_size = batch_size
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        self._promotion_store = promotion_store
        self._promotion_loader = promotion_loader
        # One shared client instance serves every worker loop, so this
        # semaphore is the process-wide cap on concurrent n8n executions —
        # the flow (Snowflake + Iterable behind it) degrades badly when all
        # worker loops hit it at once.
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._client = client or httpx.AsyncClient(
            # The flow can legitimately run 10-15 minutes per id under load;
            # only the connect phase stays on a short fuse.
            timeout=httpx.Timeout(timeout, connect=15.0),
            follow_redirects=False,
            headers={"authorization": f"Bearer {token}"},
        )

    async def _post_chunk(
        self, chunk: list[str], on_retry: Callable[[], Awaitable[None]] | None = None
    ) -> httpx.Response:
        """One chunk, with the retry budget. `on_retry` runs immediately before
        each RE-attempt (never before the first), in the same shape as
        FleetSlots.acquire's on_wait.

        Why it exists: a read timeout is an httpx.HTTPError, so a stalled flow
        costs the FULL budget — 3 x the timeout plus backoff — and nothing
        heartbeats around this call (it runs before any stage handler), so the
        degraded case outlives the worker's lease and the job gets worked
        twice. The renewal goes here rather than the timeout coming down: the
        flow legitimately runs 10-15 minutes per call under load.
        """
        failure = "no attempt made"
        for attempt in range(self.attempts):
            if attempt:
                delay = self.backoff_seconds * 2 ** (attempt - 1)
                log.warning("n8n context retry %d for %d ids in %.0fs: %s",
                            attempt, len(chunk), delay, failure)
                await asyncio.sleep(delay)
                if on_retry is not None:
                    # After the backoff, so the lease is at its freshest going
                    # into another full-timeout attempt.
                    await on_retry()
            try:
                async with self._semaphore:
                    # Sent as generic "id"s: the flow owns classifying/routing
                    # each identifier (org vs pro, mixed lists welcome).
                    response = await self._client.post(self.url, json={"id": chunk})
            except httpx.HTTPError as error:
                failure = f"n8n context flow unreachable: {error}"
                continue
            if response.status_code in _RETRIABLE_STATUSES:
                failure = f"n8n context flow returned {response.status_code}"
                continue
            return response
        raise ContextUnavailable(f"{failure} (after {self.attempts} attempts)")

    async def fetch(
        self, pro_ids: list[str], on_retry: Callable[[], Awaitable[None]] | None = None
    ) -> OrgContextBatch:
        # Identifiers may be org or pro ids, even mixed; the flow validates
        # and routes them.
        organizations: list[OrgBrief] = []
        query_version: str | None = None
        promotion_required = (
            self._promotion_store is not None or self._promotion_loader is not None
        )
        promotion = (
            await self._promotion_loader()
            if self._promotion_loader is not None
            else self._promotion_store.read_active()
            if self._promotion_store is not None
            else None
        )
        for start in range(0, len(pro_ids), self.batch_size):
            chunk = pro_ids[start : start + self.batch_size]
            response = await self._post_chunk(chunk, on_retry)
            if response.status_code == 202:
                raise ContextConfigurationError(
                    "N8N_CONTEXT_URL returned asynchronous 202 Accepted; "
                    "it must target the synchronous Standard workflow that returns rows"
                )
            if response.status_code != 200:
                raise ContextUnavailable(
                    f"n8n context flow returned {response.status_code} for {len(chunk)} ids"
                )
            rows = response.json()
            if not isinstance(rows, list):
                raise ContextUnavailable("n8n context response was not a list of rows")
            query_version = query_version or next(
                (
                    str(row["audience_query_version"])
                    for row in rows
                    if isinstance(row, dict) and row.get("audience_query_version")
                ),
                None,
            )
            try:
                pairs = [
                    (
                        row,
                        _brief_from_row(
                            row,
                            promotion,
                            promotion_required=promotion_required,
                        ),
                    )
                    for row in rows
                ]
            except (ValueError, KeyError, TypeError) as error:
                raise ContextUnavailable(f"n8n context contract violation: {error}") from error
            # The flow accepts three id spaces (numeric org id, pro_<hex>
            # Iterable id, dashed org uuid) but always keys rows by the dashed
            # org_uuid — pro_<hex> is NOT the uuid respelled. It echoes the
            # resolved ids back on each row, so match the submitted id against
            # all of them (raw row: the projection drops id fields), then
            # re-key the brief to the submitted id so pipeline matching
            # (brief.pro_id == run pro_id) holds regardless of format.
            submitted = {_canon(identifier): identifier for identifier in chunk}
            for row, brief in pairs:
                echoed = (row.get(k) for k in _ROW_ID_KEYS)
                original = next(
                    (
                        found
                        for value in echoed
                        if value is not None
                        and (found := submitted.get(_canon(str(value)))) is not None
                    ),
                    None,
                )
                if original is not None:
                    brief = brief.model_copy(update={"org_uuid": original})
                else:
                    log.warning("context row %s matches no submitted id", brief.org_uuid)
                organizations.append(brief)
        return OrgContextBatch(
            contract_version=CONTRACT_VERSION,
            organizations=organizations,
            audience_query_version=query_version,
        )
