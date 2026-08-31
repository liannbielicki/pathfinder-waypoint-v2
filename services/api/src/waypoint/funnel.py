"""Full-funnel counts: audience -> Waypoint verdict -> LCM intake -> sent -> returned.

Every stage is already recorded in Waypoint's own tables, so nothing here reads
Slack, Allison's Supabase, or Iterable:

    audience      runs.pro_ids                     the Pros handed to a run
    verdict       winners.kind                     winner | no_action | abstained
    handed_off    handoffs (one per shipped winner)
    intake        handoffs.status                  accepted | rejected | duplicate
    sent          touch_outcomes                   an Iterable send event was observed
    returned      touch_outcomes.returned_*        measured at each horizon

The gap between `handed_off` and `sent` IS the human QA drop inside the LCM app
(the "50 in, 45 out"): a row that never earned a send event was never delivered.
It is inferred rather than read from her database on purpose — the absence of a
send event is the honest signal, and it needs no second integration.

`detail=True` additionally drives the n8n outcome flow: it returns the
(run_id, pro_id) pairs that were shipped, so the flow can start from what we are
waiting on instead of pulling every message event in the project and discarding
what is not ours.

ponytail: aggregates in SQL, one query per stage over the whole window — not one
query per run. Ceiling: the detail rows are unpaginated; add a cursor if a
window ever returns more than a few thousand Pros.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.tables import (
    CandidateRow,
    HandoffRow,
    RunRow,
    TouchOutcomeRow,
    WinnerRow,
)

VERDICTS = ("winner", "no_action", "abstained")
INTAKE_STATUSES = ("accepted", "rejected", "duplicate", "pending")


@dataclass
class RunFunnel:
    run_id: str
    created_at: datetime
    journey_window: str
    status: str
    audience: int
    verdicts: dict[str, int] = field(default_factory=dict)
    handed_off: int = 0
    intake: dict[str, int] = field(default_factory=dict)
    sent: int = 0
    returned: dict[str, dict[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat(),
            "journey_window": self.journey_window,
            "status": self.status,
            "audience": self.audience,
            # EVERY observed kind, not just the three we expect. WinnerRow.kind is
            # free text with no check constraint, so emitting a fixed set meant a
            # novel kind was subtracted from `undecided` and shown under no key —
            # a Pro deducted from the arithmetic and displayed nowhere.
            **{kind: 0 for kind in VERDICTS},
            **self.verdicts,
            # Pros with no winner row yet (still running, or the run died) are
            # neither a verdict nor a silent drop — name them. Clamped: a winner
            # for a Pro outside runs.pro_ids would otherwise drive this negative.
            "undecided": max(self.audience - sum(self.verdicts.values()), 0),
            "handed_off": self.handed_off,
            **{f"intake_{s}": self.intake.get(s, 0) for s in INTAKE_STATUSES},
            "sent": self.sent,
            # ACCEPTED minus sent, not handed_off minus sent. `handed_off` counts
            # every status including rejected/duplicate/pending — and a pending row
            # is written BEFORE the POST to LCM, so a handoff that died on the wire
            # and a row LCM rejected at intake would both have been reported as
            # "a human dropped it in QA". Only an accepted row was ever QA'd.
            "qa_dropped": max(self.intake.get("accepted", 0) - self.sent, 0),
            "returned": self.returned,
        }


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


async def _runs(session: AsyncSession, days: int) -> dict[str, RunFunnel]:
    rows = (
        await session.execute(
            select(RunRow).where(RunRow.created_at >= _since(days)).order_by(
                RunRow.created_at.desc()
            )
        )
    ).scalars().all()
    return {
        run.id: RunFunnel(
            run_id=run.id,
            created_at=run.created_at,
            journey_window=run.journey_window,
            status=run.status,
            audience=len(set(run.pro_ids or [])),
        )
        for run in rows
    }


async def _add_verdicts(session: AsyncSession, funnels: dict[str, RunFunnel]) -> None:
    rows = (
        await session.execute(
            select(WinnerRow.run_id, WinnerRow.kind, func.count())
            .where(WinnerRow.run_id.in_(funnels))
            .group_by(WinnerRow.run_id, WinnerRow.kind)
        )
    ).all()
    for run_id, kind, count in rows:
        funnels[run_id].verdicts[kind] = count


async def _add_intake(session: AsyncSession, funnels: dict[str, RunFunnel]) -> None:
    rows = (
        await session.execute(
            select(HandoffRow.run_id, HandoffRow.status, func.count())
            .where(HandoffRow.run_id.in_(funnels))
            .group_by(HandoffRow.run_id, HandoffRow.status)
        )
    ).all()
    for run_id, status, count in rows:
        funnels[run_id].intake[status] = count
        funnels[run_id].handed_off += count


async def _add_outcomes(session: AsyncSession, funnels: dict[str, RunFunnel]) -> None:
    """Sent + per-horizon returns. Only attributable rows count as measured —
    a guardrailed or unresolvable record is real audit data but is not evidence
    that a Pro received anything (see outcomes.evidence_limitation)."""
    horizons = ("7d", "14d", "30d", "90d")
    columns = [
        # Distinct PROS, not rows. `IN (...)` already excludes a NULL run_id.
        func.count(distinct(TouchOutcomeRow.pro_id)),
    ]
    for horizon in horizons:
        column = getattr(TouchOutcomeRow, f"returned_{horizon}")
        columns.append(
            func.count(distinct(case((column.isnot(None), TouchOutcomeRow.pro_id))))
        )
        columns.append(
            func.count(distinct(case((column.is_(True), TouchOutcomeRow.pro_id))))
        )
    rows = (
        await session.execute(
            select(TouchOutcomeRow.run_id, *columns)
            .where(
                TouchOutcomeRow.run_id.in_(funnels),
                TouchOutcomeRow.evidence_limitation.is_(None),
                # `sent` claims a send was observed, so require the evidence of
                # one. An unsubscribe-only or manual row is real data but is not
                # proof the message went out.
                TouchOutcomeRow.sent_at.isnot(None),
            )
            .group_by(TouchOutcomeRow.run_id)
        )
    ).all()
    for row in rows:
        funnel = funnels[row[0]]
        funnel.sent = row[1]
        for index, horizon in enumerate(horizons):
            measured, returned = row[2 + index * 2], row[3 + index * 2]
            if measured:
                funnel.returned[horizon] = {"measured": measured, "returned": returned}


async def summary(session: AsyncSession, days: int = 7) -> dict[str, Any]:
    funnels = await _runs(session, days)
    if funnels:
        await _add_verdicts(session, funnels)
        await _add_intake(session, funnels)
        await _add_outcomes(session, funnels)
    runs = [f.as_dict() for f in funnels.values()]
    # Seeded so a quiet window returns zeros rather than a KeyError at the caller.
    totals: dict[str, Any] = {
        key: 0 for key in
        ("audience", "undecided", *VERDICTS, "handed_off", "sent", "qa_dropped",
         *(f"intake_{s}" for s in INTAKE_STATUSES))
    }
    # `returned` is a dict, so the isinstance(int) sweep silently dropped it —
    # a caller reading only totals saw the whole funnel except the outcome it
    # exists to measure.
    returned: dict[str, dict[str, int]] = {}
    for run in runs:
        for key, value in run.items():
            if isinstance(value, int):
                totals[key] = totals.get(key, 0) + value
        for horizon, bucket in run["returned"].items():
            into = returned.setdefault(horizon, {"measured": 0, "returned": 0})
            into["measured"] += bucket["measured"]
            into["returned"] += bucket["returned"]
    totals["returned"] = returned
    return {"days": days, "runs": runs, "totals": totals}


async def worklist(session: AsyncSession, days: int = 7) -> list[dict[str, Any]]:
    """The MACHINE work list: the shipped touches, and nothing else.

    Deliberately not `detail()`. The n8n flow filters on
    `verdict == "winner" and handed_off` and then keeps only the natural key, so
    that is all it is given — n8n persists node output in plaintext execution
    history, and shipping themes, mechanisms and org_ids into that store nightly
    would hand a leaked automation token the whole recommendation catalogue.
    """
    return [
        {"run_id": row["run_id"], "pro_id": row["pro_id"]}
        for row in await detail(session, days)
        if row["verdict"] == "winner" and row["handed_off"]
    ]


async def detail(session: AsyncSession, days: int = 7) -> list[dict[str, Any]]:
    """One row per Pro that reached a verdict, with its theme and where it got to.

    Doubles as the n8n flow's work list: `run_id` + `pro_id` is the natural key
    that resolves to exactly one winner (uq_winners_run_pro).
    """
    funnels = await _runs(session, days)
    if not funnels:
        return []
    rows = (
        await session.execute(
            select(WinnerRow, CandidateRow, HandoffRow)
            .outerjoin(CandidateRow, WinnerRow.candidate_id == CandidateRow.id)
            .outerjoin(HandoffRow, HandoffRow.winner_id == WinnerRow.id)
            .where(WinnerRow.run_id.in_(funnels))
            .order_by(WinnerRow.created_at.desc())
        )
    ).all()
    out = []
    for winner, candidate, handoff in rows:
        recommendation = candidate.recommendation if candidate else {}
        out.append({
            "run_id": winner.run_id,
            "pro_id": winner.pro_id,
            "org_id": winner.evidence.get("org_id", ""),
            "verdict": winner.kind,
            "theme": recommendation.get("pro_facing_concept", ""),
            "mechanism": recommendation.get("mechanism", ""),
            "handed_off": handoff is not None,
            "intake_status": handoff.status if handoff else None,
            "warm_start_eligible": winner.warm_start_eligible,
            "validation_status": winner.validation_status,
        })
    return out
