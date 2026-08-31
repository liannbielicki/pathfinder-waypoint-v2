"""Bounded checkpoint resolution: unmeasured horizons become measured
negatives only after the send is confirmed AND the observation window (plus
grace) has provably closed. Day 30 resolves too, but stays diagnostic-only
downstream (evidence.py). The sweep also synthesizes negative outcome rows for
confirmed exposures that never produced an outcome event — the control side of
A+B causal evidence.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from waypoint.checkpoints import (
    CHECKPOINT_VERSION,
    GRACE,
    resolve_due_checkpoints,
)
from waypoint.tables import ExposureRow, RunRow, TouchOutcomeRow, WinnerRow

NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)


def confirmed_outcome(**kwargs) -> TouchOutcomeRow:
    defaults = {
        "recommendation_id": "rec-1",
        "source": "iterable",
        "send_status": "confirmed",
        "sent_at": NOW - timedelta(days=2),
    }
    return TouchOutcomeRow(**{**defaults, **kwargs})


async def test_day1_resolves_to_a_measured_negative_after_the_window_closes(db_session) -> None:
    db_session.add(confirmed_outcome())
    await db_session.commit()

    result = await resolve_due_checkpoints(db_session, now=NOW)

    row = (await db_session.execute(select(TouchOutcomeRow))).scalar_one()
    assert row.returned_1d is False
    assert row.returned_7d is None  # window not provably complete yet
    assert row.returned_30d is None
    assert row.checkpoint_version == CHECKPOINT_VERSION
    assert result["resolved"] == 1


async def test_all_due_horizons_resolve_after_thirty_days(db_session) -> None:
    db_session.add(confirmed_outcome(sent_at=NOW - timedelta(days=31)))
    await db_session.commit()

    await resolve_due_checkpoints(db_session, now=NOW)

    row = (await db_session.execute(select(TouchOutcomeRow))).scalar_one()
    assert (row.returned_1d, row.returned_7d, row.returned_30d) == (False, False, False)


async def test_grace_period_holds_a_barely_due_horizon(db_session) -> None:
    db_session.add(confirmed_outcome(sent_at=NOW - timedelta(days=1) - GRACE / 2))
    await db_session.commit()

    await resolve_due_checkpoints(db_session, now=NOW)

    row = (await db_session.execute(select(TouchOutcomeRow))).scalar_one()
    assert row.returned_1d is None  # inside the grace window: not provably complete


async def test_unconfirmed_sends_are_never_resolved(db_session) -> None:
    db_session.add(confirmed_outcome(send_status="unknown", sent_at=NOW - timedelta(days=40)))
    await db_session.commit()

    result = await resolve_due_checkpoints(db_session, now=NOW)

    row = (await db_session.execute(select(TouchOutcomeRow))).scalar_one()
    assert row.returned_1d is None
    assert result["resolved"] == 0


async def test_observed_return_beats_the_sweep(db_session) -> None:
    db_session.add(confirmed_outcome(
        sent_at=NOW - timedelta(days=8),
        first_return_at=NOW - timedelta(days=7, hours=12),
    ))
    await db_session.commit()

    await resolve_due_checkpoints(db_session, now=NOW)

    row = (await db_session.execute(select(TouchOutcomeRow))).scalar_one()
    assert row.returned_1d is True  # derived from the real event, not forced False
    assert row.returned_7d is True


async def test_sweep_is_bounded(db_session) -> None:
    for i in range(3):
        db_session.add(confirmed_outcome(recommendation_id=f"rec-{i}"))
    await db_session.commit()

    result = await resolve_due_checkpoints(db_session, now=NOW, limit=2)

    assert result["resolved"] == 2
    unresolved = [
        r for r in (await db_session.execute(select(TouchOutcomeRow))).scalars()
        if r.returned_1d is None
    ]
    assert len(unresolved) == 1  # the next beat picks it up


async def test_confirmed_exposure_without_events_gets_a_synthesized_negative(db_session) -> None:
    db_session.add(ExposureRow(
        id="exp-quiet", pro_id="pro-1", org_id="org-1", item_id="item-1",
        item_version="v1", arm="B", channel="sms",
        send_status="confirmed", sent_at=NOW - timedelta(days=2),
    ))
    await db_session.commit()

    result = await resolve_due_checkpoints(db_session, now=NOW)

    row = (await db_session.execute(
        select(TouchOutcomeRow).where(TouchOutcomeRow.recommendation_id == "exp-quiet")
    )).scalar_one()
    assert row.source == "checkpoint"
    assert row.arm == "B"
    assert row.item_id == "item-1"
    assert row.returned_1d is False
    assert row.checkpoint_version == CHECKPOINT_VERSION
    assert result["synthesized"] == 1


async def test_resolved_day7_negative_reruns_winner_promotion(db_session) -> None:
    db_session.add(RunRow(
        id="run-cp", pro_ids=["pro-1"], audience_query="q", audience_run="r",
        channels=["sms"],
    ))
    await db_session.flush()
    db_session.add(WinnerRow(id="win-cp", run_id="run-cp", pro_id="pro-1", kind="winner"))
    db_session.add(confirmed_outcome(
        recommendation_id="win-cp", run_id="run-cp", sent_at=NOW - timedelta(days=8),
    ))
    await db_session.commit()

    await resolve_due_checkpoints(db_session, now=NOW)

    winner = await db_session.get(WinnerRow, "win-cp")
    assert winner.validation_status == "validated_negative"
    assert winner.warm_start_eligible is False


async def test_sweep_is_idempotent(db_session) -> None:
    db_session.add(confirmed_outcome())
    await db_session.commit()

    first = await resolve_due_checkpoints(db_session, now=NOW)
    second = await resolve_due_checkpoints(db_session, now=NOW)

    assert first["resolved"] == 1
    assert second["resolved"] == 0


async def test_sweep_respects_the_independent_learning_kill_switch(db_session) -> None:
    from waypoint.checkpoints import sweep_if_enabled
    from waypoint.tables import FleetControlRow

    db_session.add(FleetControlRow(id=1, killed=False, learning_killed=True))
    db_session.add(confirmed_outcome())
    await db_session.commit()

    assert await sweep_if_enabled(db_session, now=NOW) is None
    row = (await db_session.execute(select(TouchOutcomeRow))).scalar_one()
    assert row.returned_1d is None  # nothing resolved while learning is killed


async def test_fleet_kill_does_not_stop_the_learning_sweep(db_session) -> None:
    """The switches are independent: killing run processing must not silently
    stop measurement, and vice versa."""
    from waypoint.checkpoints import sweep_if_enabled
    from waypoint.tables import FleetControlRow

    db_session.add(FleetControlRow(id=1, killed=True, learning_killed=False))
    db_session.add(confirmed_outcome())
    await db_session.commit()

    result = await sweep_if_enabled(db_session, now=NOW)
    assert result == {"resolved": 1, "synthesized": 0}
