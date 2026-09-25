import re
from decimal import Decimal

import pytest
from sqlalchemy import select

from waypoint import pipeline
from waypoint.loop import DEFAULT_LOOP_CONFIG, replay
from waypoint.pipeline import run_job
from waypoint.tables import JobRow, LlmCallRow, RunRow, WinnerRow

from .conftest import FakeDeps, InjectedCrash, idea_json, reactions_json
from .test_pipeline import (
    FIRST_WIN,
    GREAT,
    LOSE,
    candidate_count,
    rounds,
    run_status,
    set_loop_config,
)


async def test_resume_skips_completed_paid_stages(deps: FakeDeps, seeded_job) -> None:
    deps.fail_after("evolve")
    with pytest.raises(InjectedCrash):
        await run_job(seeded_job.id, deps)
    evolve_calls = deps.gateway.calls_for("evolve")
    deps.clear_failure()
    await run_job(seeded_job.id, deps)
    assert deps.gateway.calls_for("evolve") == evolve_calls  # nothing re-paid
    assert deps.gateway.calls_for("final") == 1
    assert await run_status(deps.db, seeded_job.run_id) == "complete"


async def test_resume_does_not_duplicate_candidates_winners_or_rounds(
    deps: FakeDeps,
    seeded_job,
) -> None:
    deps.fail_after("score")
    with pytest.raises(InjectedCrash):
        await run_job(seeded_job.id, deps)
    candidates_before = await candidate_count(deps.db, seeded_job.run_id)
    rounds_before = len(await rounds(deps.db, seeded_job.run_id))
    deps.clear_failure()
    await run_job(seeded_job.id, deps)
    assert await candidate_count(deps.db, seeded_job.run_id) == candidates_before
    assert len(await rounds(deps.db, seeded_job.run_id)) == rounds_before
    winners = (
        (await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id)))
        .scalars()
        .all()
    )
    assert len(winners) == 1


async def test_checkpoints_are_durable_across_the_crash(deps: FakeDeps, seeded_job) -> None:
    deps.fail_after("evolve")
    with pytest.raises(InjectedCrash):
        await run_job(seeded_job.id, deps)
    job = (
        await deps.db.execute(
            select(JobRow)
            .where(JobRow.id == seeded_job.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    for stage in ("context", "evolve"):
        assert stage in job.checkpoint
    assert "final" not in job.checkpoint


async def test_second_full_run_is_a_no_op(deps: FakeDeps, seeded_job) -> None:
    await run_job(seeded_job.id, deps)
    calls = deps.gateway.call_count
    await run_job(seeded_job.id, deps)
    assert deps.gateway.call_count == calls


async def test_mid_loop_crash_replays_the_ledger_without_re_paying(
    deps: FakeDeps,
    seeded_job,
) -> None:
    """Crash inside round 2 at the screen call. Resume must not re-pay round 1,
    must reuse round 2's committed generate/critic responses, and must land on
    the same loop state."""
    await set_loop_config(deps, seeded_job.run_id, CANDIDATE_COUNT=1, MAX_ROUNDS=2)
    deps.gateway.responses["evolve"] = [
        idea_json("invoice_delivery", 1),
        idea_json("invoice_delivery", 2),
    ]
    deps.gateway.responses["screen"] = [
        reactions_json(FIRST_WIN),
        RuntimeError("provider died mid-round"),
        reactions_json(GREAT),
        reactions_json(LOSE),
    ]
    await run_job(seeded_job.id, deps)  # provider dies mid-round 2: honest requeue
    job = await deps.db.get(JobRow, seeded_job.id)
    await deps.db.refresh(job)
    assert job.status == "queued"
    assert "unhandled at evolve" in job.checkpoint["failure"]["reason"]
    assert len(await rounds(deps.db, seeded_job.run_id)) == 1  # round 2 never completed
    generate_calls_at_crash = deps.gateway.calls_for("evolve")
    assert generate_calls_at_crash == 2

    await run_job(seeded_job.id, deps)
    ledger = await rounds(deps.db, seeded_job.run_id)
    # Round 2's generate/critic short-circuited from the recorded calls:
    total_rounds = len(ledger)
    assert deps.gateway.calls_for("evolve") == total_rounds
    assert [r.round for r in ledger] == list(range(1, total_rounds + 1))
    assert ledger[1].outcome == "win"  # GREAT (4.87pp) beats FIRST_WIN (1.19pp) + delta
    assert await run_status(deps.db, seeded_job.run_id) == "complete"


async def test_abandoned_call_reservation_converts_to_spend_on_resume(
    deps: FakeDeps,
    seeded_job,
) -> None:
    deps.gateway.responses["screen"] = [
        RuntimeError("provider died before responding"),
        reactions_json(FIRST_WIN),
        reactions_json(LOSE),
    ]
    await run_job(seeded_job.id, deps)  # provider dies before responding: honest requeue
    pending = (
        (await deps.db.execute(select(LlmCallRow).where(LlmCallRow.status == "pending")))
        .scalars()
        .all()
    )
    assert len(pending) == 1  # the dead screen call

    await run_job(seeded_job.id, deps)
    run = await deps.db.get(RunRow, seeded_job.run_id)
    await deps.db.refresh(run)
    # The abandoned worst-case reservation became honest recorded spend.
    assert run.cost_spent > Decimal(0)
    statuses = {
        row.call_key: row.status
        for row in (
            await deps.db.execute(select(LlmCallRow).execution_options(populate_existing=True))
        ).scalars()
    }
    assert "abandoned" not in statuses.values() or all(
        status in ("reconciled", "abandoned") for status in statuses.values()
    )
    # The re-primed screen call finished its lifecycle.
    screen_key = f"{seeded_job.run_id}:{seeded_job.pro_id}:r1:screen:c1"
    assert statuses[screen_key] == "reconciled"
    assert await run_status(deps.db, seeded_job.run_id) == "complete"


async def test_replayed_state_matches_the_ledger_counters(deps: FakeDeps, seeded_job) -> None:
    deps.fail_after("evolve")
    deps.gateway.responses["screen"] = [
        reactions_json(FIRST_WIN),
        reactions_json(LOSE),
    ]
    with pytest.raises(InjectedCrash):
        await run_job(seeded_job.id, deps)
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].outcome == "win"
    assert all(r.outcome == "lose" for r in ledger[1:])
    state = replay(ledger, DEFAULT_LOOP_CONFIG)
    assert state.best_score == pytest.approx(float(ledger[0].score_pp), abs=1e-3)


async def test_replayed_state_carries_the_champion_reaction_from_the_ledger(
    deps: FakeDeps, seeded_job
) -> None:
    """End-to-end replay parity for the win rule's primary input. `evolve_rounds`
    has no mean_reaction column and gets no migration, so the live round writes
    it into the ranking JSON and replay reads it back — otherwise a resumed run
    would judge its next challenger on the pp fallback while the original judged
    it on the reaction lattice."""
    deps.gateway.responses["screen"] = [
        reactions_json(FIRST_WIN),
        reactions_json(LOSE),
    ]
    await run_job(seeded_job.id, deps)
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].outcome == "win"
    assert ledger[0].ranking["mean_reaction"] == pytest.approx(FIRST_WIN)
    assert replay(ledger, DEFAULT_LOOP_CONFIG).best_reaction == pytest.approx(FIRST_WIN)


async def test_plan_is_loaded_not_recomputed_on_resume(seeded_job, deps) -> None:
    deps.contact_plan_mode = "enforce"
    org = deps.context.batch.organizations[0]
    deps.context.batch = deps.context.batch.model_copy(update={"organizations": [
        org.model_copy(update={"contact_candidates": [
            {"pro_uuid": "pro_1", "has_phone": True, "pct_call": 0.9,
             "reco_scoring_date": "2099-01-01"}]})]})
    run = await deps.db.get(RunRow, seeded_job.run_id)
    run.channels = ["call"]
    await deps.db.commit()
    deps.fail_after("plan")
    with pytest.raises(InjectedCrash):
        await run_job(seeded_job.id, deps)
    deps.clear_failure()
    # Different candidates on resume must not change the pinned plan (a
    # recompute here would abstain: the run's Pro is no longer a candidate).
    deps.context.batch = deps.context.batch.model_copy(update={"organizations": [
        org.model_copy(update={"contact_candidates": [
            {"pro_uuid": "pro_new", "has_phone": True, "pct_call": 0.99,
             "reco_scoring_date": "2099-01-01"}]})]})
    await run_job(seeded_job.id, deps)
    job = await deps.db.get(JobRow, seeded_job.id)
    await deps.db.refresh(job)
    assert job.checkpoint["plan"]["plan"]["pro_uuid"] == "pro_1"
    winner = await deps.store.winner_for(seeded_job.run_id, seeded_job.pro_id)
    assert winner is None or not (winner.rationale or "").startswith("contact_plan:")


async def test_shadow_plan_is_not_enforced_after_a_mode_flip(
    seeded_job, deps, monkeypatch
) -> None:
    deps.contact_plan_mode = "shadow"
    org = deps.context.batch.organizations[0]
    deps.context.batch = deps.context.batch.model_copy(update={"organizations": [
        org.model_copy(update={"contact_candidates": [
            {"pro_uuid": "pro_1", "has_phone": True, "pct_call": 0.9,
             "reco_scoring_date": "2099-01-01"}]})]})
    run = await deps.db.get(RunRow, seeded_job.run_id)
    run.channels = ["call"]
    await deps.db.commit()
    deps.fail_after("plan")
    with pytest.raises(InjectedCrash):
        await run_job(seeded_job.id, deps)
    deps.clear_failure()
    # The rollout flips shadow -> enforce while this job is past "plan".
    deps.contact_plan_mode = "enforce"
    seen: list[object] = []
    evolve = pipeline.STAGE_HANDLERS["evolve"]

    async def spy(state, stage_deps):
        seen.append(pipeline._enforced(state, stage_deps))
        return await evolve(state, stage_deps)

    monkeypatch.setitem(pipeline.STAGE_HANDLERS, "evolve", spy)
    await run_job(seeded_job.id, deps)
    assert seen == [None]  # the shadow plan loaded, but is never enforced
    assert deps.gateway.call_count > 0


async def test_resumed_enforce_job_never_leaks_the_reco_hint(seeded_job, deps) -> None:
    # The restored brief is rebuilt from context on resume; the hint must be
    # stripped again there, not only in the plan stage that already ran.
    deps.contact_plan_mode = "enforce"
    org = deps.context.batch.organizations[0]
    curated = {**(org.curated_context or {}), "v": {"suggested_channel": "sms"}}
    deps.context.batch = deps.context.batch.model_copy(update={"organizations": [
        org.model_copy(update={
            "suggested_channel": "sms",
            "curated_context": curated,
            "contact_candidates": [
                {"pro_uuid": "pro_1", "has_phone": True, "pct_call": 0.9,
                 "reco_scoring_date": "2099-01-01"}],
        })]})
    run = await deps.db.get(RunRow, seeded_job.run_id)
    run.channels = ["call"]
    await deps.db.commit()
    deps.fail_after("plan")
    with pytest.raises(InjectedCrash):
        await run_job(seeded_job.id, deps)
    deps.clear_failure()
    await run_job(seeded_job.id, deps)
    assert deps.gateway.prompts
    assert not any(re.search(r'"suggested_channel":\s*"', p) for p in deps.gateway.prompts)
