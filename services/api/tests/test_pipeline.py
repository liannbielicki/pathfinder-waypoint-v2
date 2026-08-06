from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.llm import RateLimitExhausted
from waypoint.measurement import UnmeasurableWinner
from waypoint.pipeline import run_job
from waypoint.queue import claim_job, enqueue, set_kill
from waypoint.tables import CandidateRow, FleetControlRow, MeasurementRow, RunRow, WinnerRow

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


async def test_lost_lease_stops_paid_work_immediately(deps: FakeDeps, seeded_job) -> None:
    # Another worker owns the claim; this worker's heartbeat must fail before
    # the first paid call.
    async with deps.db.begin_nested():
        claimed = await claim_job(deps.db, "worker-other")
        assert claimed is not None and claimed.id == seeded_job.id
    await deps.db.commit()
    deps.worker_id = "worker-loser"
    await run_job(seeded_job.id, deps)
    assert deps.llm.call_count == 0
    assert await candidate_count(deps.db, seeded_job.run_id) == 0


async def test_owner_heartbeats_keep_the_lease_alive(deps: FakeDeps, seeded_job) -> None:
    job = await claim_job(deps.db, "worker-owner", lease_seconds=60)
    assert job is not None
    await deps.db.commit()
    deps.worker_id = "worker-owner"
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "complete"


async def test_operator_kill_mid_run_keeps_its_honest_reason(
    deps: FakeDeps, seeded_job,
) -> None:
    original = deps.store.complete_stage

    async def stop_after_generate(job_id: str, stage: str, payload=None) -> None:
        await original(job_id, stage, payload)
        if stage == "generate":
            run = await deps.db.get(RunRow, seeded_job.run_id)
            run.status = "stopped"
            run.stop_reason = "operator_kill"
            await deps.db.commit()

    deps.store.complete_stage = stop_after_generate  # type: ignore[method-assign]
    await run_job(seeded_job.id, deps)
    run = await deps.db.get(RunRow, seeded_job.run_id)
    assert run is not None
    await deps.db.refresh(run)
    assert run.status == "stopped"
    assert run.stop_reason == "operator_kill"  # never relabeled budget_exhausted
    assert deps.llm.calls_for("screen") == 0


async def test_missing_context_pro_abstains_and_run_degrades(
    deps: FakeDeps, db_session: AsyncSession,
) -> None:
    run = RunRow(
        id="run-ghost", pro_ids=["pro_1", "pro_ghost"], audience_query="q",
        audience_run="r", channels=["sms"], cost_limit=Decimal("100.00"),
    )
    db_session.add(run)
    db_session.add(FleetControlRow(id=1, day_cost_limit=Decimal("1000.00")))
    await db_session.flush()
    job_id = await enqueue(db_session, run.id, stage="recommend")
    await db_session.commit()
    await run_job(job_id, deps)
    assert await run_status(db_session, run.id) == "degraded"
    winners = {(w.pro_id): w for w in (await db_session.execute(
        select(WinnerRow).where(WinnerRow.run_id == run.id)
    )).scalars()}
    assert winners["pro_1"].kind == "winner"
    assert winners["pro_ghost"].kind == "abstained"
    assert "context" in winners["pro_ghost"].rationale


async def test_unmeasurable_winner_abstains(deps: FakeDeps, seeded_job) -> None:
    async def unmeasurable(winner, llm, catalog):
        raise UnmeasurableWinner("unknown metric: imaginary")

    deps.create_plan = unmeasurable
    await run_job(seeded_job.id, deps)
    winner = (await deps.db.execute(
        select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id)
    )).scalar_one()
    assert winner.kind == "abstained"
    assert "unmeasurable" in winner.rationale


async def test_transient_measure_failure_retries_instead_of_abstaining(
    deps: FakeDeps, seeded_job,
) -> None:
    async def flaky(winner, llm, catalog):
        raise RateLimitExhausted("429 storm")

    deps.create_plan = flaky
    with pytest.raises(RateLimitExhausted):
        await run_job(seeded_job.id, deps)
    winner = (await deps.db.execute(
        select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id)
    )).scalar_one()
    assert winner.kind == "winner"  # a validated winner survives a 429 storm
    assert await run_status(deps.db, seeded_job.run_id) not in ("abstained", "failed")


async def test_malformed_reactions_abstain_candidates_not_the_job(
    deps: FakeDeps, seeded_job,
) -> None:
    deps.llm.responses["screen"] = "not json at all"
    deps.llm.responses["final"] = "not json at all"
    await run_job(seeded_job.id, deps)  # must not raise
    assert await run_status(deps.db, seeded_job.run_id) == "no_action"


async def test_unmatchable_pro_abstains_with_low_panel_fit(deps: FakeDeps, seeded_job) -> None:
    deps.personas = [p for p in deps.personas if p.family == "solo_operators"]
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "abstained"
    winner = (await deps.db.execute(
        select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id)
    )).scalar_one()
    assert winner.kind == "abstained"
    assert "panel" in winner.rationale
