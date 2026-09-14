"""Outcome ingestion (spec: `/api/outcomes`).

Observed messaging/app-usage outcomes, keyed by recommendation_id (a Waypoint
winner id or an exposure id), by exposure_id, or by the natural (run_id,
pro_id) pair — see TouchOutcomeIn and `_resolve_run_pro`. Attributable records
backfill run/pro/journey_window/mechanism/channel/org and the canonical item
identity from the winner or exposure; everything else is stored with an
explicit evidence_limitation label (spec: label the limitation, never
pretend), stays queryable for audit, and never reaches the evidence readers or
warm-start promotion.

Two things disqualify a record as evidence, and both label rather than drop it:
no resolvable winner or exposure, and a send that did not provably reach the
real Pro (REAL_SEND_ROUTING). Where an exposure exists, ITS routing claim is
authoritative; otherwise routing is merged across the source's submissions
(merge_routing) and two disagreeing claims fail closed.

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
warm-start eligibility: an attributable, routing-proven winner with a measured
7-day return — A+B (causal) when arm-tagged rows exist, legacy otherwise.
A-only positives are directional: recorded, never eligible. Eligibility is
RECOMPUTED from scratch on every write, never only granted — a later
disqualification (routing merging to conflict, say) must DEMOTE, or the
mechanism escapes through the cross-org warm-start channel and stays escaped.
Nothing on the scoring/persona side may set eligibility.

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

# The ONLY routing mode whose outcomes are evidence. A guardrailed send carries a
# real Pro's context but is delivered to an internal inbox, so the Pro never got
# it — while Amplitude will still happily report that Pro's organic app activity.
# Joining the two would manufacture returns for touches nobody received, and
# `_promote_warm_start` would then seed future runs off them. Positive proof is
# required: anything that is not exactly this is labelled, never counted.
REAL_SEND_ROUTING = "route-to-pro"

CONFLICTING_ROUTING = "conflict"


async def _resolve_run_pro(
    session: AsyncSession, body: list[TouchOutcomeIn]
) -> list[TouchOutcomeIn]:
    """Fill in `recommendation_id` for items keyed on the natural (run_id, pro_id)
    pair, in one query for the whole batch.

    `uq_winners_run_pro` is what makes this exact rather than heuristic: one run
    plus one pro is at most one winner, so the pair names a touch as precisely as
    the winner id does — with no Waypoint identifier stamped into the message.
    An unresolvable pair keeps a stable, namespaced key so it still stores as one
    honest unattributed row instead of colliding with every other unresolved one.

    ponytail: an outcome that arrives BEFORE its winner exists is keyed
    "unresolved:<run>:<pro>", and a later resubmission — now resolvable — writes a
    SECOND row under the real winner id rather than re-keying the first. The
    orphan stays labelled, so it never reaches the evidence readers or warm-start
    promotion; it only double-counts in a raw count(*) over touch_outcomes. Left
    alone because the ordering barely happens: a send event cannot precede its own
    winner, and human QA puts days between the two. Re-key the orphan (or merge it
    under the unique constraint) if the unattributed count ever shows it does.
    """
    needs = [item for item in body if not item.recommendation_id]
    if not needs:
        return body
    rows = (
        await session.execute(
            select(WinnerRow.id, WinnerRow.run_id, WinnerRow.pro_id).where(
                WinnerRow.run_id.in_({item.run_id for item in needs}),
                WinnerRow.pro_id.in_({item.pro_id for item in needs}),
                WinnerRow.kind == "winner",
            )
        )
    ).all()
    by_pair = {(row.run_id, row.pro_id): row.id for row in rows}
    return [
        item
        if item.recommendation_id
        else item.model_copy(
            update={
                "recommendation_id": by_pair.get((item.run_id, item.pro_id))
                or f"unresolved:{item.run_id}:{item.pro_id}"
            }
        )
        for item in body
    ]


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


def merge_routing(stored: str, submitted: str) -> str:
    """Routing is a property of the touch as seen BY ONE SOURCE — the merge is
    per (recommendation_id, source), because that is the grain of the row. One
    source writes a touch several times (the send event, then each return
    horizon) and does not know the routing on every one of those writes.

    CONSEQUENCE, and it is sharp: a SECOND source must re-assert routing on its
    own submissions. It does not inherit the first source's proof, so a manual
    backfill or a second flow that omits `routing` produces rows that are
    permanently non-evidence — silently. Registering the send as an EXPOSURE
    (whose routing is authoritative for every outcome attributed to it) removes
    the per-source re-assertion burden entirely.

    So: an empty claim defers to what is already known (the horizon sweep must
    not demote a proven real send), a first claim is taken, and two sources that
    DISAGREE fail closed to `conflict`, which is not REAL_SEND_ROUTING and so
    never counts as evidence. Failing closed matters more than picking a winner:
    if we cannot tell whether the Pro received the message, we do not get to
    treat their app activity as a response to it.

    Recovery from `conflict` is deliberately manual — it is terminal, so a
    later correct claim cannot quietly rehabilitate a touch a bad run poisoned:
        UPDATE touch_outcomes SET routing = '' WHERE recommendation_id = ...;
    then resubmit.
    """
    if not submitted:
        return stored
    if not stored:
        return submitted
    return stored if stored == submitted else CONFLICTING_ROUTING


def evidence_limitation(
    winner: WinnerRow | None,
    exposure: ExposureRow | None,
    routing: str,
    delivered: bool | None = None,
) -> str | None:
    """Why this record cannot be evidence, or None when it can.

    DERIVED, and recomputed on every write. Computing it only on the first
    submission let a later guardrailed return land on a row already marked
    clean — the exact laundering the routing gate exists to prevent.

    delivered=False (a bounce) disqualifies like bad routing does: the Pro
    provably never received the message, so its silence must never become a
    measured negative against the winner.
    """
    if winner is None and exposure is None:
        return "unattributed: recommendation_id matches no winner or exposure"
    if routing != REAL_SEND_ROUTING:
        return f"not a real-Pro send: routing={routing or 'unknown'!r}"
    if delivered is False:
        return "send bounced: never delivered to the Pro"
    return None


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
        (
            await session.execute(
                # kind == "winner" matches _resolve_run_pro: a no_action /
                # abstained row is not a touch, so neither key path may
                # attribute an outcome to one.
                select(WinnerRow).where(
                    WinnerRow.id.in_(winner_ids), WinnerRow.kind == "winner"
                )
            )
        )
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
            await session.execute(
                select(WinnerRow).where(
                    WinnerRow.id.in_(linked_winner_ids), WinnerRow.kind == "winner"
                )
            )
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
) -> dict[str, Any]:
    if winner is None and exposure is None:
        return {}
    recommendation = candidate.recommendation if candidate else {}
    if exposure is not None:
        # The exposure IS the identity authority — arm, routing, send state,
        # and item identity come from it, never from the caller or the winner.
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
            "routing": exposure.routing,
            "channel": exposure.channel,
            "sent_at": exposure.sent_at,
            "send_status": exposure.send_status,
            "mechanism": recommendation.get("mechanism", "") if candidate else "",
        }
    assert winner is not None  # no exposure and the first guard passed
    return {
        "run_id": winner.run_id,
        # NOT item.pro_id: a submission that named a different Pro would pin a
        # measured outcome (and, via evidence.failed_mechanisms, a mechanism
        # block) on someone who was never touched. The winner knows who it was
        # for; the submitter is at best guessing.
        "pro_id": winner.pro_id,
        "journey_window": run.journey_window if run else "churn_risk",
        "mechanism": recommendation.get("mechanism", "") if candidate else "",
        # item-supplied non-empty channel wins; backfill only fills blanks.
        "channel": item.channel or recommendation.get("channel", ""),
        "org_id": winner.evidence.get("org_id", "") or item.org_id,
        "item_id": winner.item_id,
        "item_version": winner.item_version,
    }


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
    return on an attributable, routing-proven winner. Derived from EVERY row
    attributable to this winner — direct outcome rows plus its exposures' rows
    (any observed return wins, ties broken by source name), so duplicates,
    late arrivals, and a lagging second source converge on the same values
    whatever order they land in.

    Rows carrying an evidence_limitation are excluded, which is the
    load-bearing line: eligibility propagates a mechanism to OTHER Pros, so a
    guardrailed or unattributed row promoted here would seed every future
    similar run off a touch nobody received. The evidence readers filter the
    same way in SQL.

    Eligibility is RECOMPUTED from scratch, never only granted. An earlier
    submission can be disqualified by a later one (routing merging to
    `conflict`, say), and an empty `observed` then means "no surviving
    evidence" — which has to DEMOTE, not leave a stale grant standing."""
    if winner is None or winner.kind != "winner":
        return
    observed = [
        row
        for row in rows
        if row.returned_7d is not None and row.evidence_limitation is None
    ]
    recommendation = candidate.recommendation if candidate else {}
    if not observed:
        # Nothing measured survives: strip the claim rather than leave a stale one.
        winner.warm_start_eligible = False
        winner.validation_status = None
        winner.warm_start_evidence = {}
        return

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
    """Adds or updates one outcome row; returns True when the STORED row lacks
    attribution — the row's own state, not the submission's, so the count an
    operator watches can never disagree with what is in the table."""
    fill = _attribution_fill(item, winner, exposure, run, candidate)
    key = (item.recommendation_id, item.source)
    existing = existing_by_key.get(key)
    if existing is None:
        routing = exposure.routing if exposure is not None else item.routing
        fields = {
            "recommendation_id": item.recommendation_id,
            "source": item.source,
            "org_id": item.org_id,
            "channel": item.channel,
            "sent_at": item.sent_at,
            "first_return_at": item.first_return_at,
            "routing": routing,
            "evidence_limitation": evidence_limitation(winner, exposure, routing, item.delivered),
            "pro_id": item.pro_id,
            "exposure_id": item.exposure_id,
            "send_status": item.send_status,
            "send_confirmed_at": item.send_confirmed_at,
            **{k: getattr(item, k) for k in _OUTCOME_FLAGS},
            **fill,
        }
        stored = TouchOutcomeRow(**fields)
        if stored.send_status == "confirmed" and stored.first_return_at is not None:
            for flag, value in derive_checkpoint_flags(
                sent_at=stored.sent_at, first_return_at=stored.first_return_at
            ).items():
                setattr(stored, flag, value)
        session.add(stored)
        existing_by_key[key] = stored
    else:
        # Re-attribution is a RECOMPUTE, never an unconditional clear: the
        # routing half of the verdict still has to hold, and a row attributed
        # on arrival still refreshes identity from the authority records.
        if exposure is not None:
            existing.routing = exposure.routing  # the exposure's claim is authoritative
        else:
            existing.routing = merge_routing(existing.routing, item.routing)
        if winner is not None or exposure is not None:
            for field_name, value in fill.items():
                setattr(existing, field_name, value)
        _apply_flags(existing, item)
        # After _apply_flags: the verdict must see the flags this submission
        # just measured (a bounce landing on an existing row disqualifies it).
        existing.evidence_limitation = evidence_limitation(
            winner, exposure, existing.routing, existing.delivered
        )
        stored = existing
    return stored.evidence_limitation is not None


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
    body = await _resolve_run_pro(session, body)
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
