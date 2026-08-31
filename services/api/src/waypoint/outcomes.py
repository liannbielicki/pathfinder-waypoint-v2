"""Outcome ingestion (spec: `/api/outcomes`).

Observed messaging/app-usage outcomes, keyed by recommendation_id (a Waypoint
winner id or an exposure id). Attributable records backfill
run/pro/journey_window/mechanism/channel/org and the canonical item identity
from the winner or exposure; unattributable ones are stored with an explicit
evidence_limitation label (spec: label the limitation, never pretend). A
resubmission that arrives after the winner now exists clears a stale
evidence_limitation instead of carrying it forever.

V3 authority rules enforced here:
- Attribution identity (pro, org, item, arm) comes ONLY from winner/exposure
  records; TouchOutcomeIn carries no identity or horizon fields to override
  them with. Where an exposure exists it is the identity authority (it is the
  exposure-level unit), the winner supplying only mechanism context.
- Return horizons (1d/7d/30d) are DERIVED from first_return_at against a
  confirmed send, or resolved later by the checkpoint sweep. Callers never
  assert them. Intake/QA acknowledgement is not delivery; clocks start at
  send_status == "confirmed".

This is also the ONLY path (with checkpoints.py, which reuses it) that grants
warm-start eligibility: an attributable winner with a measured 7-day return —
A+B (causal) when arm-tagged rows exist, legacy otherwise. A-only positives
are directional: recorded, never eligible. Nothing on the scoring/persona side
may set eligibility.

Batched: winners/exposures/runs/candidates/existing rows are prefetched with
IN() queries keyed on the batch's distinct ids, not one SELECT per item. A
concurrent duplicate `(recommendation_id, source)` submission from a sibling
request can still race past the prefetch and collide at flush/commit — that
raises IntegrityError on uq_touch_outcomes_rec_source, so the whole batch is
retried once as updates against the now-current rows rather than 500ing and
losing every sibling item.
"""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.models import TouchOutcomeIn
from waypoint.tables import CandidateRow, ExposureRow, RunRow, TouchOutcomeRow, WinnerRow

_OUTCOME_FLAGS = ("delivered", "clicked", "replied", "unsubscribed")


def derive_checkpoint_flags(
    *, sent_at: datetime | None, first_return_at: datetime | None
) -> dict[str, bool | None]:
    """Derive V3 return horizons from the first qualifying return event.

    A missing event is unresolved here, and so is a "return" that PREDATES the
    send (clock skew, or a pro active before the touch) — it is not a
    qualifying return event and must never derive a positive. The checkpoint
    sweep may later turn unresolved state into a measured false only after it
    proves the source window is complete (checkpoints.py).
    """
    if sent_at is None or first_return_at is None or first_return_at < sent_at:
        return {"returned_1d": None, "returned_7d": None, "returned_30d": None}
    elapsed = first_return_at - sent_at
    return {
        "returned_1d": elapsed.total_seconds() <= 24 * 60 * 60,
        "returned_7d": elapsed.total_seconds() <= 7 * 24 * 60 * 60,
        "returned_30d": elapsed.total_seconds() <= 30 * 24 * 60 * 60,
    }


class _Prefetched:
    def __init__(
        self,
        winners: dict[str, WinnerRow],
        exposures: dict[str, ExposureRow],
        runs: dict[str, RunRow],
        candidates: dict[str, CandidateRow],
    ) -> None:
        self.winners = winners
        self.exposures = exposures
        self.runs = runs
        self.candidates = candidates


async def _prefetch(session: AsyncSession, body: list[TouchOutcomeIn]) -> _Prefetched:
    winner_ids = {item.recommendation_id for item in body}
    winners = (
        (await session.execute(select(WinnerRow).where(WinnerRow.id.in_(winner_ids))))
        .scalars().all()
    )
    winners_by_id = {w.id: w for w in winners}
    # Exposures are always consulted: an explicit exposure_id, or the
    # recommendation_id itself when it names an exposure rather than a winner.
    exposure_ids = {item.exposure_id for item in body if item.exposure_id}
    exposure_ids.update(
        item.recommendation_id for item in body if item.recommendation_id not in winners_by_id
    )
    exposures = (
        (await session.execute(select(ExposureRow).where(ExposureRow.id.in_(exposure_ids))))
        .scalars().all()
        if exposure_ids else []
    )
    exposures_by_id = {e.id: e for e in exposures}
    # Winners reachable only through an exposure link still need loading, so
    # promotion and mechanism context work for control exposures too.
    linked_winner_ids = {
        e.winner_id for e in exposures if e.winner_id and e.winner_id not in winners_by_id
    }
    if linked_winner_ids:
        linked = (
            await session.execute(select(WinnerRow).where(WinnerRow.id.in_(linked_winner_ids)))
        ).scalars().all()
        winners_by_id.update({w.id: w for w in linked})
    run_ids = {w.run_id for w in winners_by_id.values()}
    run_ids.update(e.run_id for e in exposures if e.run_id)
    candidate_ids = {w.candidate_id for w in winners_by_id.values() if w.candidate_id}
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
    return _Prefetched(winners_by_id, exposures_by_id, runs_by_id, candidates_by_id)


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
    exposure: ExposureRow | None,
    run: RunRow | None,
    candidate: CandidateRow | None,
) -> tuple[dict[str, Any], str | None]:
    if winner is None and exposure is None:
        return {}, "unattributed: recommendation_id matches no winner or exposure"
    recommendation = candidate.recommendation if candidate else {}
    if exposure is not None:
        # The exposure IS the identity authority — arm, send state, and item
        # identity come from it, never from the caller or even the winner.
        return {
            "exposure_id": exposure.id,
            "run_id": exposure.run_id,
            # The run's window, so a control exposure never pollutes another
            # window's evidence corpus with the column default.
            **({"journey_window": run.journey_window} if run else {}),
            "pro_id": exposure.pro_id,
            "org_id": exposure.org_id,
            "item_id": exposure.item_id,
            "item_version": exposure.item_version,
            "arm": exposure.arm,
            "channel": exposure.channel,
            "sent_at": exposure.sent_at,
            "send_status": exposure.send_status,
            "mechanism": recommendation.get("mechanism", "") if candidate else "",
        }, None
    assert winner is not None  # no exposure and the first guard passed
    fill = {
        "run_id": winner.run_id,
        # Winner/exposure records are the authority. Caller identity is
        # observational input and must never rewrite attribution.
        "pro_id": winner.pro_id,
        "journey_window": run.journey_window if run else "churn_risk",
        "mechanism": recommendation.get("mechanism", "") if candidate else "",
        # item-supplied non-empty channel wins; backfill only fills blanks.
        "channel": item.channel or recommendation.get("channel", ""),
        "org_id": winner.evidence.get("org_id", ""),
        "item_id": winner.item_id,
        "item_version": winner.item_version,
    }
    return fill, None


def _apply_flags(row: TouchOutcomeRow, item: TouchOutcomeIn) -> None:
    # Non-None fields win, None never erases a measured value.
    for key in _OUTCOME_FLAGS:
        value = getattr(item, key)
        if value is not None:
            setattr(row, key, value)
    # Send state only ADVANCES (same rule as exposures.register): once a send
    # is confirmed the clock is running, and a resubmission that moved sent_at
    # or regressed send_status would silently invalidate horizons already
    # resolved off the old clock.
    if row.send_status != "confirmed":
        if item.sent_at is not None:
            row.sent_at = item.sent_at
        if item.send_status != "unknown":
            row.send_status = item.send_status
    if item.send_confirmed_at is not None and row.send_confirmed_at is None:
        row.send_confirmed_at = item.send_confirmed_at
    if item.first_return_at is not None and (
        row.first_return_at is None or item.first_return_at < row.first_return_at
    ):
        row.first_return_at = item.first_return_at
    # Intake/QA acknowledgement is not delivery. Checkpoint clocks start only
    # once the source has confirmed the message was sent. Derivation only
    # FILLS unresolved horizons — a measured value (including a sweep-resolved
    # negative stamped with checkpoint_version) is never rewritten.
    if row.send_status == "confirmed" and row.sent_at is not None and row.first_return_at is not None:
        derived = derive_checkpoint_flags(
            sent_at=row.sent_at, first_return_at=row.first_return_at
        )
        for key, value in derived.items():
            if getattr(row, key) is None and value is not None:
                setattr(row, key, value)


def _promote_warm_start(
    rows: list[TouchOutcomeRow], winner: WinnerRow | None, candidate: CandidateRow | None
) -> None:
    """The ONLY path that grants warm-start eligibility: a real observed 7-day
    return on an attributable winner. Derived from EVERY row attributable to
    this winner — direct outcome rows plus its exposures' rows (any observed
    return wins, ties broken by source name), so duplicates, late arrivals,
    and a lagging second source converge on the same values whatever order
    they land in."""
    observed = [row for row in rows if row.returned_7d is not None]
    if winner is None or winner.kind != "winner" or not observed:
        return
    recommendation = candidate.recommendation if candidate else {}

    def evidence_from(row: TouchOutcomeRow, kind: str | None) -> dict[str, Any]:
        # Mechanism/channel ride along so retrieval never joins org-scoped rows.
        return {
            "returned_7d": row.returned_7d,
            "source": row.source,
            "mechanism": recommendation.get("mechanism", ""),
            "channel": row.channel or "",
            **({"evidence_kind": kind} if kind else {}),
        }

    explicit_arms = [row for row in observed if row.arm in {"A", "B"}]
    evidence_kind = None
    if explicit_arms:
        positive = [row for row in explicit_arms if row.arm == "A" and row.returned_7d]
        if not any(row.arm == "B" for row in explicit_arms):
            # A-only results are directional evidence, not global promotion
            # proof: recorded, and eligibility is REVOKED — an arm-tagged
            # winner may only seed warm starts on causal proof.
            winner.warm_start_eligible = False
            if positive:
                winner.validation_status = "directional"
                winner.warm_start_evidence = evidence_from(
                    min(positive, key=lambda r: r.source), "directional"
                )
            return
        evidence_kind = "causal"  # A observed against a B/control comparison
    else:
        # Preserve legacy records while they migrate to the V3 arm contract.
        positive = [row for row in observed if row.returned_7d]
    row = min(positive or observed, key=lambda r: r.source)
    winner.warm_start_eligible = bool(positive)
    winner.validation_status = "validated" if positive else "validated_negative"
    winner.warm_start_evidence = evidence_from(row, evidence_kind)


async def promote_winners(
    session: AsyncSession,
    winners: Iterable[WinnerRow],
    candidates: dict[str, CandidateRow],
) -> None:
    """Re-derive warm-start state for each winner from EVERY attributable row:
    outcomes keyed by the winner id plus outcomes of its linked exposures —
    this is how a silent control (arm B) exposure reaches the causal
    comparison. Shared by ingest and the checkpoint sweep."""
    for winner in winners:
        linked_exposures = (
            select(ExposureRow.id).where(ExposureRow.winner_id == winner.id).scalar_subquery()
        )
        rows = (
            await session.execute(
                select(TouchOutcomeRow).where(
                    (TouchOutcomeRow.recommendation_id == winner.id)
                    | (TouchOutcomeRow.exposure_id.in_(linked_exposures))
                )
            )
        ).scalars().all()
        _promote_warm_start(
            list(rows),
            winner,
            candidates.get(winner.candidate_id) if winner.candidate_id else None,
        )


def _apply_item(
    session: AsyncSession,
    item: TouchOutcomeIn,
    winner: WinnerRow | None,
    exposure: ExposureRow | None,
    run: RunRow | None,
    candidate: CandidateRow | None,
    existing_by_key: dict[tuple[str, str], TouchOutcomeRow],
) -> bool:
    """Adds or updates one outcome row; returns True when it lacks attribution."""
    fill, limitation = _attribution_fill(item, winner, exposure, run, candidate)
    key = (item.recommendation_id, item.source)
    existing = existing_by_key.get(key)
    if existing is None:
        fields = {
            "recommendation_id": item.recommendation_id,
            "source": item.source,
            "org_id": "",
            "channel": item.channel,
            "sent_at": item.sent_at,
            "first_return_at": item.first_return_at,
            "evidence_limitation": limitation,
            "pro_id": "",
            "exposure_id": item.exposure_id,
            "send_status": item.send_status,
            "send_confirmed_at": item.send_confirmed_at,
            **{k: getattr(item, k) for k in _OUTCOME_FLAGS},
            **fill,
        }
        row = TouchOutcomeRow(**fields)
        if row.send_status == "confirmed" and row.first_return_at is not None:
            for flag, value in derive_checkpoint_flags(
                sent_at=row.sent_at, first_return_at=row.first_return_at
            ).items():
                setattr(row, flag, value)
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
    return limitation is not None


def _winner_for_item(item: TouchOutcomeIn, prefetched: _Prefetched) -> WinnerRow | None:
    winner = prefetched.winners.get(item.recommendation_id)
    if winner is not None:
        return winner
    exposure = prefetched.exposures.get(item.exposure_id or item.recommendation_id)
    if exposure is not None and exposure.winner_id:
        return prefetched.winners.get(exposure.winner_id)
    return None


async def _apply_batch(
    session: AsyncSession, body: list[TouchOutcomeIn], prefetched: _Prefetched
) -> int:
    rec_ids = {item.recommendation_id for item in body}
    existing_by_key = await _existing_by_key(session, rec_ids)
    unattributed = 0
    for item in body:
        exposure = prefetched.exposures.get(item.exposure_id or item.recommendation_id)
        winner = _winner_for_item(item, prefetched)
        if exposure is not None and exposure.run_id:
            run = prefetched.runs.get(exposure.run_id)
        elif winner is not None:
            run = prefetched.runs.get(winner.run_id)
        else:
            run = None
        candidate = (
            prefetched.candidates.get(winner.candidate_id)
            if winner and winner.candidate_id
            else None
        )
        if _apply_item(
            session,
            item,
            winner=winner,
            exposure=exposure,
            run=run,
            candidate=candidate,
            existing_by_key=existing_by_key,
        ):
            unattributed += 1
    # Eligibility must not depend on which source/exposure lands last in the
    # batch — promotion re-derives from every attributable row per winner.
    affected = {w.id: w for w in (_winner_for_item(item, prefetched) for item in body) if w}
    await promote_winners(session, affected.values(), prefetched.candidates)
    return unattributed


async def ingest(session: AsyncSession, body: list[TouchOutcomeIn]) -> dict[str, int]:
    try:
        prefetched = await _prefetch(session, body)
        unattributed = await _apply_batch(session, body, prefetched)
        await session.commit()
    except IntegrityError:
        # A sibling request committed the same (recommendation_id, source)
        # first: rebuild the batch against the now-current rows as updates
        # instead of 500ing and losing every sibling item in the batch.
        await session.rollback()
        prefetched = await _prefetch(session, body)
        unattributed = await _apply_batch(session, body, prefetched)
        await session.commit()
    return {"stored": len(body), "unattributed": unattributed}
