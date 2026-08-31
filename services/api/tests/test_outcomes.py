import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from waypoint.models import TouchOutcomeIn
from waypoint.outcomes import ingest
from waypoint.tables import CandidateRow, RunRow, TouchOutcomeRow, WinnerRow

# V3: a 7d positive is DERIVED from a confirmed send plus a real return
# event; callers cannot assert horizons (returned_* was dropped at the wire).
SENT = datetime(2026, 8, 1, 12, tzinfo=UTC)
RETURNED_WITHIN_7D = {
    "send_status": "confirmed", "sent_at": SENT,
    "first_return_at": SENT + timedelta(days=3),
}


async def _ingest_with_new_session(
    factory: async_sessionmaker, body: list[TouchOutcomeIn]
) -> dict[str, int]:
    async with factory() as session:
        return await ingest(session, body)


async def test_concurrent_duplicate_outcomes_do_not_500_or_lose_siblings(
    db_session_factory,
) -> None:
    # Two sibling requests race to submit the SAME (recommendation_id, source)
    # pair concurrently. One batch also carries an unrelated sibling item —
    # the fix must not lose it when the race is resolved.
    dup = TouchOutcomeIn(recommendation_id="dup-winner", source="iterable_n8n", pro_id="pro_1")
    sibling = TouchOutcomeIn(
        recommendation_id="dup-winner-2", source="iterable_n8n", pro_id="pro_1"
    )
    results = await asyncio.gather(
        _ingest_with_new_session(db_session_factory, [dup, sibling]),
        _ingest_with_new_session(db_session_factory, [dup]),
    )
    for result in results:
        assert result["stored"] >= 1  # neither call raised / 500'd

    async with db_session_factory() as session:
        rows = (await session.execute(select(TouchOutcomeRow))).scalars().all()
    keys = {(r.recommendation_id, r.source) for r in rows}
    # Exactly one row per (recommendation_id, source) — the duplicate collapsed
    # into one row, and the sibling item survived the race.
    assert keys == {("dup-winner", "iterable_n8n"), ("dup-winner-2", "iterable_n8n")}


async def _winner_with_run(session, *, mechanism: str = "invoice_delivery"):
    """One run + champion candidate + winner, the shape every resolver test needs."""
    run = RunRow(pro_ids=["pro_1"], audience_query="q", audience_run="r",
                 channels=["sms"], journey_window="churn_risk")
    session.add(run)
    await session.flush()
    candidate = CandidateRow(
        run_id=run.id, pro_id="pro_1", status="champion",
        recommendation={"title": "t", "mechanism": mechanism, "actions": ["a"],
                        "pro_facing_concept": "c", "manager_rationale": "m",
                        "channel": "sms", "risk": ""},
    )
    session.add(candidate)
    await session.flush()
    winner = WinnerRow(run_id=run.id, pro_id="pro_1", kind="winner",
                       candidate_id=candidate.id, rationale="m")
    session.add(winner)
    await session.commit()
    return run, winner


async def test_run_id_and_pro_id_resolve_to_the_winner_without_a_waypoint_id(
    db_session_factory,
) -> None:
    # The whole point of the natural key: nothing Waypoint-shaped was stamped
    # into the message, yet the outcome still attributes exactly.
    async with db_session_factory() as session:
        run, winner = await _winner_with_run(session)

    result = await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       channel="sms", routing="route-to-pro", **RETURNED_WITHIN_7D),
    ])
    assert result == {"stored": 1, "unattributed": 0}

    async with db_session_factory() as session:
        row = (await session.execute(select(TouchOutcomeRow))).scalar_one()
        assert row.recommendation_id == winner.id
        assert row.evidence_limitation is None
        assert row.mechanism == "invoice_delivery"
        assert (await session.get(WinnerRow, winner.id)).warm_start_eligible is True


async def test_a_guardrailed_send_never_becomes_evidence_or_a_warm_start(
    db_session_factory,
) -> None:
    # The dangerous case: the copy is about a real Pro and Amplitude will report
    # that Pro's organic app activity, but the message went to an internal inbox.
    async with db_session_factory() as session:
        run, winner = await _winner_with_run(session)

    result = await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       channel="sms", routing="guardrail", **RETURNED_WITHIN_7D),
    ])
    assert result == {"stored": 1, "unattributed": 1}

    async with db_session_factory() as session:
        row = (await session.execute(select(TouchOutcomeRow))).scalar_one()
        assert row.evidence_limitation is not None
        assert "not a real-Pro send" in row.evidence_limitation
        # The load-bearing assertion: a return observed on a touch nobody
        # received must not propagate this mechanism to other Pros.
        assert (await session.get(WinnerRow, winner.id)).warm_start_eligible is False


async def test_unknown_routing_is_disqualified_too(db_session_factory) -> None:
    # Absence of proof is not proof: a source that never says how it routed
    # cannot mint evidence by omission.
    async with db_session_factory() as session:
        run, winner = await _winner_with_run(session)

    result = await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       **RETURNED_WITHIN_7D),
    ])
    assert result == {"stored": 1, "unattributed": 1}
    async with db_session_factory() as session:
        assert (await session.get(WinnerRow, winner.id)).warm_start_eligible is False


async def test_an_unresolvable_pair_stores_one_honest_row_per_pair(
    db_session_factory,
) -> None:
    # Two different unresolvable pairs must not collide on one "" key.
    result = await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id="ghost-run", pro_id="pro_1", source="iterable_n8n",
                       routing="route-to-pro"),
        TouchOutcomeIn(run_id="ghost-run", pro_id="pro_2", source="iterable_n8n",
                       routing="route-to-pro"),
    ])
    assert result == {"stored": 2, "unattributed": 2}
    async with db_session_factory() as session:
        rows = (await session.execute(select(TouchOutcomeRow))).scalars().all()
    assert {r.recommendation_id for r in rows} == {
        "unresolved:ghost-run:pro_1", "unresolved:ghost-run:pro_2"
    }
    assert all("matches no winner" in r.evidence_limitation for r in rows)


async def test_a_record_naming_no_touch_at_all_is_refused_at_the_boundary() -> None:
    with pytest.raises(ValidationError):
        TouchOutcomeIn(source="iterable_n8n", returned_7d=True)
    with pytest.raises(ValidationError):
        TouchOutcomeIn(source="iterable_n8n", run_id="run-1")  # pro_id missing


# --- the routing gate across MULTIPLE submissions ---------------------------
# One touch is written several times: the Iterable send event, then each return
# horizon as the Amplitude sweep fills it in. The gate used to be evaluated only
# on the first write, so a later submission could launder a guardrailed return
# onto a row already marked clean. These pin the merged behaviour.


async def test_a_later_guardrail_submission_cannot_launder_a_clean_row(
    db_session_factory,
) -> None:
    # The documented operating shape: send event first, return horizon second.
    async with db_session_factory() as session:
        run, winner = await _winner_with_run(session)

    await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       routing="route-to-pro", delivered=True),
    ])
    result = await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       routing="guardrail", **RETURNED_WITHIN_7D),
    ])
    assert result == {"stored": 1, "unattributed": 1}

    async with db_session_factory() as session:
        row = (await session.execute(select(TouchOutcomeRow))).scalar_one()
        assert row.evidence_limitation is not None
        assert (await session.get(WinnerRow, winner.id)).warm_start_eligible is False


async def test_a_later_real_send_cannot_promote_a_guardrailed_return(
    db_session_factory,
) -> None:
    # Reverse order, same verdict: two sources disagree, so we cannot say the Pro
    # received it, so the return it carries is not evidence.
    async with db_session_factory() as session:
        run, winner = await _winner_with_run(session)

    await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       routing="guardrail", **RETURNED_WITHIN_7D),
    ])
    await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       routing="route-to-pro"),
    ])
    async with db_session_factory() as session:
        row = (await session.execute(select(TouchOutcomeRow))).scalar_one()
        assert row.routing == "conflict"
        assert row.evidence_limitation is not None
        assert (await session.get(WinnerRow, winner.id)).warm_start_eligible is False


async def test_mixed_routing_within_one_batch_fails_closed(db_session_factory) -> None:
    async with db_session_factory() as session:
        run, winner = await _winner_with_run(session)

    result = await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       routing="route-to-pro", delivered=True),
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       routing="guardrail", **RETURNED_WITHIN_7D),
    ])
    assert result["unattributed"] == 1
    async with db_session_factory() as session:
        assert (await session.get(WinnerRow, winner.id)).warm_start_eligible is False


async def test_the_horizon_sweep_does_not_demote_a_proven_real_send(
    db_session_factory,
) -> None:
    # The case the merge rule exists to PRESERVE: the Amplitude sweep knows the
    # return but not the routing, and must not undo what the send event proved.
    async with db_session_factory() as session:
        run, winner = await _winner_with_run(session)

    await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       routing="route-to-pro", channel="sms", delivered=True,
                       send_status="confirmed", sent_at=SENT),
    ])
    result = await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       first_return_at=SENT + timedelta(days=3)),  # no routing claim
    ])
    assert result == {"stored": 1, "unattributed": 0}

    async with db_session_factory() as session:
        row = (await session.execute(select(TouchOutcomeRow))).scalar_one()
        assert row.routing == "route-to-pro"
        assert row.evidence_limitation is None
        assert (await session.get(WinnerRow, winner.id)).warm_start_eligible is True


async def test_a_late_winner_still_re_attributes_on_the_id_path(
    db_session_factory,
) -> None:
    # The pre-existing promise: a stale "no winner" label clears once the winner
    # exists — but only when routing also holds up.
    async with db_session_factory() as session:
        _, winner = await _winner_with_run(session)

    await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(recommendation_id="not-yet", source="iterable_n8n",
                       routing="route-to-pro", **RETURNED_WITHIN_7D),
    ])
    async with db_session_factory() as session:
        row = (await session.execute(select(TouchOutcomeRow))).scalar_one()
        assert "matches no winner" in row.evidence_limitation

    result = await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(recommendation_id=winner.id, source="iterable_n8n",
                       routing="route-to-pro", **RETURNED_WITHIN_7D),
    ])
    assert result == {"stored": 1, "unattributed": 0}


async def test_the_unattributed_count_matches_what_was_stored(
    db_session_factory,
) -> None:
    # The one metric an operator watches must never claim the gate fired when the
    # stored row says otherwise.
    async with db_session_factory() as session:
        run, _ = await _winner_with_run(session)

    for routing, expected in (("route-to-pro", 0), ("guardrail", 1), ("", 1)):
        result = await _ingest_with_new_session(db_session_factory, [
            TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source=f"src_{expected}_{routing}",
                           routing=routing, delivered=True),
        ])
        async with db_session_factory() as session:
            rows = (await session.execute(
                select(TouchOutcomeRow).where(
                    TouchOutcomeRow.source == f"src_{expected}_{routing}"
                )
            )).scalars().all()
        stored_unattributed = sum(1 for r in rows if r.evidence_limitation is not None)
        assert result["unattributed"] == stored_unattributed == expected


async def test_a_no_action_row_is_not_a_touch_on_either_key_path(
    db_session_factory,
) -> None:
    async with db_session_factory() as session:
        run = RunRow(pro_ids=["pro_9"], audience_query="q", audience_run="r",
                     channels=["sms"], journey_window="churn_risk")
        session.add(run)
        await session.flush()
        skipped = WinnerRow(run_id=run.id, pro_id="pro_9", kind="no_action", rationale="")
        session.add(skipped)
        await session.commit()
        skipped_id, run_id = skipped.id, run.id

    for item in (
        TouchOutcomeIn(recommendation_id=skipped_id, source="a", routing="route-to-pro"),
        TouchOutcomeIn(run_id=run_id, pro_id="pro_9", source="b", routing="route-to-pro"),
    ):
        assert (await _ingest_with_new_session(db_session_factory, [item]))["unattributed"] == 1


async def test_a_colon_in_the_natural_key_is_refused(db_session_factory) -> None:
    # Two different pairs must never render the same "unresolved:<run>:<pro>" key.
    with pytest.raises(ValidationError):
        TouchOutcomeIn(run_id="a:b", pro_id="c", source="s")
    with pytest.raises(ValidationError):
        TouchOutcomeIn(run_id="   ", pro_id="pro_1", source="s")


async def test_a_later_disqualification_revokes_an_earlier_warm_start(
    db_session_factory,
) -> None:
    # Mirror image of the laundering above: the clean write lands FIRST and the
    # guardrail signal second. Eligibility must be recomputed, not just granted —
    # warm_start_eligible propagates the mechanism cross-org, so a stale grant
    # escapes permanently even though the row itself is correctly labelled.
    async with db_session_factory() as session:
        run, winner = await _winner_with_run(session)

    await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       routing="route-to-pro", **RETURNED_WITHIN_7D),
    ])
    async with db_session_factory() as session:
        assert (await session.get(WinnerRow, winner.id)).warm_start_eligible is True

    await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       routing="guardrail", delivered=True),
    ])
    async with db_session_factory() as session:
        row = (await session.execute(select(TouchOutcomeRow))).scalar_one()
        assert row.routing == "conflict"
        assert row.evidence_limitation is not None
        revoked = await session.get(WinnerRow, winner.id)
        assert revoked.warm_start_eligible is False
        assert revoked.validation_status is None
        assert revoked.warm_start_evidence == {}


async def test_revocation_also_happens_within_one_batch(db_session_factory) -> None:
    async with db_session_factory() as session:
        run, winner = await _winner_with_run(session)

    await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       routing="route-to-pro", **RETURNED_WITHIN_7D),
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="iterable_n8n",
                       routing="guardrail", delivered=True),
    ])
    async with db_session_factory() as session:
        assert (await session.get(WinnerRow, winner.id)).warm_start_eligible is False


async def test_a_second_clean_source_still_holds_eligibility_up(
    db_session_factory,
) -> None:
    # Demotion must key on surviving evidence, not on "something got labelled":
    # one disqualified source cannot erase another source's real observation.
    async with db_session_factory() as session:
        run, winner = await _winner_with_run(session)

    await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="src_clean",
                       routing="route-to-pro", **RETURNED_WITHIN_7D),
        TouchOutcomeIn(run_id=run.id, pro_id="pro_1", source="src_dirty",
                       routing="guardrail", **RETURNED_WITHIN_7D),
    ])
    async with db_session_factory() as session:
        still = await session.get(WinnerRow, winner.id)
        assert still.warm_start_eligible is True
        assert still.warm_start_evidence["source"] == "src_clean"


async def test_the_winner_owns_the_identity_not_the_submitter(
    db_session_factory,
) -> None:
    # A submission naming a different Pro used to be stored verbatim, writing a
    # measured outcome — and, via evidence.failed_mechanisms, a mechanism block —
    # against someone who was never touched.
    async with db_session_factory() as session:
        _, winner = await _winner_with_run(session)

    await _ingest_with_new_session(db_session_factory, [
        TouchOutcomeIn(recommendation_id=winner.id, source="iterable_n8n",
                       pro_id="SOMEONE_ELSE", org_id="OTHER_ORG",
                       routing="route-to-pro", returned_30d=False),
    ])
    async with db_session_factory() as session:
        row = (await session.execute(select(TouchOutcomeRow))).scalar_one()
    assert row.pro_id == "pro_1"
    assert row.pro_id != "SOMEONE_ELSE"
