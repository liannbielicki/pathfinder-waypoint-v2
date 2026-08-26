from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from waypoint.models import TouchOutcomeIn
from waypoint.outcomes import _promote_warm_start, derive_checkpoint_flags, ingest
from waypoint.tables import RunRow, TouchOutcomeRow, WinnerRow

SENT = datetime(2026, 8, 26, 12, tzinfo=UTC)


def test_touch_outcome_accepts_v3_identity_and_nullable_arm() -> None:
    outcome = TouchOutcomeIn(
        recommendation_id="winner-1",
        source="lcm_personalization",
        item_id="item-1",
        item_version="v2",
        arm="A",
    )

    assert outcome.item_id == "item-1"
    assert outcome.item_version == "v2"
    assert outcome.arm == "A"


def test_checkpoint_flags_are_derived_from_first_return_at() -> None:
    flags = derive_checkpoint_flags(
        sent_at=SENT,
        first_return_at=SENT + timedelta(hours=12),
    )

    assert flags == {"returned_1d": True, "returned_7d": True, "returned_30d": True}


def test_return_after_one_day_is_not_a_day_one_return() -> None:
    flags = derive_checkpoint_flags(
        sent_at=SENT,
        first_return_at=SENT + timedelta(days=2),
    )

    assert flags["returned_1d"] is False
    assert flags["returned_7d"] is True
    assert flags["returned_30d"] is True


def test_missing_timestamps_leave_checkpoint_flags_unresolved() -> None:
    assert derive_checkpoint_flags(sent_at=SENT, first_return_at=None) == {
        "returned_1d": None,
        "returned_7d": None,
        "returned_30d": None,
    }


def test_outcome_row_has_v3_identity_and_checkpoint_state() -> None:
    winner = WinnerRow(
        id="winner-1",
        run_id="run-1",
        pro_id="pro-1",
        kind="winner",
        item_id="item-1",
        item_version="v1",
    )
    row = TouchOutcomeRow(
        recommendation_id=winner.id,
        source="test",
        item_id=winner.item_id,
        item_version=winner.item_version,
        arm=None,
        first_return_at=SENT,
        returned_1d=True,
    )

    assert row.item_id == "item-1"
    assert row.item_version == "v1"
    assert row.arm is None
    assert row.first_return_at == SENT
    assert row.returned_1d is True


def test_checkpoint_flags_reject_a_caller_supplied_positive_without_event() -> None:
    with pytest.raises(ValueError, match="first_return_at"):
        derive_checkpoint_flags(sent_at=SENT, first_return_at=None, returned_7d=True)


def test_a_only_positive_is_directional_and_does_not_promote() -> None:
    winner = WinnerRow(id="winner-a", run_id="run-a", pro_id="pro-a", kind="winner")
    row = TouchOutcomeRow(
        recommendation_id=winner.id, source="amplitude", arm="A", returned_7d=True
    )

    _promote_warm_start([row], winner, None)

    assert winner.warm_start_eligible is not True
    assert winner.validation_status is None


def test_a_plus_b_positive_can_promote() -> None:
    winner = WinnerRow(id="winner-ab", run_id="run-ab", pro_id="pro-ab", kind="winner")
    rows = [
        TouchOutcomeRow(
            recommendation_id=winner.id, source="a", arm="A", returned_7d=True, channel="sms"
        ),
        TouchOutcomeRow(
            recommendation_id=winner.id, source="b", arm="B", returned_7d=False, channel="sms"
        ),
    ]

    _promote_warm_start(rows, winner, None)

    assert winner.warm_start_eligible is True


async def test_ingestion_derives_v3_checkpoints_and_inherits_item_identity(
    db_session,
) -> None:
    db_session.add(RunRow(
        id="run-v3", pro_ids=["pro-1"], audience_query="q", audience_run="r", channels=["sms"]
    ))
    db_session.add(WinnerRow(
        id="winner-v3", run_id="run-v3", pro_id="pro-1", kind="winner",
        item_id="item-v3", item_version="v1",
    ))
    await db_session.commit()

    await ingest(db_session, [TouchOutcomeIn(
        recommendation_id="winner-v3", source="amplitude", sent_at=SENT,
        first_return_at=SENT + timedelta(days=2), arm="A",
    )])

    row = (await db_session.execute(
        select(TouchOutcomeRow).where(TouchOutcomeRow.recommendation_id == "winner-v3")
    )).scalar_one()
    assert (row.item_id, row.item_version, row.arm) == ("item-v3", "v1", "A")
    assert row.returned_1d is False
    assert row.returned_7d is True
    assert row.returned_30d is True
