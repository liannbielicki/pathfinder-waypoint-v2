"""Call-channel winners as an operator work list.

A winner whose recommendation channel is "call" is handled by Pathfinder
operators, not LCM (handoff.ready_rows skips it). This lists them across runs,
newest first, with their to-do state from call_logs.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.models import CallItem
from waypoint.tables import CallLogRow, CandidateRow, WinnerRow

CALL_STATUSES = ("todo", "done")


async def list_calls(session: AsyncSession, limit: int = 500) -> list[CallItem]:
    rows = (
        await session.execute(
            select(WinnerRow, CandidateRow, CallLogRow)
            .join(CandidateRow, CandidateRow.id == WinnerRow.candidate_id)
            .outerjoin(CallLogRow, CallLogRow.winner_id == WinnerRow.id)
            .where(
                WinnerRow.kind == "winner",
                CandidateRow.recommendation["channel"].astext == "call",
            )
            .order_by(WinnerRow.created_at.desc())
            .limit(limit)
        )
    ).all()
    items = []
    for winner, candidate, log in rows:
        rec: dict[str, Any] = candidate.recommendation
        items.append(
            CallItem(
                winner_id=winner.id,
                run_id=winner.run_id,
                pro_id=winner.pro_id,
                org_id=str(winner.evidence.get("org_id", "")),
                title=str(rec.get("title", "")),
                mechanism=str(rec.get("mechanism", "")),
                pro_facing_concept=str(rec.get("pro_facing_concept", "")),
                manager_rationale=str(rec.get("manager_rationale", "")),
                actions=[str(a) for a in rec.get("actions", [])],
                created_at=winner.created_at,
                status=log.status if log else "todo",
                note=log.note if log else "",
                updated_at=log.updated_at if log else None,
            )
        )
    return items


async def update_call(
    session: AsyncSession, winner_id: str, status: str, note: str
) -> CallLogRow | None:
    """Upsert the to-do state. Returns None when the winner is not a call."""
    exists = (
        await session.execute(
            select(WinnerRow.id)
            .join(CandidateRow, CandidateRow.id == WinnerRow.candidate_id)
            .where(
                WinnerRow.id == winner_id,
                CandidateRow.recommendation["channel"].astext == "call",
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        return None
    log = await session.get(CallLogRow, winner_id)
    if log is None:
        log = CallLogRow(winner_id=winner_id)
        session.add(log)
    log.status = status
    log.note = note
    await session.commit()
    await session.refresh(log)
    return log
