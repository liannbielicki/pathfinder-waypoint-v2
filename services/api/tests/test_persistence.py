import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.models import MeasurementIndicator, MeasurementPlan, RunCreate
from waypoint.tables import HandoffRow, PersonaEvalRow, RunRow, TouchOutcomeRow


def test_run_requires_clean_audience_lineage() -> None:
    run = RunCreate(
        pro_ids=["pro_1", "pro_2"],
        audience_query="audience_v7",
        audience_run="2026-08-06T18:00:00Z",
        channels=["email"],
    )
    assert run.pro_ids == ["pro_1", "pro_2"]
    assert run.audience_query == "audience_v7"


def test_run_rejects_empty_audience() -> None:
    with pytest.raises(ValidationError):
        RunCreate(pro_ids=[], audience_query="q", audience_run="r", channels=["email"])


def test_measurement_plan_accepts_only_one_or_two_indicators() -> None:
    indicator = MeasurementIndicator(
        key="invoices_sent",
        label="Invoices sent",
        direction="increase",
        source="billing",
        window_days=30,
        rationale="The proposal sends invoices.",
    )
    assert MeasurementPlan(indicators=[indicator]).indicators == [indicator]
    assert len(MeasurementPlan(indicators=[indicator, indicator]).indicators) == 2
    with pytest.raises(ValidationError):
        MeasurementPlan(indicators=[])
    with pytest.raises(ValidationError):
        MeasurementPlan(indicators=[indicator, indicator, indicator])


async def _seed_run(db_session: AsyncSession, run_id: str) -> None:
    db_session.add(
        RunRow(
            id=run_id,
            pro_ids=["pro_1"],
            audience_query="audience_v7",
            audience_run="2026-08-06T18:00:00Z",
            channels=["email"],
        )
    )
    await db_session.flush()


async def test_duplicate_handoff_key_is_rejected(db_session: AsyncSession) -> None:
    await _seed_run(db_session, "run_1")
    first = HandoffRow(run_id="run_1", idempotency_key="run_1:winner_1", payload={})
    db_session.add(first)
    await db_session.commit()
    db_session.add(HandoffRow(run_id="run_1", idempotency_key="run_1:winner_1", payload={}))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_measurement_indicator_count_is_database_enforced(db_session: AsyncSession) -> None:
    from waypoint.tables import MeasurementRow

    await _seed_run(db_session, "run_2")
    db_session.add(MeasurementRow(run_id="run_2", indicators=[]))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_evolve_round_ledger_round_trips(db_session: AsyncSession) -> None:
    from sqlalchemy import select

    from waypoint.tables import EvolveRoundRow

    await _seed_run(db_session, "run_3")
    db_session.add(
        EvolveRoundRow(
            run_id="run_3",
            pro_id="pro_1",
            round=1,
            mechanism="invoice_delivery",
            candidate_id=None,
            outcome="win",
            score_pp=2.5,
        )
    )
    await db_session.commit()
    row = (
        await db_session.execute(select(EvolveRoundRow).where(EvolveRoundRow.run_id == "run_3"))
    ).scalar_one()
    assert (row.round, row.mechanism, row.outcome, row.score_pp) == (
        1,
        "invoice_delivery",
        "win",
        2.5,
    )


async def test_duplicate_round_number_is_rejected(db_session: AsyncSession) -> None:
    from waypoint.tables import EvolveRoundRow

    await _seed_run(db_session, "run_4")
    db_session.add(
        EvolveRoundRow(
            run_id="run_4",
            pro_id="pro_1",
            round=1,
            mechanism="m",
            outcome="lose",
        )
    )
    await db_session.commit()
    db_session.add(
        EvolveRoundRow(
            run_id="run_4",
            pro_id="pro_1",
            round=1,
            mechanism="m2",
            outcome="lose",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_duplicate_call_key_is_rejected(db_session: AsyncSession) -> None:
    from waypoint.tables import LlmCallRow

    await _seed_run(db_session, "run_5")
    db_session.add(
        LlmCallRow(
            call_key="run_5:pro_1:r1:generate",
            run_id="run_5",
            pro_id="pro_1",
            stage="evolve",
            model="claude-sonnet-5",
            reserved_usd=1,
        )
    )
    await db_session.commit()
    db_session.add(
        LlmCallRow(
            call_key="run_5:pro_1:r1:generate",
            run_id="run_5",
            pro_id="pro_1",
            stage="evolve",
            model="claude-sonnet-5",
            reserved_usd=1,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_loop_config_snapshot_and_defaults_columns(db_session: AsyncSession) -> None:
    from waypoint.tables import FleetControlRow

    db_session.add(
        RunRow(
            id="run_6",
            pro_ids=["pro_1"],
            audience_query="q",
            audience_run="r",
            channels=["sms"],
            loop_config={"MAX_ROUNDS": 4},
        )
    )
    db_session.add(FleetControlRow(id=1, loop_defaults={"PATIENCE": 2}))
    await db_session.commit()
    run = await db_session.get(RunRow, "run_6")
    fleet = await db_session.get(FleetControlRow, 1)
    assert run is not None and run.loop_config == {"MAX_ROUNDS": 4}
    assert fleet is not None and fleet.loop_defaults == {"PATIENCE": 2}


async def test_touch_outcome_and_persona_eval_roundtrip(db_session) -> None:
    db_session.add(
        TouchOutcomeRow(
            recommendation_id="w-1",
            source="iterable_n8n",
            pro_id="pro_1",
            channel="sms",
            mechanism="invoice_delivery",
            journey_window="churn_risk",
            returned_7d=True,
        )
    )
    db_session.add(
        PersonaEvalRow(cache_key="abc123", reactions={"p1": 5.0}, snapshot_version="s1")
    )
    await db_session.commit()
    outcome = (await db_session.execute(
        select(TouchOutcomeRow).where(TouchOutcomeRow.recommendation_id == "w-1")
    )).scalar_one()
    assert outcome.returned_7d is True
    assert outcome.returned_30d is None  # not-yet-measurable stays honestly unknown
    assert outcome.evidence_limitation is None


async def test_run_defaults_to_churn_risk_window(db_session) -> None:
    run = RunRow(pro_ids=["p"], audience_query="q", audience_run="r", channels=["sms"])
    db_session.add(run)
    await db_session.commit()
    assert run.journey_window == "churn_risk"
