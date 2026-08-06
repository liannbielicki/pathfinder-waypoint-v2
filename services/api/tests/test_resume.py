import pytest
from sqlalchemy import select

from waypoint.pipeline import run_job
from waypoint.tables import JobRow, WinnerRow

from .conftest import FakeDeps, InjectedCrash
from .test_pipeline import candidate_count, run_status


async def test_resume_skips_completed_paid_stages(deps: FakeDeps, seeded_job) -> None:
    deps.fail_after("screen")
    with pytest.raises(InjectedCrash):
        await run_job(seeded_job.id, deps)
    calls_before = deps.llm.call_count
    deps.clear_failure()
    await run_job(seeded_job.id, deps)
    assert deps.llm.calls_for("generate") == 1
    assert deps.llm.call_count > calls_before
    assert await run_status(deps.db, seeded_job.run_id) == "complete"


async def test_resume_does_not_duplicate_candidates_or_winners(
    deps: FakeDeps, seeded_job,
) -> None:
    deps.fail_after("score")
    with pytest.raises(InjectedCrash):
        await run_job(seeded_job.id, deps)
    deps.clear_failure()
    await run_job(seeded_job.id, deps)
    assert await candidate_count(deps.db, seeded_job.run_id) == 3
    winners = (await deps.db.execute(
        select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id)
    )).scalars().all()
    assert len(winners) == 1


async def test_checkpoints_are_durable_across_the_crash(deps: FakeDeps, seeded_job) -> None:
    deps.fail_after("screen")
    with pytest.raises(InjectedCrash):
        await run_job(seeded_job.id, deps)
    job = (await deps.db.execute(
        select(JobRow).where(JobRow.id == seeded_job.id)
    )).scalar_one()
    for stage in ("context", "generate", "critics", "screen"):
        assert stage in job.checkpoint
    assert "final" not in job.checkpoint


async def test_second_full_run_is_a_no_op(deps: FakeDeps, seeded_job) -> None:
    await run_job(seeded_job.id, deps)
    calls = deps.llm.call_count
    await run_job(seeded_job.id, deps)
    assert deps.llm.call_count == calls
    assert await candidate_count(deps.db, seeded_job.run_id) == 3
