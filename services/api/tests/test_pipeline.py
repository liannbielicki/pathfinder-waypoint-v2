from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.pipeline import run_job
from waypoint.queue import set_kill
from waypoint.tables import CandidateRow, MeasurementRow, RunRow, WinnerRow

from .conftest import FakeDeps, reactions_json


async def run_status(session: AsyncSession, run_id: str) -> str:
    return (await session.execute(
        select(RunRow.status).where(RunRow.id == run_id)
    )).scalar_one()


async def candidate_count(session: AsyncSession, run_id: str) -> int:
    return (await session.execute(
        select(func.count()).select_from(CandidateRow).where(CandidateRow.run_id == run_id)
    )).scalar_one()


async def test_happy_path_completes_with_winner_and_measurement(
    deps: FakeDeps, seeded_job,
) -> None:
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "complete"
    winner = (await deps.db.execute(
        select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id)
    )).scalar_one()
    assert winner.kind == "winner"
    assert winner.candidate_id is not None
    assert winner.evidence["final"]["reduction_pp"] > 1.0
    measurement = (await deps.db.execute(
        select(MeasurementRow).where(MeasurementRow.run_id == seeded_job.run_id)
    )).scalar_one()
    assert measurement.indicators[0]["key"] == "invoices_sent"
    # Screen used the fast tier; the five-person final check used the deep tier.
    assert ("screen", "fast") in deps.llm.calls
    assert ("final", "deep") in deps.llm.calls


async def test_evidence_stays_attached_to_candidates(deps: FakeDeps, seeded_job) -> None:
    await run_job(seeded_job.id, deps)
    rows = (await deps.db.execute(
        select(CandidateRow).where(CandidateRow.run_id == seeded_job.run_id)
    )).scalars().all()
    assert len(rows) == 3
    for row in rows:
        assert row.critics["block_kind"] == "none"
        panel = row.persona_evidence["screen"]["panel"]
        assert len(panel["items"]) == 3
        assert row.score["screen"]["calibration_version"] == "22cc4a1c89354327"


async def test_generation_failure_never_uses_canned_ideas(deps: FakeDeps, seeded_job) -> None:
    deps.llm.fail_stage("generate")
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "failed"
    assert await candidate_count(deps.db, seeded_job.run_id) == 0


async def test_critic_failure_fails_closed(deps: FakeDeps, seeded_job) -> None:
    deps.llm.fail_stage("critics")
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "failed"


async def test_kill_switch_stops_before_next_paid_stage(deps: FakeDeps, seeded_job) -> None:
    await set_kill(deps.db, True)
    await deps.db.commit()
    await run_job(seeded_job.id, deps)
    run = await deps.db.get(RunRow, seeded_job.run_id)
    assert run is not None
    assert run.status == "stopped"
    assert run.stop_reason == "fleet_killed"
    assert deps.llm.call_count == 0


async def test_budget_exhaustion_is_an_honest_stop(deps: FakeDeps, seeded_job) -> None:
    run = await deps.db.get(RunRow, seeded_job.run_id)
    assert run is not None
    run.cost_limit = Decimal("0.00")
    await deps.db.commit()
    await run_job(seeded_job.id, deps)
    refreshed = await deps.db.get(RunRow, seeded_job.run_id)
    assert refreshed is not None
    assert refreshed.status == "stopped"
    assert refreshed.stop_reason == "budget_exhausted"


async def test_context_outage_waits_for_retry(deps: FakeDeps, seeded_job) -> None:
    deps.context.unavailable = True
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "waiting"


async def test_flat_reactions_resolve_to_no_action(deps: FakeDeps, seeded_job) -> None:
    flat = reactions_json(deps.calibration.pivot)
    deps.llm.responses["screen"] = flat
    deps.llm.responses["final"] = flat
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "no_action"
    winner = (await deps.db.execute(
        select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id)
    )).scalar_one()
    assert winner.kind == "no_action"
    # A flat screen triggers one bounded search round, never a canned fallback.
    assert deps.llm.calls_for("generate") == 2


async def test_unmatchable_pro_abstains_with_low_panel_fit(deps: FakeDeps, seeded_job) -> None:
    deps.personas = [p for p in deps.personas if p.family == "solo_operators"]
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "abstained"
    winner = (await deps.db.execute(
        select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id)
    )).scalar_one()
    assert winner.kind == "abstained"
    assert "panel" in winner.rationale
