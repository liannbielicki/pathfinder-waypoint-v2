"""Bounded V3 checkpoint resolution.

Measurement starts from authoritative send confirmation — never LCM intake
acknowledgement. A horizon flag left NULL is "not yet measurable"; this sweep
turns it into a measured False only once the observation window plus a grace
lag has provably closed with no qualifying return event. It also synthesizes
negative outcome rows (source="checkpoint") for confirmed exposures that never
produced an outcome event — without them the B/control side of A+B causal
evidence would never exist.

Scheduling is bounded (LIMIT per sweep, oldest first, backed by
ix_touch_outcomes_checkpoint_due) and idempotent: any failure rolls back and
the next worker beat retries the same rows. The sweep is gated by the
learning-loop kill switch (fleet_control.learning_killed), which is
independent of the fleet kill switch.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.outcomes import derive_checkpoint_flags, evidence_limitation, promote_winners
from waypoint.tables import (
    CandidateRow,
    ExposureRow,
    FleetControlRow,
    PollCursorRow,
    RunRow,
    TouchOutcomeRow,
    WinnerRow,
)

log = logging.getLogger("waypoint.checkpoints")

LEARNING_VERSION = "waypoint_learning_v3"
CHECKPOINT_VERSION = "checkpoints_v1"

# The window must be PROVABLY complete: sources lag, so a horizon is due only
# this long after it nominally closes.
GRACE = timedelta(hours=6)

HORIZONS: tuple[tuple[str, timedelta], ...] = (
    ("returned_1d", timedelta(days=1)),
    ("returned_7d", timedelta(days=7)),
    ("returned_30d", timedelta(days=30)),
)


def _due_horizons(
    sent_at: datetime, now: datetime, returns_covered: datetime | None
) -> list[str]:
    """Horizons whose window has provably closed: past its nominal end plus
    GRACE (sources index late), and — when return coverage is per-exposure
    (returns_covered is not None-by-gate, see resolve_due_checkpoints) — with
    the exposure's return events fetched past the horizon's close. Without
    that proof a backfilled send would be graded "no return" before its
    returns were ever fetched — a permanent false negative."""
    return [
        flag
        for flag, window in HORIZONS
        if sent_at + window + GRACE <= now
        and (returns_covered is None or sent_at + window <= returns_covered)
    ]


async def _synthesize_exposure_rows(
    session: AsyncSession, now: datetime, limit: int
) -> int:
    """Confirmed exposures old enough for their first checkpoint that have no
    outcome row at all get one (source="checkpoint") so their negatives exist."""
    first_due = now - HORIZONS[0][1] - GRACE
    rows = (
        await session.execute(
            select(ExposureRow)
            .where(
                ExposureRow.send_status == "confirmed",
                ExposureRow.sent_at.is_not(None),
                ExposureRow.sent_at <= first_due,
                ~select(TouchOutcomeRow.id)
                .where(TouchOutcomeRow.exposure_id == ExposureRow.id)
                .exists(),
                ~select(TouchOutcomeRow.id)
                .where(TouchOutcomeRow.recommendation_id == ExposureRow.id)
                .exists(),
                # An outcome keyed by the linked WINNER (ingested before this
                # exposure registered, so exposure_id is NULL on it) already
                # measures this send — synthesizing here would manufacture a
                # second measurement of the same physical send.
                ~select(TouchOutcomeRow.id)
                .where(TouchOutcomeRow.recommendation_id == ExposureRow.winner_id)
                .exists(),
            )
            .order_by(ExposureRow.sent_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    run_ids = {e.run_id for e in rows if e.run_id}
    runs: dict[str, RunRow] = {}
    if run_ids:
        runs = {
            r.id: r
            for r in (
                await session.execute(select(RunRow).where(RunRow.id.in_(run_ids)))
            ).scalars()
        }
    for exposure in rows:
        run = runs.get(exposure.run_id) if exposure.run_id else None
        session.add(
            TouchOutcomeRow(
                recommendation_id=exposure.id,
                exposure_id=exposure.id,
                source="checkpoint",
                run_id=exposure.run_id,
                # The run's window when known, so a synthesized control never
                # pollutes another window's evidence with the column default.
                **({"journey_window": run.journey_window} if run else {}),
                pro_id=exposure.pro_id,
                org_id=exposure.org_id,
                item_id=exposure.item_id,
                item_version=exposure.item_version,
                arm=exposure.arm,
                channel=exposure.channel,
                # The exposure's routing claim rides along, and the evidence
                # gate applies to synthesized rows exactly as to ingested ones:
                # a guardrailed send's silence is not evidence either.
                routing=exposure.routing,
                evidence_limitation=evidence_limitation(None, exposure, exposure.routing),
                send_status=exposure.send_status,
                sent_at=exposure.sent_at,
            )
        )
    return len(rows)


async def resolve_due_checkpoints(
    session: AsyncSession, now: datetime, limit: int = 500, gated: bool = False
) -> dict[str, int]:
    """One bounded sweep. Returns {"resolved": n, "synthesized": m}.

    With gated=True (the amplitude poller owns return ingestion), a horizon
    may be stamped a measured negative only once its row's exposure carries a
    returns_checked_at stamp past the horizon's close — the per-exposure
    proof that the pro's return events were actually fetched. Rows with no
    exposure or no stamp only ever derive positives from first_return_at."""
    synthesized = await _synthesize_exposure_rows(session, now, limit)

    due_flags = [
        TouchOutcomeRow.returned_1d.is_(None),
        TouchOutcomeRow.returned_7d.is_(None),
        TouchOutcomeRow.returned_30d.is_(None),
    ]
    conditions = [
        TouchOutcomeRow.send_status == "confirmed",
        TouchOutcomeRow.sent_at.is_not(None),
        TouchOutcomeRow.sent_at <= now - HORIZONS[0][1] - GRACE,
        due_flags[0] | due_flags[1] | due_flags[2],
    ]
    if gated:
        # A row whose exposure has NO coverage stamp (unresolved amplitude
        # identity, out-paged history, or no exposure link at all) can never
        # be graded — left in the query, those permanently-ungradable rows
        # accumulate at the oldest-first head until they fill the LIMIT and
        # newer resolvable rows are never swept again.
        conditions.append(
            select(ExposureRow.id)
            .where(
                ExposureRow.id == TouchOutcomeRow.exposure_id,
                ExposureRow.returns_checked_at.is_not(None),
            )
            .exists()
        )
    rows = (
        await session.execute(
            select(TouchOutcomeRow)
            .where(*conditions)
            .order_by(TouchOutcomeRow.sent_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    coverage: dict[str, datetime | None] = {}
    if gated:
        exposure_ids = {row.exposure_id for row in rows if row.exposure_id}
        if exposure_ids:
            coverage = {
                e.id: e.returns_checked_at
                for e in (
                    await session.execute(
                        select(ExposureRow).where(ExposureRow.id.in_(exposure_ids))
                    )
                ).scalars()
            }
    resolved: list[TouchOutcomeRow] = []
    for row in rows:
        if row.sent_at is None:  # excluded by the query; narrows the type
            continue
        # A real event derives its horizons; a horizon whose window has
        # provably closed with no QUALIFYING return (none at all, or only a
        # pre-send timestamp derive_checkpoint_flags refuses) is a measured
        # negative.
        derived = derive_checkpoint_flags(
            sent_at=row.sent_at, first_return_at=row.first_return_at
        )
        covered = coverage.get(row.exposure_id or "") if gated else None
        if gated and covered is None:
            due = []  # returns never fetched: only positives may derive
        else:
            due = _due_horizons(row.sent_at, now, covered)
        for flag in due:
            if derived.get(flag) is None:
                derived[flag] = False
        changed = False
        for flag, value in derived.items():
            if getattr(row, flag) is None and value is not None:
                setattr(row, flag, value)
                changed = True
        if changed:
            row.checkpoint_version = CHECKPOINT_VERSION
            resolved.append(row)

    # A newly measured Day-7 fact changes what the winner has proven — re-run
    # promotion for every winner the resolved rows reach: directly by
    # recommendation_id, or through the exposure's winner link (how a silent
    # control's negative feeds the causal comparison).
    affected_ids = {row.recommendation_id for row in resolved if row.returned_7d is not None}
    exposure_ids = {row.exposure_id for row in resolved if row.exposure_id}
    if exposure_ids:
        linked = (
            await session.execute(
                select(ExposureRow.winner_id).where(
                    ExposureRow.id.in_(exposure_ids), ExposureRow.winner_id.is_not(None)
                )
            )
        ).scalars().all()
        affected_ids.update(w for w in linked if w)
    if affected_ids:
        winners = (
            await session.execute(select(WinnerRow).where(WinnerRow.id.in_(affected_ids)))
        ).scalars().all()
        candidate_ids = [w.candidate_id for w in winners if w.candidate_id]
        candidates: dict[str, CandidateRow] = {}
        if candidate_ids:
            candidates = {
                c.id: c
                for c in (
                    await session.execute(
                        select(CandidateRow).where(CandidateRow.id.in_(candidate_ids))
                    )
                ).scalars()
            }
        await promote_winners(session, winners, candidates)

    try:
        await session.commit()
    except IntegrityError:
        # A concurrent sweep synthesized the same (exposure, "checkpoint") row
        # first. Nothing is lost: roll back and let the next tick resolve
        # against the now-current rows.
        await session.rollback()
        log.info("checkpoint sweep lost a synthesis race; retrying next tick")
        return {"resolved": 0, "synthesized": 0}
    return {"resolved": len(resolved), "synthesized": synthesized}


async def sweep_if_enabled(
    session: AsyncSession, now: datetime, limit: int = 500
) -> dict[str, int] | None:
    """One sweep, gated by the learning kill switch ONLY — the fleet kill
    switch is independent and never stops measurement.

    A negative may only be stamped for a period whose returns are PROVABLY
    ingested. When the amplitude poller owns return ingestion (its heartbeat
    row exists in poll_cursors), that proof is the per-exposure
    returns_checked_at stamp — see resolve_due_checkpoints(gated=True). No
    heartbeat row (poller not in use) keeps the old wall-clock behavior.
    Runbook note: the row's existence outlives the poller — disabling the
    amplitude keys leaves the sweep gated (fail-safe: no negatives are ever
    stamped again); delete the poll_cursors "amplitude" row to un-gate."""
    fleet = await session.get(FleetControlRow, 1)
    if fleet is not None and fleet.learning_killed:
        return None
    amplitude = await session.get(PollCursorRow, "amplitude")
    return await resolve_due_checkpoints(session, now, limit, gated=amplitude is not None)
