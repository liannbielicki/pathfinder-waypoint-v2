"""V3 measurement spine: exposure-level identity, derived checkpoints, and
directional-vs-causal evidence. Callers can never assert horizons or identity;
arms exist only on exposures."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from waypoint.models import ExposureIn, TouchOutcomeIn
from waypoint.outcomes import _promote_warm_start, derive_checkpoint_flags, ingest
from waypoint.tables import ExposureRow, RunRow, TouchOutcomeRow, WinnerRow

SENT = datetime(2026, 8, 26, 12, tzinfo=UTC)


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


def test_a_return_predating_the_send_is_not_a_qualifying_return() -> None:
    """Clock skew / pre-touch activity must never derive a positive."""
    flags = derive_checkpoint_flags(
        sent_at=SENT, first_return_at=SENT - timedelta(hours=1)
    )

    assert flags == {"returned_1d": None, "returned_7d": None, "returned_30d": None}


async def test_control_arm_failures_do_not_suppress_mechanisms(db_session) -> None:
    """An untouched control's Day-7 negative must not veto the mechanism it
    benchmarks — only treated rows feed failed_mechanisms."""
    from waypoint.evidence import failed_mechanisms

    db_session.add(TouchOutcomeRow(
        recommendation_id="fm-control", source="checkpoint", pro_id="pro-fm",
        mechanism="mech_control", arm="B", returned_7d=False,
    ))
    db_session.add(TouchOutcomeRow(
        recommendation_id="fm-treated", source="amplitude", pro_id="pro-fm",
        mechanism="mech_treated", arm="A", returned_7d=False,
    ))
    await db_session.commit()

    assert await failed_mechanisms(db_session, "pro-fm") == ["mech_treated"]


def test_missing_timestamps_leave_checkpoint_flags_unresolved() -> None:
    assert derive_checkpoint_flags(sent_at=SENT, first_return_at=None) == {
        "returned_1d": None,
        "returned_7d": None,
        "returned_30d": None,
    }


def test_callers_cannot_assert_horizons_or_identity() -> None:
    """returned_* / arm / item identity fields are not part of the wire
    contract — pydantic drops them instead of letting a caller assert a
    measured fact or rewrite attribution."""
    outcome = TouchOutcomeIn.model_validate({
        "recommendation_id": "winner-1",
        "source": "hostile",
        "returned_7d": True,
        "returned_1d": True,
        "arm": "B",
        "item_id": "forged",
        "item_version": "v9",
    })
    assert not hasattr(outcome, "returned_7d")
    assert not hasattr(outcome, "arm")
    assert not hasattr(outcome, "item_id")


def test_a_only_positive_is_directional_and_revokes_eligibility() -> None:
    winner = WinnerRow(
        id="winner-a", run_id="run-a", pro_id="pro-a", kind="winner",
        warm_start_eligible=True,  # e.g. an earlier legacy promotion
    )
    row = TouchOutcomeRow(
        recommendation_id=winner.id, source="amplitude", arm="A", returned_7d=True,
        channel="sms",
    )

    _promote_warm_start([row], winner, None)

    assert winner.warm_start_eligible is False
    assert winner.validation_status == "directional"
    assert winner.warm_start_evidence["evidence_kind"] == "directional"


def test_a_plus_b_positive_records_causal_evidence_and_promotes() -> None:
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
    assert winner.validation_status == "validated"
    assert winner.warm_start_evidence["evidence_kind"] == "causal"


async def _seed_winner(db_session, run_id: str, winner_id: str, **winner_kwargs) -> None:
    db_session.add(RunRow(
        id=run_id, pro_ids=["pro-1"], audience_query="q", audience_run="r",
        channels=["sms"], journey_window=winner_kwargs.pop("journey_window", "churn_risk"),
    ))
    await db_session.flush()
    db_session.add(WinnerRow(
        id=winner_id, run_id=run_id, pro_id="pro-1", kind="winner", **winner_kwargs,
    ))
    await db_session.commit()


async def test_caller_identity_never_overrides_winner_attribution(db_session) -> None:
    await _seed_winner(
        db_session, "run-id-auth", "winner-id-auth",
        item_id="item-real", item_version="v1", evidence={"org_id": "org-real"},
    )

    await ingest(db_session, [TouchOutcomeIn(
        recommendation_id="winner-id-auth", source="hostile",
        pro_id="pro-forged", org_id="org-forged",
    )])

    row = (await db_session.execute(
        select(TouchOutcomeRow).where(TouchOutcomeRow.recommendation_id == "winner-id-auth")
    )).scalar_one()
    assert row.item_id == "item-real"
    assert row.item_version == "v1"
    assert row.pro_id == "pro-1"
    assert row.org_id == "org-real"


async def test_exposure_is_the_identity_authority_for_winner_outcomes(db_session) -> None:
    """An outcome citing both a winner and an exposure takes identity (arm,
    item, send state) from the exposure — the exposure-level unit."""
    await _seed_winner(
        db_session, "run-exp-auth", "winner-exp-auth",
        item_id="item-real", item_version="v1", evidence={"org_id": "org-real"},
    )
    db_session.add(ExposureRow(
        id="exp-auth", run_id="run-exp-auth", winner_id="winner-exp-auth",
        pro_id="pro-1", org_id="org-real", item_id="item-real", item_version="v1",
        arm="A", channel="sms", send_status="confirmed", sent_at=SENT,
    ))
    await db_session.commit()

    await ingest(db_session, [TouchOutcomeIn(
        recommendation_id="winner-exp-auth", exposure_id="exp-auth", source="amplitude",
        first_return_at=SENT + timedelta(days=2),
    )])

    row = (await db_session.execute(
        select(TouchOutcomeRow).where(TouchOutcomeRow.recommendation_id == "winner-exp-auth")
    )).scalar_one()
    assert row.arm == "A"
    assert row.send_status == "confirmed"
    assert row.returned_1d is False
    assert row.returned_7d is True


async def test_control_exposure_outcome_needs_no_winner(db_session) -> None:
    db_session.add(ExposureRow(
        id="exp-control", pro_id="pro-c", org_id="org-c",
        item_id="item-c", item_version="v1", arm="B", channel="sms",
        routing="route-to-pro",
    ))
    await db_session.commit()

    await ingest(db_session, [TouchOutcomeIn(
        recommendation_id="exp-control", source="amplitude",
    )])

    row = (await db_session.execute(
        select(TouchOutcomeRow).where(TouchOutcomeRow.recommendation_id == "exp-control")
    )).scalar_one()
    assert row.arm == "B"
    assert row.evidence_limitation is None
    assert row.item_id == "item-c"


async def test_exposure_outcome_inherits_the_run_journey_window(db_session) -> None:
    db_session.add(RunRow(
        id="run-jw", pro_ids=["pro-jw"], audience_query="q", audience_run="r",
        channels=["sms"], journey_window="onboarding",
    ))
    await db_session.flush()
    db_session.add(ExposureRow(
        id="exp-jw", run_id="run-jw", pro_id="pro-jw", arm="B", channel="sms",
    ))
    await db_session.commit()

    await ingest(db_session, [TouchOutcomeIn(recommendation_id="exp-jw", source="amplitude")])

    row = (await db_session.execute(
        select(TouchOutcomeRow).where(TouchOutcomeRow.recommendation_id == "exp-jw")
    )).scalar_one()
    assert row.journey_window == "onboarding"


async def test_a_return_plus_silent_control_promotes_causally_through_exposures(
    db_session,
) -> None:
    """The full A+B causal path: an A exposure with a real return, plus a
    linked control exposure whose measured negative arrives keyed by the
    exposure id — promotion must join them through the winner link."""
    from waypoint.exposures import register

    await _seed_winner(
        db_session, "run-causal", "winner-causal",
        item_id="item-x", item_version="v1", evidence={"org_id": "org-x"},
    )
    await register(db_session, [
        ExposureIn(
            exposure_id="exp-a", recommendation_id="winner-causal", arm="A",
            channel="sms", routing="route-to-pro", send_status="confirmed", sent_at=SENT,
        ),
        ExposureIn(
            exposure_id="exp-b", recommendation_id="winner-causal", arm="B",
            channel="sms", routing="route-to-pro", send_status="confirmed", sent_at=SENT,
        ),
    ])

    # The A side returns within 7 days; the B side reports a measured no-show.
    await ingest(db_session, [
        TouchOutcomeIn(
            recommendation_id="winner-causal", exposure_id="exp-a", source="amplitude",
            first_return_at=SENT + timedelta(days=2),
        ),
        TouchOutcomeIn(
            recommendation_id="exp-b", source="amplitude",
            first_return_at=SENT + timedelta(days=20),
        ),
    ])

    winner = await db_session.get(WinnerRow, "winner-causal")
    assert winner.warm_start_eligible is True
    assert winner.validation_status == "validated"
    assert winner.warm_start_evidence["evidence_kind"] == "causal"


async def test_ingestion_derives_v3_checkpoints_and_inherits_item_identity(
    db_session,
) -> None:
    await _seed_winner(
        db_session, "run-v3", "winner-v3", item_id="item-v3", item_version="v1",
    )

    await ingest(db_session, [TouchOutcomeIn(
        recommendation_id="winner-v3", source="amplitude", sent_at=SENT,
        send_status="confirmed", send_confirmed_at=SENT,
        first_return_at=SENT + timedelta(days=2),
    )])

    row = (await db_session.execute(
        select(TouchOutcomeRow).where(TouchOutcomeRow.recommendation_id == "winner-v3")
    )).scalar_one()
    assert (row.item_id, row.item_version) == ("item-v3", "v1")
    assert row.returned_1d is False
    assert row.returned_7d is True
    assert row.returned_30d is True
