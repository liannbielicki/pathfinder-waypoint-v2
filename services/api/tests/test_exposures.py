"""Exposure registration: canonical exposure-level identity.

Neutral/control exposures need no WinnerRow; winner-linked exposures derive
identity from the winner and never from the caller. Send confirmation updates
start the measurement clock in place.
"""

from datetime import UTC, datetime

from waypoint.exposures import register
from waypoint.models import ExposureIn
from waypoint.tables import ExposureRow, RunRow, WinnerRow

SENT = datetime(2026, 8, 26, 12, tzinfo=UTC)


async def test_control_exposure_registers_without_a_winner(db_session) -> None:
    result = await register(db_session, [ExposureIn(
        exposure_id="exp-ctl-1", pro_id="pro-1", org_id="org-1",
        item_id="item-1", item_version="v1", arm="B", channel="sms",
    )])
    assert result == {"stored": 1, "unknown_recommendation": 0}
    row = await db_session.get(ExposureRow, "exp-ctl-1")
    assert row is not None
    assert row.arm == "B"
    assert row.pro_id == "pro-1"
    assert row.learning_version != ""


async def test_winner_linked_exposure_derives_identity_from_the_winner(db_session) -> None:
    db_session.add(RunRow(
        id="run-e1", pro_ids=["pro-real"], audience_query="q", audience_run="r",
        channels=["sms"],
    ))
    await db_session.flush()
    db_session.add(WinnerRow(
        id="win-e1", run_id="run-e1", pro_id="pro-real", kind="winner",
        item_id="item-real", item_version="v2", evidence={"org_id": "org-real"},
    ))
    await db_session.commit()

    await register(db_session, [ExposureIn(
        exposure_id="exp-w1", recommendation_id="win-e1", arm="A", channel="sms",
        pro_id="pro-forged", org_id="org-forged",
        item_id="item-forged", item_version="v9",
    )])

    row = await db_session.get(ExposureRow, "exp-w1")
    assert row.pro_id == "pro-real"
    assert row.org_id == "org-real"
    assert row.item_id == "item-real"
    assert row.item_version == "v2"
    assert row.run_id == "run-e1"


async def test_send_confirmation_updates_in_place_without_touching_identity(db_session) -> None:
    await register(db_session, [ExposureIn(
        exposure_id="exp-conf", pro_id="pro-1", org_id="org-1",
        item_id="item-1", item_version="v1", arm="B", channel="sms",
    )])
    await register(db_session, [ExposureIn(
        exposure_id="exp-conf", pro_id="pro-other",
        send_status="confirmed", sent_at=SENT,
    )])
    row = await db_session.get(ExposureRow, "exp-conf")
    assert row.send_status == "confirmed"
    assert row.sent_at == SENT
    assert row.pro_id == "pro-1"  # identity is immutable after registration
    assert row.item_id == "item-1"


async def test_unknown_recommendation_is_reported_not_stored(db_session) -> None:
    result = await register(db_session, [ExposureIn(
        exposure_id="exp-miss", recommendation_id="no-such-winner",
    )])
    assert result == {"stored": 0, "unknown_recommendation": 1}
    assert await db_session.get(ExposureRow, "exp-miss") is None
