"""Historical outcome evidence (spec stage 2).

Aggregates observed touch outcomes into per-(channel, mechanism) patterns for
one journey window (plus any window sharing its objective, see
evidence_windows), and lists mechanisms that recently failed for a specific
pro. Only attributable rows (evidence_limitation IS NULL) count as evidence —
unattributed records exist for audit but must never masquerade as proof.

ponytail: rows are aggregated in Python over a bounded recent slice; move to
SQL GROUP BY if touch_outcomes outgrows the LIMIT.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.tables import TouchOutcomeRow

_HORIZONS = ("7d", "14d", "30d", "90d")

# Journey windows that are ONE evidence corpus: they optimize for the same
# thing, so a touch that worked in one is real evidence for the other. Reads are
# symmetric — every window in a group sees every row in that group — otherwise
# an ungated variant would run blind while its gated twin held all the history.
_EVIDENCE_GROUPS = (frozenset({"churn_risk", "churn_risk_open"}),)


def evidence_windows(journey_window: str) -> list[str]:
    """The window values whose outcomes count as evidence for this window."""
    for group in _EVIDENCE_GROUPS:
        if journey_window in group:
            return sorted(group)
    return [journey_window]


@dataclass(frozen=True)
class PatternEvidence:
    channel: str
    mechanism: str
    sent: int
    returned: dict[str, tuple[int, int]]  # horizon -> (returned_true, measured)
    unsubscribed: int
    item_id: str | None = None
    item_version: str | None = None
    arm_counts: dict[str, int] | None = None


async def pattern_summaries(
    session: AsyncSession, journey_window: str, channels: list[str], limit: int = 500
) -> list[PatternEvidence]:
    rows = (
        await session.execute(
            select(TouchOutcomeRow)
            .where(
                TouchOutcomeRow.journey_window.in_(evidence_windows(journey_window)),
                TouchOutcomeRow.channel.in_(channels),
                TouchOutcomeRow.evidence_limitation.is_(None),
            )
            .order_by(TouchOutcomeRow.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    grouped: dict[tuple[str, str], list[TouchOutcomeRow]] = {}
    for row in rows:
        # V3 learns at canonical item level; legacy rows fall back to mechanism.
        grouped.setdefault((row.channel, row.item_id or row.mechanism), []).append(row)
    patterns = []
    for (channel, _item_key), group in sorted(grouped.items()):
        returned: dict[str, tuple[int, int]] = {}
        for horizon in _HORIZONS:
            values = [getattr(r, f"returned_{horizon}") for r in group]
            measured = [v for v in values if v is not None]
            returned[horizon] = (sum(1 for v in measured if v), len(measured))
        patterns.append(
            PatternEvidence(
                channel=channel,
                mechanism=group[0].mechanism,
                sent=len(group),
                returned=returned,
                unsubscribed=sum(1 for r in group if r.unsubscribed),
                item_id=group[0].item_id,
                item_version=group[0].item_version,
                arm_counts={
                    arm: sum(1 for r in group if r.arm == arm)
                    for arm in ("A", "B")
                    if any(r.arm == arm for r in group)
                } or None,
            )
        )
    return patterns


async def failed_mechanisms(session: AsyncSession, pro_id: str) -> list[str]:
    """Mechanisms that recently failed FOR THIS PRO: an unsubscribe, or a
    measured 30-day no-return. Spec gate: a new candidate must be materially
    different from recent failed touches — same mechanism is not different.
    Reduced in SQL (DISTINCT + the failure predicate) instead of loading
    every full row into Python just to throw most of it away."""
    rows = (
        await session.execute(
            select(TouchOutcomeRow.mechanism)
            .distinct()
            .where(
                TouchOutcomeRow.pro_id == pro_id,
                TouchOutcomeRow.evidence_limitation.is_(None),
                TouchOutcomeRow.mechanism != "",
                (TouchOutcomeRow.unsubscribed.is_(True)) | (TouchOutcomeRow.returned_30d.is_(False)),
            )
        )
    ).scalars().all()
    return sorted(rows)


def evidence_block(patterns: list[PatternEvidence]) -> str:
    """Prompt-ready evidence text. Honest when empty — the generator must know
    it is working without historical support, not assume silence means novelty."""
    if not patterns:
        return (
            "No historical outcome evidence is available for this journey window yet. "
            "Treat every idea as unproven."
        )
    lines = []
    for p in patterns:
        horizons = ", ".join(
            f"{h} return {t}/{m}" for h, (t, m) in p.returned.items() if m > 0
        ) or "no return horizons measured yet"
        lines.append(
            f"- {p.mechanism} via {p.channel}: {p.sent} sent, {horizons}, "
            f"{p.unsubscribed} unsubscribed"
        )
    return "Observed outcomes for similar pros (returns to the app are the goal):\n" + "\n".join(
        lines
    )
