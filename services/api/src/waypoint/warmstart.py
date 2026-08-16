"""Warm-start fingerprints: the only sanitized shape allowed to cross orgs.

A winner earns the right to seed future runs for SIMILAR pros only after a real
observed 7-day return outcome (see outcomes.ingest) — persona scores never
qualify. Cross-org reuse therefore travels as a versioned, allowlisted band
fingerprint: structured bands/states only, never raw ids, names, amounts, free
text, rationale, or any other org-specific fact. FINGERPRINT_FIELDS is a strict
subset of n8n.ALLOWED_FIELDS and deliberately excludes org_uuid.

Retrieval is deliberately Postgres + Python: a bounded, index-backed scan of
eligible winners scored by weighted exact-match similarity. No RAG, no vector
store — `retrieve` is the whole interface, so a different retrieval system can
replace its body without touching the pipeline.
"""

import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.n8n import OrgBrief
from waypoint.tables import WinnerRow

log = logging.getLogger("waypoint.warmstart")

FINGERPRINT_VERSION = "fp_v1"

FINGERPRINT_FIELDS = (
    "segment", "vertical", "plan_tier", "org_size_band", "tenure_band",
    "lifecycle_stage", "churn_risk_state", "health_grade", "platform_usage_band",
    "feature_adoption_band", "jobs_created_28d_band", "open_ar_band", "mrr_band",
    "email_engagement_state",
)


def build_fingerprint(brief: OrgBrief) -> dict[str, str]:
    """The allowlisted band fields present on the brief, verbatim. Nothing else
    can enter the dict — absent fields are simply omitted."""
    values = ((field, getattr(brief, field)) for field in FINGERPRINT_FIELDS)
    return {field: value for field, value in values if value is not None}


# Weights are the whole tuning surface of similarity. Uniform 1.0 everywhere
# except the three fields that actually determine whether a mechanism transfers:
# segment (the persona/match cohort), lifecycle_stage (where the pro is in its
# life), and churn_risk_state (why we are touching them at all). A shared
# vertical or plan tier is much weaker evidence that the same touch will land.
DEFAULT_SIMILARITY_WEIGHTS: dict[str, float] = {
    **{field: 1.0 for field in FINGERPRINT_FIELDS},
    "segment": 2.0,
    "lifecycle_stage": 2.0,
    "churn_risk_state": 2.0,
}

# Bounded scan: newest eligible winners only. Cheap, index-backed, and enough —
# ponytail: raise it (or replace retrieve's body) if telemetry ever shows the
# best match falling off the end of the window.
DEFAULT_SCAN_LIMIT = 200


def similarity(
    query_fp: dict[str, str],
    candidate_fp: dict[str, str],
    weights: dict[str, float] = DEFAULT_SIMILARITY_WEIGHTS,
) -> float:
    """Weighted share of the QUERY fingerprint's weighted fields that match the
    candidate exactly. A field absent from the candidate is a mismatch, never a
    free pass. Only keys present in `weights` are ever read, so a hand-written
    winner row carrying non-allowlisted keys cannot contribute to the score.
    An empty (or unweighted) query fingerprint scores 0.0 — no fingerprint is
    no evidence, not a perfect match."""
    fields = [field for field in weights if field in query_fp]
    total = sum(weights[field] for field in fields)
    if total <= 0:
        return 0.0
    matched = sum(
        weights[field] for field in fields if candidate_fp.get(field) == query_fp[field]
    )
    return matched / total


@dataclass(frozen=True)
class WarmStartMatch:
    mechanism: str
    score: float
    winner_id: str
    fingerprint_version: str


async def retrieve(
    session: AsyncSession,
    brief: OrgBrief,
    *,
    threshold: float,
    weights: dict[str, float] = DEFAULT_SIMILARITY_WEIGHTS,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
) -> tuple[WarmStartMatch | None, dict[str, Any]]:
    """Best validated mechanism from a SIMILAR pro, or None for a cold start.

    Returns (match, telemetry). Retrieval is best-effort by design: ANY failure
    degrades to a visible cold start — outcome "degraded" plus a warning — and
    never raises into the round. Every call emits one structured info line, so
    warm/cold/degraded rates are derivable from logs without a new dashboard.

    The fingerprint_version filter is not redundant with the partial index:
    briefless winners can be promoted with a NULL version and btree indexes
    NULLs, so they DO sit in ix_winners_warm_start. Their fingerprints were
    never built under this contract and must never be scored.
    """
    started = perf_counter()
    telemetry: dict[str, Any] = {
        "scanned": 0,
        "latency_ms": 0.0,
        "best_score": None,
        "outcome": "cold",
    }
    match: WarmStartMatch | None = None
    try:
        query_fp = build_fingerprint(brief)
        rows = (
            await session.execute(
                select(WinnerRow.id, WinnerRow.fingerprint, WinnerRow.warm_start_evidence)
                .where(
                    WinnerRow.warm_start_eligible.is_(True),
                    WinnerRow.fingerprint_version == FINGERPRINT_VERSION,
                )
                .order_by(WinnerRow.created_at.desc())
                .limit(scan_limit)
            )
        ).all()
        telemetry["scanned"] = len(rows)
        best: WarmStartMatch | None = None
        for row in rows:
            mechanism = str((row.warm_start_evidence or {}).get("mechanism") or "")
            if not mechanism:
                continue  # nothing to seed with; the label IS the payload
            score = similarity(query_fp, row.fingerprint or {}, weights)
            if best is None or score > best.score:
                best = WarmStartMatch(
                    mechanism=mechanism,
                    score=score,
                    winner_id=row.id,
                    fingerprint_version=FINGERPRINT_VERSION,
                )
        telemetry["best_score"] = best.score if best is not None else None
        if best is not None and best.score >= threshold:
            match, telemetry["outcome"] = best, "warm"
    except Exception as error:  # degraded cold start, never a crash
        # The caller writes the round's candidate/ledger rows through THIS
        # session: a DB-level failure here (timeout, reset, serialization)
        # leaves it needing a rollback, and without one the whole round dies at
        # commit after every paid call has already been made. That transaction
        # is doomed either way. A non-DB failure leaves the session healthy, so
        # rolling back there would needlessly discard the round's pending work.
        if isinstance(error, SQLAlchemyError):
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001 — a dead connection is still a cold start
                log.warning("warm_start rollback failed after a degraded retrieval", exc_info=True)
        match = None
        telemetry["outcome"] = "degraded"
        telemetry["error"] = str(error)
        log.warning(
            "warm_start retrieval failed; degrading to a cold start: %s", error, exc_info=True
        )
    telemetry["latency_ms"] = round((perf_counter() - started) * 1000, 3)
    log.info(
        "warm_start outcome=%s scanned=%s latency_ms=%s best_score=%s threshold=%s",
        telemetry["outcome"],
        telemetry["scanned"],
        telemetry["latency_ms"],
        telemetry["best_score"],
        threshold,
    )
    return match, telemetry
