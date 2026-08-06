import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.models import MeasurementIndicator, MeasurementPlan, RunCreate
from waypoint.tables import HandoffRow, RunRow


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
        key="invoices_sent", label="Invoices sent", direction="increase",
        source="billing", window_days=30, rationale="The proposal sends invoices.",
    )
    assert MeasurementPlan(indicators=[indicator]).indicators == [indicator]
    assert len(MeasurementPlan(indicators=[indicator, indicator]).indicators) == 2
    with pytest.raises(ValidationError):
        MeasurementPlan(indicators=[])
    with pytest.raises(ValidationError):
        MeasurementPlan(indicators=[indicator, indicator, indicator])


async def _seed_run(db_session: AsyncSession, run_id: str) -> None:
    db_session.add(RunRow(
        id=run_id, pro_ids=["pro_1"], audience_query="audience_v7",
        audience_run="2026-08-06T18:00:00Z", channels=["email"],
    ))
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
