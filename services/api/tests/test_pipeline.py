from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.calls import BudgetExhausted  # noqa: F401 — re-exported contract
from waypoint.llm import RateLimitExhausted
from waypoint.measurement import UnmeasurableWinner
from waypoint.pipeline import finalize_run, run_job
from waypoint.queue import claim_job, enqueue, set_kill
from waypoint.tables import (
    CandidateRow,
    EvolveRoundRow,
    FleetControlRow,
    LlmCallRow,
    MeasurementRow,
    RunRow,
    WinnerRow,
)

from .conftest import (
    CRITIC_BLOCK,
    CRITIC_OK,
    PERSONAS,
    FakeDeps,
    idea_json,
    reactions_json,
)

# Reaction → reduction_pp under the fixture calibration (3 identical reactions):
# 4.0 → -0.92 · 4.6 → 1.19 · 5.0 → 2.40 · 5.1 → 2.68 · 5.3 → 3.22 · 6.0 → 4.87
LOSE, FIRST_WIN, BETTER, NEAR_MISS, GOOD, GREAT = 4.0, 4.6, 5.0, 5.1, 5.3, 6.0


async def run_status(session: AsyncSession, run_id: str) -> str:
    return (await session.execute(select(RunRow.status).where(RunRow.id == run_id))).scalar_one()


async def candidate_count(session: AsyncSession, run_id: str) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(CandidateRow).where(CandidateRow.run_id == run_id)
        )
    ).scalar_one()


async def rounds(session: AsyncSession, run_id: str) -> list[EvolveRoundRow]:
    return list(
        (
            await session.execute(
                select(EvolveRoundRow)
                .where(EvolveRoundRow.run_id == run_id)
                .order_by(EvolveRoundRow.round)
            )
        ).scalars()
    )


async def seed_two_pro_job(db_session: AsyncSession) -> tuple[str, dict[str, str]]:
    run = RunRow(
        id="run-two",
        pro_ids=["pro_1", "pro_2"],
        audience_query="q",
        audience_run="r",
        channels=["sms"],
        cost_limit=Decimal("100.00"),
    )
    db_session.add(run)
    db_session.add(FleetControlRow(id=1, day_cost_limit=Decimal("1000.00")))
    await db_session.flush()
    jobs = {}
    for pro_id in run.pro_ids:
        jobs[pro_id] = await enqueue(db_session, run.id, stage="pro", pro_id=pro_id)
    await db_session.commit()
    return run.id, jobs


# --- happy path and evidence ------------------------------------------------


async def test_happy_path_completes_with_champion_and_measurement(
    deps: FakeDeps,
    seeded_job,
) -> None:
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "complete"
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "winner"
    assert winner.candidate_id is not None
    assert winner.evidence["final"]["reduction_pp"] > 1.0
    measurement = (
        await deps.db.execute(
            select(MeasurementRow).where(MeasurementRow.run_id == seeded_job.run_id)
        )
    ).scalar_one()
    assert measurement.indicators[0]["key"] == "invoices_sent"
    tiers = {(c["stage"], c["tier"]) for c in deps.gateway.calls}
    assert ("screen", "fast") in tiers
    assert ("final", "deep") in tiers


async def test_evaluation_calls_run_at_temperature_zero(deps: FakeDeps, seeded_job) -> None:
    await run_job(seeded_job.id, deps)
    by_stage: dict[str, set] = {}
    for c in deps.gateway.calls:
        by_stage.setdefault(c["stage"], set()).add(c["temperature"])
    assert by_stage["screen"] == {0.0}
    assert by_stage["final"] == {0.0}
    assert by_stage["evolve"] == {None}  # generation stays creative


async def test_round_ledger_is_written_per_round(deps: FakeDeps, seeded_job) -> None:
    await run_job(seeded_job.id, deps)
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert [r.round for r in ledger] == list(range(1, len(ledger) + 1))
    assert ledger[0].outcome == "win"  # 5.3 clears the 1.0pp floor
    champion = (
        await deps.db.execute(
            select(CandidateRow).where(
                CandidateRow.run_id == seeded_job.run_id, CandidateRow.status == "champion"
            )
        )
    ).scalar_one()
    assert champion.id == ledger[0].candidate_id
    assert champion.persona_evidence["screen"]["reactions"]


# --- win-stay / lose-shift at the pipeline level -----------------------------


async def test_win_stays_then_loss_shifts_and_forbids_tried_mechanisms(
    deps: FakeDeps,
    seeded_job,
) -> None:
    deps.gateway.responses["evolve"] = [
        idea_json("invoice_delivery", 1),
        idea_json("invoice_delivery", 2),
        idea_json("review_requests", 3),
    ]
    deps.gateway.responses["screen"] = [
        reactions_json(FIRST_WIN),  # r1 win → stay
        reactions_json(LOSE),  # r2 lose at patience 1 → shift
        reactions_json(LOSE),
    ]
    await run_job(seeded_job.id, deps)
    prompts = deps.gateway.prompts_for("evolve")
    assert "Mode: REFINE" in prompts[1]  # after the win: stay
    assert "Mode: SHIFT" in prompts[2]  # after the loss: shift
    assert "invoice_delivery" in prompts[2]  # tried mechanism is forbidden


async def test_keep_delta_rejects_a_small_improvement(deps: FakeDeps, seeded_job) -> None:
    deps.gateway.responses["screen"] = [
        reactions_json(BETTER),  # r1 win: 2.40pp
        reactions_json(NEAR_MISS),  # r2: 2.68pp, +0.28 under the 0.5 delta → lose
        reactions_json(LOSE),
    ]
    await run_job(seeded_job.id, deps)
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].outcome == "win"
    assert ledger[1].outcome == "lose"
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    champion = await deps.db.get(CandidateRow, winner.candidate_id)
    assert champion is not None and champion.round == 1  # the near-miss never dethroned


async def test_patience_two_gives_a_mechanism_a_second_try(deps: FakeDeps, seeded_job) -> None:
    run = await deps.db.get(RunRow, seeded_job.run_id)
    run.loop_config = {"PATIENCE": 2, "MAX_NO_IMPROVE": 1}
    await deps.db.commit()
    deps.gateway.responses["screen"] = [reactions_json(LOSE)]
    await run_job(seeded_job.id, deps)
    prompts = deps.gateway.prompts_for("evolve")
    assert len(prompts) == 2  # two tries on one mechanism, then dry → stop
    assert all("Mode: REFINE" in p for p in prompts)  # never shifted
    assert await run_status(deps.db, seeded_job.run_id) == "no_action"


# --- stops in isolation -------------------------------------------------------


async def test_stop_win_threshold(deps: FakeDeps, seeded_job) -> None:
    run = await deps.db.get(RunRow, seeded_job.run_id)
    run.loop_config = {"WIN_THRESHOLD_PP": 3.0}
    await deps.db.commit()
    await run_job(seeded_job.id, deps)  # 5.3 → 3.22pp > 3.0 → stop after round 1
    assert deps.gateway.calls_for("evolve") == 1
    assert await run_status(deps.db, seeded_job.run_id) == "complete"


async def test_stop_no_improve_exhausted(deps: FakeDeps, seeded_job) -> None:
    deps.gateway.responses["screen"] = [reactions_json(LOSE)]
    await run_job(seeded_job.id, deps)
    assert deps.gateway.calls_for("evolve") == 3  # MAX_NO_IMPROVE dry mechanisms
    assert await run_status(deps.db, seeded_job.run_id) == "no_action"
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "no_action"


async def test_stop_round_cap(deps: FakeDeps, seeded_job) -> None:
    run = await deps.db.get(RunRow, seeded_job.run_id)
    run.loop_config = {"MAX_ROUNDS": 4, "MAX_NO_IMPROVE": 99}
    await deps.db.commit()
    deps.gateway.responses["screen"] = [reactions_json(LOSE)]
    await run_job(seeded_job.id, deps)
    assert deps.gateway.calls_for("evolve") == 4
    assert len(await rounds(deps.db, seeded_job.run_id)) == 4


async def test_run_loop_config_snapshot_beats_fleet_defaults(
    deps: FakeDeps,
    seeded_job,
) -> None:
    fleet = await deps.db.get(FleetControlRow, 1)
    fleet.loop_defaults = {"MAX_ROUNDS": 10}
    run = await deps.db.get(RunRow, seeded_job.run_id)
    run.loop_config = {"MAX_ROUNDS": 2, "MAX_NO_IMPROVE": 99}
    await deps.db.commit()
    deps.gateway.responses["screen"] = [reactions_json(LOSE)]
    await run_job(seeded_job.id, deps)
    assert deps.gateway.calls_for("evolve") == 2


# --- suppression and honest failures -----------------------------------------


async def test_suppressed_round_spends_nothing_on_personas(deps: FakeDeps, seeded_job) -> None:
    deps.gateway.responses["critics"] = [CRITIC_BLOCK, CRITIC_OK]
    deps.gateway.responses["screen"] = [reactions_json(LOSE)]
    await run_job(seeded_job.id, deps)
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].outcome == "suppressed"
    # One suppressed round + lose rounds: screens = rounds − suppressed.
    assert deps.gateway.calls_for("screen") == len(ledger) - 1
    suppressed = (
        (
            await deps.db.execute(
                select(CandidateRow).where(
                    CandidateRow.run_id == seeded_job.run_id, CandidateRow.status == "suppressed"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(suppressed) == 1


async def test_generation_failure_fails_the_run_honestly(deps: FakeDeps, seeded_job) -> None:
    deps.gateway.fail_stage("evolve")
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "failed"
    assert await candidate_count(deps.db, seeded_job.run_id) == 0


async def test_critic_failure_fails_closed(deps: FakeDeps, seeded_job) -> None:
    deps.gateway.fail_stage("critics")
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "failed"


async def test_malformed_reactions_are_unavailable_not_a_crash(
    deps: FakeDeps,
    seeded_job,
) -> None:
    deps.gateway.responses["screen"] = "not json at all"
    deps.gateway.responses["final"] = "not json at all"
    await run_job(seeded_job.id, deps)  # must not raise
    assert await run_status(deps.db, seeded_job.run_id) == "no_action"
    assert {r.outcome for r in await rounds(deps.db, seeded_job.run_id)} == {"unavailable"}


async def test_flat_reactions_resolve_to_no_action(deps: FakeDeps, seeded_job) -> None:
    flat = reactions_json(deps.calibration.pivot)
    deps.gateway.responses["screen"] = flat
    deps.gateway.responses["final"] = flat
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "no_action"


async def test_unmatchable_pro_abstains_with_low_panel_fit(deps: FakeDeps, seeded_job) -> None:
    solo = [p for p in PERSONAS if p.family == "solo_operators"]

    async def _solo(segment: str):
        return solo

    deps.get_personas = _solo
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "abstained"
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "abstained"
    assert "panel" in winner.rationale


# --- safety rails -------------------------------------------------------------


async def test_kill_switch_stops_before_any_paid_call(deps: FakeDeps, seeded_job) -> None:
    await set_kill(deps.db, True)
    await deps.db.commit()
    await run_job(seeded_job.id, deps)
    run = await deps.db.get(RunRow, seeded_job.run_id)
    assert run.status == "stopped"
    assert run.stop_reason == "fleet_killed"
    assert deps.gateway.call_count == 0


async def test_budget_exhaustion_is_an_honest_stop(deps: FakeDeps, seeded_job) -> None:
    run = await deps.db.get(RunRow, seeded_job.run_id)
    run.cost_limit = Decimal("0.00")
    await deps.db.commit()
    await run_job(seeded_job.id, deps)
    refreshed = await deps.db.get(RunRow, seeded_job.run_id)
    await deps.db.refresh(refreshed)
    assert refreshed.status == "stopped"
    assert refreshed.stop_reason == "budget_exhausted"


async def test_context_outage_waits_for_retry(deps: FakeDeps, seeded_job) -> None:
    deps.context.unavailable = True
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "waiting"


async def test_lost_lease_stops_paid_work_immediately(deps: FakeDeps, seeded_job) -> None:
    async with deps.db.begin_nested():
        claimed = await claim_job(deps.db, "worker-other")
        assert claimed is not None and claimed.id == seeded_job.id
    await deps.db.commit()
    deps.worker_id = "worker-loser"
    await run_job(seeded_job.id, deps)
    assert deps.gateway.call_count == 0
    assert await candidate_count(deps.db, seeded_job.run_id) == 0


async def test_owner_heartbeats_keep_the_lease_alive(deps: FakeDeps, seeded_job) -> None:
    job = await claim_job(deps.db, "worker-owner", lease_seconds=60)
    assert job is not None
    await deps.db.commit()
    deps.worker_id = "worker-owner"
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "complete"


async def test_operator_kill_mid_run_keeps_its_honest_reason(
    deps: FakeDeps,
    seeded_job,
) -> None:
    original = deps.store.complete_stage

    async def stop_after_context(job_id: str, stage: str, payload=None) -> None:
        await original(job_id, stage, payload)
        if stage == "context":
            run = await deps.db.get(RunRow, seeded_job.run_id)
            run.status = "stopped"
            run.stop_reason = "operator_kill"
            await deps.db.commit()

    deps.store.complete_stage = stop_after_context  # type: ignore[method-assign]
    await run_job(seeded_job.id, deps)
    run = await deps.db.get(RunRow, seeded_job.run_id)
    await deps.db.refresh(run)
    assert run.status == "stopped"
    assert run.stop_reason == "operator_kill"  # never relabeled budget_exhausted
    assert deps.gateway.calls_for("screen") == 0


async def test_legacy_recommend_job_fails_honestly(deps: FakeDeps, db_session) -> None:
    run = RunRow(
        id="run-legacy",
        pro_ids=["pro_1"],
        audience_query="q",
        audience_run="r",
        channels=["sms"],
        cost_limit=Decimal("100.00"),
    )
    db_session.add(run)
    db_session.add(FleetControlRow(id=1, day_cost_limit=Decimal("1000.00")))
    await db_session.flush()
    job_id = await enqueue(db_session, run.id, stage="recommend")
    await db_session.commit()
    await run_job(job_id, deps)
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.stop_reason == "superseded_deploy"
    assert deps.gateway.call_count == 0


# --- per-Pro jobs and finalization ---------------------------------------------


async def test_two_pro_run_with_one_failed_job_degrades(
    deps: FakeDeps,
    db_session,
) -> None:
    run_id, jobs = await seed_two_pro_job(db_session)
    await run_job(jobs["pro_1"], deps)  # pro_1 wins
    deps.gateway.fail_stage("evolve")
    await run_job(jobs["pro_2"], deps)  # pro_2's model dies → job failed
    assert await run_status(db_session, run_id) == "degraded"
    run = await db_session.get(RunRow, run_id)
    await db_session.refresh(run)
    assert "1 of 2" in (run.stop_reason or "")


async def test_two_pro_run_where_both_decide_completes(deps: FakeDeps, db_session) -> None:
    # pro_2 abstains at panel fit (fixture is single-family for it? no — the
    # shared pool matches both). Both pros run the loop and decide.
    run_id, jobs = await seed_two_pro_job(db_session)
    await run_job(jobs["pro_1"], deps)
    assert await run_status(db_session, run_id) != "complete"  # sibling still queued
    await run_job(jobs["pro_2"], deps)
    assert await run_status(db_session, run_id) == "complete"


async def test_finalize_run_is_idempotent(deps: FakeDeps, seeded_job) -> None:
    await run_job(seeded_job.id, deps)
    assert await finalize_run(deps.db, seeded_job.run_id) is None  # already terminal
    assert await run_status(deps.db, seeded_job.run_id) == "complete"


async def test_missing_context_pro_abstains_and_run_degrades(
    deps: FakeDeps,
    db_session: AsyncSession,
) -> None:
    run = RunRow(
        id="run-ghost",
        pro_ids=["pro_1", "pro_ghost"],
        audience_query="q",
        audience_run="r",
        channels=["sms"],
        cost_limit=Decimal("100.00"),
    )
    db_session.add(run)
    db_session.add(FleetControlRow(id=1, day_cost_limit=Decimal("1000.00")))
    await db_session.flush()
    job_1 = await enqueue(db_session, run.id, stage="pro", pro_id="pro_1")
    job_ghost = await enqueue(db_session, run.id, stage="pro", pro_id="pro_ghost")
    await db_session.commit()
    await run_job(job_1, deps)
    await run_job(job_ghost, deps)
    assert await run_status(db_session, run.id) == "degraded"
    winners = {
        w.pro_id: w
        for w in (
            await db_session.execute(select(WinnerRow).where(WinnerRow.run_id == run.id))
        ).scalars()
    }
    assert winners["pro_1"].kind == "winner"
    assert winners["pro_ghost"].kind == "abstained"
    assert "context" in winners["pro_ghost"].rationale


# --- measurement ---------------------------------------------------------------


async def test_measurement_call_is_recorded_and_keyed(deps: FakeDeps, seeded_job) -> None:
    await run_job(seeded_job.id, deps)
    key = f"{seeded_job.run_id}:{seeded_job.pro_id}:measure"
    row = (await deps.db.execute(select(LlmCallRow).where(LlmCallRow.call_key == key))).scalar_one()
    assert row.status == "reconciled"
    calls_before = deps.gateway.calls_for("measure")
    await run_job(seeded_job.id, deps)  # terminal → no-op, no second measure call
    assert deps.gateway.calls_for("measure") == calls_before


async def test_unmeasurable_winner_abstains(deps: FakeDeps, seeded_job) -> None:
    async def unmeasurable(winner, llm, catalog):
        raise UnmeasurableWinner("unknown metric: imaginary")

    deps.create_plan = unmeasurable
    await run_job(seeded_job.id, deps)
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "abstained"
    assert "unmeasurable" in winner.rationale


async def test_transient_measure_failure_retries_instead_of_abstaining(
    deps: FakeDeps,
    seeded_job,
) -> None:
    async def flaky(winner, llm, catalog):
        raise RateLimitExhausted("429 storm")

    deps.create_plan = flaky
    with pytest.raises(RateLimitExhausted):
        await run_job(seeded_job.id, deps)
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "winner"  # a validated winner survives a 429 storm
    assert await run_status(deps.db, seeded_job.run_id) not in ("abstained", "failed")
