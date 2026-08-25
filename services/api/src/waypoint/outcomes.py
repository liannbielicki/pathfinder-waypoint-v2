"""Outcome ingestion (spec: `/api/outcomes`).

Observed messaging/app-usage outcomes, keyed either by recommendation_id or by
the natural (run_id, pro_id) pair — see TouchOutcomeIn and `_resolve_run_pro`.
Attributable records backfill run_id/pro_id/journey_window/mechanism/channel/
org_id from the winner; everything else is stored with an explicit
evidence_limitation label (spec: label the limitation, never pretend). A
resubmission that arrives after the winner now exists clears a stale
evidence_limitation instead of carrying it forever.

Two things disqualify a record as evidence, and both label rather than drop it:
no resolvable winner, and a send that did not provably reach the real Pro
(REAL_SEND_ROUTING). Labelled rows stay queryable for audit and never reach the
evidence readers or warm-start promotion.

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

# The ONLY routing mode whose outcomes are evidence. A guardrailed send carries a
# real Pro's context but is delivered to an internal inbox, so the Pro never got
# it — while Amplitude will still happily report that Pro's organic app activity.
# Joining the two would manufacture returns for touches nobody received, and
# `_promote_warm_start` would then seed future runs off them. Positive proof is
# required: anything that is not exactly this is labelled, never counted.
REAL_SEND_ROUTING = "route-to-pro"


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


async def _prefetch_winners(
    session: AsyncSession, body: list[TouchOutcomeIn]
) -> tuple[dict[str, WinnerRow], dict[str, RunRow], dict[str, CandidateRow]]:
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


CONFLICTING_ROUTING = "conflict"


def merge_routing(stored: str, submitted: str) -> str:
    """Routing is a property of the touch as seen BY ONE SOURCE — the merge is
    per (recommendation_id, source), because that is the grain of the row. One
    source writes a touch several times (the send event, then each return
    horizon) and does not know the routing on every one of those writes.

    CONSEQUENCE, and it is sharp: a SECOND source must re-assert routing on its
    own submissions. It does not inherit the first source's proof, so a manual
    backfill or a second flow that omits `routing` produces rows that are
    permanently non-evidence — silently. Today both halves of the n8n flow post
    `source="iterable_n8n"`, so this is latent; it bites the moment anyone adds
    a second writer.

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


def evidence_limitation(winner: WinnerRow | None, routing: str) -> str | None:
    """Why this record cannot be evidence, or None when it can.

    DERIVED, and recomputed on every write. It was once computed only for the
    first submission of a (recommendation_id, source) key, which meant a second
    submission could carry a guardrailed return onto a row already marked clean
    — the exact laundering the routing gate exists to prevent.
    """
    if winner is None:
        return "unattributed: recommendation_id matches no winner"
    if routing != REAL_SEND_ROUTING:
        return f"not a real-Pro send: routing={routing or 'unknown'!r}"
    return None


def _attribution_fill(
    item: TouchOutcomeIn,
    winner: WinnerRow | None,
    run: RunRow | None,
    candidate: CandidateRow | None,
) -> dict[str, Any]:
    if winner is None:
        return {}
    recommendation = candidate.recommendation if candidate else {}
    fill = {
        "run_id": winner.run_id,
        # NOT item.pro_id: a submission that named a different Pro would pin a
        # measured outcome (and, via evidence.failed_mechanisms, a mechanism
        # block) on someone who was never touched. The winner knows who it was
        # for; the submitter is at best guessing.
        "pro_id": winner.pro_id,
        "journey_window": run.journey_window if run else "churn_risk",
        "mechanism": recommendation.get("mechanism", "") if candidate else "",
        # item-supplied non-empty values win; backfill only fills blanks.
        "channel": item.channel or recommendation.get("channel", ""),
        "org_id": winner.evidence.get("org_id", "") or item.org_id,
    }
    return fill


def _apply_flags(row: TouchOutcomeRow, item: TouchOutcomeIn) -> None:
    # Later horizons arrive later; non-None fields win, None never erases a
    # measured value.
    for key in _OUTCOME_FLAGS:
        value = getattr(item, key)
        if value is not None:
            setattr(row, key, value)
    if item.sent_at is not None:
        row.sent_at = item.sent_at


def _promote_warm_start(
    rows: list[TouchOutcomeRow], winner: WinnerRow | None, candidate: CandidateRow | None
) -> None:
    """The ONLY path that grants warm-start eligibility: a real observed 7-day
    return on an attributable winner. Derived from EVERY source's row for this
    winner (any observed return wins, ties broken by source name), so
    duplicates, late arrivals, and a lagging second source converge on the same
    values whatever order the batch applies them in.

    Rows carrying an evidence_limitation are excluded, which is the load-bearing
    line: eligibility propagates a mechanism to OTHER Pros, so a guardrailed or
    unattributed row promoted here would seed every future similar run off a
    touch nobody received. The evidence readers (evidence.pattern_summaries,
    evidence.failed_mechanisms) already filter the same way in SQL.

    Eligibility is RECOMPUTED from scratch here, never only granted. An earlier
    submission can be disqualified by a later one (routing merging to `conflict`,
    say), and an empty `observed` then means "no surviving evidence" — which has
    to DEMOTE. Returning early on it left the grant standing on a row that had
    since been labelled, so the mechanism escaped through the cross-org warm-start
    channel and stayed escaped: the same laundering the evidence gate exists to
    stop, arriving in the opposite order."""
    if winner is None or winner.kind != "winner":
        return
    observed = [
        row
        for row in rows
        if row.returned_7d is not None and row.evidence_limitation is None
    ]
    positive = [row for row in observed if row.returned_7d]
    recommendation = candidate.recommendation if candidate else {}
    winner.warm_start_eligible = bool(positive)
    if not observed:
        # Nothing measured survives: strip the claim rather than leave a stale one.
        winner.validation_status = None
        winner.warm_start_evidence = {}
        return
    row = min(positive or observed, key=lambda r: r.source)
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
    """Adds or updates one outcome row; returns True when the STORED row lacks
    attribution — the row's own state, not the submission's, so the count an
    operator watches can never disagree with what is in the table."""
    fill = _attribution_fill(item, winner, run, candidate)
    key = (item.recommendation_id, item.source)
    existing = existing_by_key.get(key)
    if existing is None:
        fields = {
            "recommendation_id": item.recommendation_id,
            "source": item.source,
            "org_id": item.org_id,
            "channel": item.channel,
            "sent_at": item.sent_at,
            "routing": item.routing,
            "evidence_limitation": evidence_limitation(winner, item.routing),
            "pro_id": item.pro_id,
            **{k: getattr(item, k) for k in _OUTCOME_FLAGS},
            **fill,
        }
        stored = TouchOutcomeRow(**fields)
        session.add(stored)
        existing_by_key[key] = stored
    else:
        # A row stored unattributed must not keep evidence_limitation forever
        # once the winner exists — but re-attribution is a RECOMPUTE, never an
        # unconditional clear: the routing half of the verdict still has to hold.
        existing.routing = merge_routing(existing.routing, item.routing)
        if winner is not None:
            for field_name, value in fill.items():
                setattr(existing, field_name, value)
        existing.evidence_limitation = evidence_limitation(winner, existing.routing)
        _apply_flags(existing, item)
        stored = existing
    # Every source's row for this recommendation, not just the one applied now:
    # eligibility must not depend on which source lands last in the batch.
    _promote_warm_start(
        [row for (rec_id, _), row in existing_by_key.items() if rec_id == item.recommendation_id],
        winner,
        candidate,
    )
    return stored.evidence_limitation is not None


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
    body = await _resolve_run_pro(session, body)
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
