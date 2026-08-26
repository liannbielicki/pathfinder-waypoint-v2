"""Outcome ingestion (spec: `/api/outcomes`).

Observed messaging/app-usage outcomes, keyed by recommendation_id. Attributable
records backfill run_id/pro_id/journey_window/mechanism/channel/org_id from the
winner; unattributable ones are stored with an explicit evidence_limitation
label (spec: label the limitation, never pretend). A resubmission that arrives
after the winner now exists clears a stale evidence_limitation instead of
carrying it forever.

This is also the ONLY path that grants warm-start eligibility: an attributable
winner whose merged record carries a measured 7-day return is promoted here
(see warmstart.py). Nothing on the scoring/persona side may set it.

Batched: winners/runs/candidates/existing rows are prefetched with IN()
queries keyed on the batch's distinct ids, not one SELECT per item. A
concurrent duplicate `(recommendation_id, source)` submission from a sibling
request can still race past the prefetch and collide at commit — that raises
IntegrityError on uq_touch_outcomes_rec_source, so the whole-batch commit is
retried once as updates against the now-current rows (same catch-rollback
pattern as PersonaEvalRow in pipeline.py) rather than 500ing and losing every
sibling item in the batch.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.models import TouchOutcomeIn
from waypoint.tables import CandidateRow, RunRow, TouchOutcomeRow, WinnerRow

_OUTCOME_FLAGS = (
    "delivered", "clicked", "replied", "unsubscribed",
    "returned_7d", "returned_14d", "returned_30d", "returned_90d",
)


def derive_checkpoint_flags(
    *, sent_at: datetime | None, first_return_at: datetime | None,
    returned_7d: bool | None = None,
) -> dict[str, bool | None]:
    """Derive V3 return horizons from the first qualifying return event.

    A missing event is unresolved here. The checkpoint worker may later turn
    that state into a measured false only after it proves the source window is
    complete. ``returned_7d`` is accepted only to reject an unsupported
    caller-supplied positive; legacy ingestion remains handled separately.
    """
    if returned_7d is True and first_return_at is None:
        raise ValueError("returned_7d requires first_return_at")
    if sent_at is None or first_return_at is None:
        return {"returned_1d": None, "returned_7d": None, "returned_30d": None}
    elapsed = first_return_at - sent_at
    return {
        "returned_1d": elapsed.total_seconds() <= 24 * 60 * 60,
        "returned_7d": elapsed.total_seconds() <= 7 * 24 * 60 * 60,
        "returned_30d": elapsed.total_seconds() <= 30 * 24 * 60 * 60,
    }


async def _prefetch_winners(
    session: AsyncSession, body: list[TouchOutcomeIn]
) -> tuple[dict[str, WinnerRow], dict[str, RunRow], dict[str, CandidateRow]]:
    winner_ids = {item.recommendation_id for item in body}
    winners = (
        (await session.execute(select(WinnerRow).where(WinnerRow.id.in_(winner_ids))))
        .scalars().all()
    )
    winners_by_id = {w.id: w for w in winners}
    run_ids = {w.run_id for w in winners}
    candidate_ids = {w.candidate_id for w in winners if w.candidate_id}
    runs_by_id: dict[str, RunRow] = {}
    if run_ids:
        runs = (await session.execute(select(RunRow).where(RunRow.id.in_(run_ids)))).scalars().all()
        runs_by_id = {r.id: r for r in runs}
    candidates_by_id: dict[str, CandidateRow] = {}
    if candidate_ids:
        candidates = (
            await session.execute(select(CandidateRow).where(CandidateRow.id.in_(candidate_ids)))
        ).scalars().all()
        candidates_by_id = {c.id: c for c in candidates}
    return winners_by_id, runs_by_id, candidates_by_id


async def _existing_by_key(
    session: AsyncSession, rec_ids: set[str]
) -> dict[tuple[str, str], TouchOutcomeRow]:
    if not rec_ids:
        return {}
    rows = (
        await session.execute(
            select(TouchOutcomeRow).where(TouchOutcomeRow.recommendation_id.in_(rec_ids))
        )
    ).scalars().all()
    return {(r.recommendation_id, r.source): r for r in rows}


def _attribution_fill(
    item: TouchOutcomeIn,
    winner: WinnerRow | None,
    run: RunRow | None,
    candidate: CandidateRow | None,
) -> tuple[dict[str, Any], str | None]:
    if winner is None:
        return {}, "unattributed: recommendation_id matches no winner"
    recommendation = candidate.recommendation if candidate else {}
    fill = {
        "run_id": winner.run_id,
        "pro_id": item.pro_id or winner.pro_id,
        "journey_window": run.journey_window if run else "churn_risk",
        "mechanism": recommendation.get("mechanism", "") if candidate else "",
        # item-supplied non-empty values win; backfill only fills blanks.
        "channel": item.channel or recommendation.get("channel", ""),
        "org_id": item.org_id or winner.evidence.get("org_id", ""),
        "item_id": item.item_id or winner.item_id,
        "item_version": item.item_version or winner.item_version,
        "arm": item.arm,
    }
    return fill, None


def _apply_flags(row: TouchOutcomeRow, item: TouchOutcomeIn) -> None:
    # Later horizons arrive later; non-None fields win, None never erases a
    # measured value.
    for key in _OUTCOME_FLAGS:
        value = getattr(item, key)
        if value is not None:
            setattr(row, key, value)
    if item.sent_at is not None:
        row.sent_at = item.sent_at
    if item.first_return_at is not None and (
        row.first_return_at is None or item.first_return_at < row.first_return_at
    ):
        row.first_return_at = item.first_return_at
    if item.item_id is not None:
        row.item_id = item.item_id
    if item.item_version is not None:
        row.item_version = item.item_version
    if item.arm is not None:
        row.arm = item.arm
    if row.sent_at is not None and row.first_return_at is not None:
        derived = derive_checkpoint_flags(
            sent_at=row.sent_at, first_return_at=row.first_return_at
        )
        for key, value in derived.items():
            setattr(row, key, value)


def _promote_warm_start(
    rows: list[TouchOutcomeRow], winner: WinnerRow | None, candidate: CandidateRow | None
) -> None:
    """The ONLY path that grants warm-start eligibility: a real observed 7-day
    return on an attributable winner. Derived from EVERY source's row for this
    winner (any observed return wins, ties broken by source name), so
    duplicates, late arrivals, and a lagging second source converge on the same
    values whatever order the batch applies them in."""
    observed = [row for row in rows if row.returned_7d is not None]
    if winner is None or winner.kind != "winner" or not observed:
        return
    explicit_arms = [row for row in observed if row.arm in {"A", "B"}]
    if explicit_arms:
        # A-only results are directional evidence, not global promotion proof.
        if not any(row.arm == "B" for row in explicit_arms):
            return
        positive = [row for row in explicit_arms if row.arm == "A" and row.returned_7d]
    else:
        # Preserve legacy records while they migrate to the V3 arm contract.
        positive = [row for row in observed if row.returned_7d]
    row = min(positive or observed, key=lambda r: r.source)
    recommendation = candidate.recommendation if candidate else {}
    winner.warm_start_eligible = bool(positive)
    winner.validation_status = "validated" if positive else "validated_negative"
    # Mechanism/channel ride along so retrieval never joins org-scoped rows.
    winner.warm_start_evidence = {
        "returned_7d": row.returned_7d,
        "source": row.source,
        "mechanism": recommendation.get("mechanism", ""),
        "channel": row.channel or "",
    }


def _apply_item(
    session: AsyncSession,
    item: TouchOutcomeIn,
    winner: WinnerRow | None,
    run: RunRow | None,
    candidate: CandidateRow | None,
    existing_by_key: dict[tuple[str, str], TouchOutcomeRow],
) -> bool:
    """Adds or updates one outcome row; returns True when it lacks attribution."""
    fill, limitation = _attribution_fill(item, winner, run, candidate)
    key = (item.recommendation_id, item.source)
    existing = existing_by_key.get(key)
    if existing is None:
        fields = {
            "recommendation_id": item.recommendation_id,
            "source": item.source,
            "org_id": item.org_id,
            "channel": item.channel,
            "sent_at": item.sent_at,
            "first_return_at": item.first_return_at,
            "evidence_limitation": limitation,
            "pro_id": item.pro_id,
            **{k: getattr(item, k) for k in _OUTCOME_FLAGS},
            **fill,
        }
        if item.first_return_at is not None:
            fields.update(derive_checkpoint_flags(
                sent_at=item.sent_at, first_return_at=item.first_return_at
            ))
        row = TouchOutcomeRow(**fields)
        session.add(row)
        existing_by_key[key] = row
    else:
        # A row stored unattributed must not keep evidence_limitation forever
        # once the winner exists — re-attribute on resubmission.
        if existing.evidence_limitation is not None and limitation is None:
            for field_name, value in fill.items():
                setattr(existing, field_name, value)
            existing.evidence_limitation = None
        _apply_flags(existing, item)
    # Every source's row for this recommendation, not just the one applied now:
    # eligibility must not depend on which source lands last in the batch.
    _promote_warm_start(
        [row for (rec_id, _), row in existing_by_key.items() if rec_id == item.recommendation_id],
        winner,
        candidate,
    )
    return limitation is not None


async def _apply_batch(
    session: AsyncSession,
    body: list[TouchOutcomeIn],
    winners: dict[str, WinnerRow],
    runs: dict[str, RunRow],
    candidates: dict[str, CandidateRow],
) -> int:
    rec_ids = {item.recommendation_id for item in body}
    existing_by_key = await _existing_by_key(session, rec_ids)
    unattributed = 0
    for item in body:
        winner = winners.get(item.recommendation_id)
        run = runs.get(winner.run_id) if winner else None
        candidate = (
            candidates.get(winner.candidate_id) if winner and winner.candidate_id else None
        )
        if _apply_item(session, item, winner, run, candidate, existing_by_key):
            unattributed += 1
    return unattributed


async def ingest(session: AsyncSession, body: list[TouchOutcomeIn]) -> dict[str, int]:
    winners, runs, candidates = await _prefetch_winners(session, body)
    unattributed = await _apply_batch(session, body, winners, runs, candidates)
    try:
        await session.commit()
    except IntegrityError:
        # A sibling request committed the same (recommendation_id, source)
        # first: rebuild the batch against the now-current rows as updates
        # instead of 500ing and losing every sibling item in the batch.
        await session.rollback()
        winners, runs, candidates = await _prefetch_winners(session, body)
        unattributed = await _apply_batch(session, body, winners, runs, candidates)
        await session.commit()
    return {"stored": len(body), "unattributed": unattributed}
