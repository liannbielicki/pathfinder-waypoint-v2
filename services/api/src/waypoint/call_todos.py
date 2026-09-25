"""Call-channel winners as an operator work list.

A winner whose recommendation channel is "call" is handled by Pathfinder
operators, not LCM (handoff.ready_rows skips it). This lists them across runs,
newest first, with their to-do state from call_logs.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.models import CallAlternative, CallItem
from waypoint.tables import CallLogRow, CandidateRow, WinnerRow

CALL_STATUSES = ("todo", "done")


def _contact_plan_flags(plan: object) -> list[str]:
    # Mirror handoff.py's isinstance guard: contact_plan is untrusted JSON
    # (evidence column), so a malformed value (e.g. a string) must degrade to
    # no flags rather than crash list_calls / 500 the whole /api/calls panel.
    if not isinstance(plan, dict):
        return []
    flags = plan.get("flags")
    if not isinstance(flags, list):
        return []
    return [str(f) for f in flags]


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
                pro_uuid=(str(winner.evidence["pro_uuid"])
                          if winner.evidence.get("pro_uuid") else None),
                flags=_contact_plan_flags(winner.evidence.get("contact_plan")),
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
    if items:
        alts = await _alternatives(session, items)
        for item in items:
            item.alternatives = alts.get((item.run_id, item.pro_id), [])
    return items


def _score_pp(candidate: CandidateRow) -> float | None:
    score = candidate.score or {}
    for stage in ("final", "screen"):
        value = (score.get(stage) or {}).get("reduction_pp")
        if value is not None:
            return float(value)
    return None


async def _alternatives(
    session: AsyncSession, items: list[CallItem], top: int = 2
) -> dict[tuple[str, str], list[CallAlternative]]:
    """The next-best panel-scored ideas per (run, pro), winner and critic-
    suppressed ideas excluded. ponytail: one query over every listed run;
    narrow by pro if the calls list ever grows past a few hundred rows."""
    winner_candidates = {(i.run_id, i.pro_id) for i in items}
    rows = (
        await session.execute(
            select(CandidateRow).where(
                CandidateRow.run_id.in_({r for r, _ in winner_candidates}),
                CandidateRow.status.in_(("discarded", "champion")),
            )
        )
    ).scalars()
    grouped: dict[tuple[str, str], list[CandidateRow]] = {}
    for c in rows:
        key = (c.run_id, c.pro_id)
        if key in winner_candidates and _score_pp(c) is not None:
            grouped.setdefault(key, []).append(c)
    out: dict[tuple[str, str], list[CallAlternative]] = {}
    for key, cands in grouped.items():
        ranked = sorted(cands, key=lambda c: _score_pp(c) or 0.0, reverse=True)
        # The winner is the top-scored champion; everything after it is a runner-up.
        out[key] = [
            CallAlternative(
                title=str(c.recommendation.get("title", "")),
                mechanism=str(c.recommendation.get("mechanism", "")),
                channel=str(c.recommendation.get("channel", "")),
                pro_facing_concept=str(c.recommendation.get("pro_facing_concept", "")),
                actions=[str(a) for a in c.recommendation.get("actions", [])],
                score_pp=_score_pp(c),
            )
            for c in ranked[1 : top + 1]
        ]
    return out


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
