"""Exposure registration (spec: `POST /api/exposures`).

Canonical exposure-level recommendation identity. Arms: "A" is the treated
recommendation exposure; "B" is the control/neutral arm — a B exposure never
requires a WinnerRow and is measured against the same item identity and
observation windows as its A counterpart.

Authority rules: a winner-linked exposure derives its identity (pro, org,
item) from the winner — caller-supplied identity is ignored; identity on an
existing exposure is immutable — a resubmission may only advance send state
(the authoritative send confirmation that starts the measurement clock).
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.checkpoints import LEARNING_VERSION
from waypoint.models import ExposureIn
from waypoint.outcomes import merge_routing
from waypoint.tables import ExposureRow, WinnerRow


def _identity_from(item: ExposureIn, winner: WinnerRow | None) -> dict[str, str | None]:
    if winner is not None:
        return {
            "run_id": winner.run_id,
            "winner_id": winner.id,
            "pro_id": winner.pro_id,
            "org_id": winner.evidence.get("org_id", ""),
            "item_id": winner.item_id,
            "item_version": winner.item_version,
        }
    return {
        "run_id": None,
        "winner_id": None,
        "pro_id": item.pro_id,
        "org_id": item.org_id,
        "item_id": item.item_id,
        "item_version": item.item_version,
    }


async def register(session: AsyncSession, body: list[ExposureIn]) -> dict[str, int]:
    try:
        result = await _apply(session, body)
        await session.commit()
    except IntegrityError:
        # A concurrent retry of the same batch committed an exposure_id first:
        # rebuild against the now-current rows as updates instead of 500ing
        # and losing every sibling exposure (same pattern as outcomes.ingest).
        await session.rollback()
        result = await _apply(session, body)
        await session.commit()
    return result


async def _apply(session: AsyncSession, body: list[ExposureIn]) -> dict[str, int]:
    winner_ids = {item.recommendation_id for item in body if item.recommendation_id}
    winners: dict[str, WinnerRow] = {}
    if winner_ids:
        winner_rows = (
            await session.execute(select(WinnerRow).where(WinnerRow.id.in_(winner_ids)))
        ).scalars().all()
        winners = {w.id: w for w in winner_rows}
    exposure_ids = {item.exposure_id for item in body if item.exposure_id}
    existing: dict[str, ExposureRow] = {}
    if exposure_ids:
        exposure_rows = (
            await session.execute(select(ExposureRow).where(ExposureRow.id.in_(exposure_ids)))
        ).scalars().all()
        existing = {e.id: e for e in exposure_rows}

    stored = unknown = 0
    for item in body:
        winner = winners.get(item.recommendation_id) if item.recommendation_id else None
        if item.recommendation_id and winner is None:
            unknown += 1
            continue
        row = existing.get(item.exposure_id)
        if row is None:
            row = ExposureRow(
                id=item.exposure_id,
                arm=item.arm,
                channel=item.channel,
                routing=item.routing,
                send_status=item.send_status,
                sent_at=item.sent_at,
                learning_version=LEARNING_VERSION,
                **_identity_from(item, winner),
            )
            session.add(row)
            existing[item.exposure_id] = row
        else:
            if row.winner_id is None and winner is not None:
                # A winner-less exposure has no winner identity to protect
                # yet: link it the moment a resubmission resolves the winner,
                # or its measured outcomes can never reach promotion.
                for field, value in _identity_from(item, winner).items():
                    setattr(row, field, value)
            # Identity is immutable, and send state only ADVANCES: once a send
            # is confirmed the clock is running — a later "pending"/"failed"
            # or a rewritten sent_at would silently invalidate checkpoints
            # already resolved off the old clock.
            if row.send_status != "confirmed":
                if item.send_status != "unknown":
                    row.send_status = item.send_status
                if item.sent_at is not None:
                    row.sent_at = item.sent_at
            # Routing merges like any other source claim: an empty claim
            # defers, a first claim is taken, disagreement fails closed.
            row.routing = merge_routing(row.routing, item.routing)
        stored += 1
    return {"stored": stored, "unknown_recommendation": unknown}
