"""Strict ranker contract (unit) + the batched evolve round it drives (pipeline)."""

import json
import math
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from waypoint.models import RankedCandidate, RankerDecision, validate_ranking
from waypoint.pipeline import MAX_BATCH_REFILLS, run_job
from waypoint.tables import CandidateRow, LlmCallRow, RunRow, TouchOutcomeRow

from .conftest import (
    DEFAULT_MECHANISMS,
    RANK_OK,
    FakeDeps,
    batch_json,
    critics_json,
    rank_json,
    reactions_json,
)
from .test_pipeline import GOOD, GREAT, LOSE, candidate_count, rounds, run_status, set_loop_config


def decision(*pairs: tuple[str, int, float], tie: bool = False) -> RankerDecision:
    return RankerDecision(
        ranking=[
            RankedCandidate(candidate_id=cid, rank=rank, score=score) for cid, rank, score in pairs
        ],
        tie=tie,
    )


# --- validate_ranking --------------------------------------------------------


def test_validate_ranking_accepts_a_valid_permutation() -> None:
    d = decision(("c1", 1, 0.9), ("c2", 2, 0.4), ("c3", 3, 0.1))
    assert validate_ranking(d, ["c1", "c2", "c3"]) is d


def test_validate_ranking_rejects_unknown_id() -> None:
    d = decision(("c1", 1, 0.9), ("cX", 2, 0.4))
    with pytest.raises(ValueError):
        validate_ranking(d, ["c1", "c2"])


def test_validate_ranking_rejects_missing_candidate() -> None:
    d = decision(("c1", 1, 0.9))
    with pytest.raises(ValueError):
        validate_ranking(d, ["c1", "c2"])


def test_validate_ranking_rejects_duplicate_id() -> None:
    d = decision(("c1", 1, 0.9), ("c1", 2, 0.4))
    with pytest.raises(ValueError):
        validate_ranking(d, ["c1", "c2"])


def test_validate_ranking_rejects_duplicate_rank() -> None:
    d = decision(("c1", 1, 0.9), ("c2", 1, 0.4))
    with pytest.raises(ValueError):
        validate_ranking(d, ["c1", "c2"])


def test_validate_ranking_rejects_gapped_rank() -> None:
    d = decision(("c1", 1, 0.9), ("c2", 3, 0.4))
    with pytest.raises(ValueError):
        validate_ranking(d, ["c1", "c2"])


# --- RankerDecision -----------------------------------------------------------


def test_ranker_decision_requires_explicit_tie() -> None:
    with pytest.raises(ValidationError):
        RankerDecision(ranking=[RankedCandidate(candidate_id="c1", rank=1, score=0.5)])


def test_ranker_decision_by_rank_sorts_ascending() -> None:
    d = decision(("c2", 2, 0.4), ("c1", 1, 0.9))
    assert [r.candidate_id for r in d.by_rank()] == ["c1", "c2"]


# --- RankedCandidate ----------------------------------------------------------


@pytest.mark.parametrize("score", [-0.01, 1.01, math.nan])
def test_ranked_candidate_rejects_out_of_bounds_score(score: float) -> None:
    with pytest.raises(ValidationError):
        RankedCandidate(candidate_id="c1", rank=1, score=score)


def test_ranked_candidate_rejects_empty_id() -> None:
    with pytest.raises(ValidationError):
        RankedCandidate(candidate_id="", rank=1, score=0.5)


def test_ranked_candidate_rejects_rank_below_one() -> None:
    with pytest.raises(ValidationError):
        RankedCandidate(candidate_id="c1", rank=0, score=0.5)


# --- batched evolve round -----------------------------------------------------


async def one_round(deps: FakeDeps, run_id: str, **overrides) -> None:
    """Pin the loop to a single round so a test reads one round's decision."""
    await set_loop_config(deps, run_id, MAX_ROUNDS=1, **overrides)


async def test_default_round_generates_criticizes_ranks_and_screens_once(
    deps: FakeDeps, seeded_job
) -> None:
    await one_round(deps, seeded_job.run_id)
    await run_job(seeded_job.id, deps)
    assert deps.gateway.calls_for("evolve") == 1  # ONE batched generation call
    assert deps.gateway.calls_for("critics") == 1  # ONE batched critic call
    assert deps.gateway.calls_for("rank") == 1
    assert deps.gateway.calls_for("screen") == 1  # clear winner: only rank 1
    assert await candidate_count(deps.db, seeded_job.run_id) == 3  # one row per idea
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].outcome == "win"
    assert ledger[0].ranking["selection_reason"] == "clear_winner"
    assert await run_status(deps.db, seeded_job.run_id) == "complete"


async def test_single_candidate_round_skips_the_ranker(deps: FakeDeps, seeded_job) -> None:
    await one_round(deps, seeded_job.run_id, CANDIDATE_COUNT=1)
    await run_job(seeded_job.id, deps)
    assert deps.gateway.calls_for("rank") == 0  # nothing to rank
    assert "exactly 1 new idea" in deps.gateway.prompts_for("evolve")[0]
    assert await candidate_count(deps.db, seeded_job.run_id) == 1
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].ranking["selection_reason"] == "single_rankable_candidate"
    assert ledger[0].ranking["ranker_model"] == "skipped"


@pytest.mark.parametrize("count", [2, 5])
async def test_other_candidate_counts_flow_through(deps: FakeDeps, seeded_job, count: int) -> None:
    mechanisms = [f"mechanism_{i}" for i in range(count)]
    deps.gateway.responses["evolve"] = [batch_json(mechanisms)]
    deps.gateway.responses["critics"] = critics_json(["none"] * count)
    deps.gateway.responses["rank"] = rank_json(
        *[(f"c{i + 1}", 0.9 - 0.2 * i) for i in range(count)]
    )
    await one_round(deps, seeded_job.run_id, CANDIDATE_COUNT=count)
    await run_job(seeded_job.id, deps)
    assert f"exactly {count} new ideas" in deps.gateway.prompts_for("evolve")[0]
    assert deps.gateway.calls_for("evolve") == 1  # the batch arrived whole: no refill
    assert await candidate_count(deps.db, seeded_job.run_id) == count
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert len(ledger[0].ranking["order"]) == count


# --- batch shape: dedupe, refill, malformed items ------------------------------


async def test_duplicate_mechanisms_are_deduped_and_refilled(deps: FakeDeps, seeded_job) -> None:
    deps.gateway.responses["evolve"] = [
        batch_json(["invoice_delivery", "invoice_delivery", "invoice_delivery"]),
        batch_json(["review_requests", "review_requests"]),
        batch_json(["feature_adoption"]),
    ]
    await one_round(deps, seeded_job.run_id)
    await run_job(seeded_job.id, deps)
    assert deps.gateway.calls_for("evolve") == 3  # generate + two bounded refills
    mechanisms = {
        c.recommendation["mechanism"]
        for c in (
            await deps.db.execute(
                select(CandidateRow).where(CandidateRow.run_id == seeded_job.run_id)
            )
        ).scalars()
    }
    assert mechanisms == {"invoice_delivery", "review_requests", "feature_adoption"}
    refill_prompt = deps.gateway.prompts_for("evolve")[1]
    assert "Mode: SHIFT" in refill_prompt
    assert "invoice_delivery" in refill_prompt  # already held → forbidden
    assert "exactly 2 new ideas" in refill_prompt  # only the missing count


async def test_refill_stops_at_the_bound_and_the_round_proceeds(
    deps: FakeDeps, seeded_job
) -> None:
    deps.gateway.responses["evolve"] = [batch_json(["invoice_delivery"] * 3)]
    await one_round(deps, seeded_job.run_id)
    await run_job(seeded_job.id, deps)
    assert deps.gateway.calls_for("evolve") == 1 + MAX_BATCH_REFILLS  # bounded, then proceed
    assert await candidate_count(deps.db, seeded_job.run_id) == 1
    assert deps.gateway.calls_for("rank") == 0  # one rankable candidate
    assert (await rounds(deps.db, seeded_job.run_id))[0].outcome == "win"


async def test_malformed_items_are_dropped_and_the_batch_is_refilled(
    deps: FakeDeps, seeded_job
) -> None:
    partly_bad = json.dumps(
        [json.loads(batch_json(["invoice_delivery"]))[0], {"title": "no mechanism"}, "garbage"]
    )
    deps.gateway.responses["evolve"] = [partly_bad, batch_json(["review_requests"]), "not json"]
    await one_round(deps, seeded_job.run_id)
    await run_job(seeded_job.id, deps)
    # The one valid idea survived; the refill added a second; the third refill's
    # unusable output is tolerated (bounded means bounded).
    assert await candidate_count(deps.db, seeded_job.run_id) == 2


# --- batch critic ---------------------------------------------------------------


async def test_one_critic_call_carries_every_non_pre_gated_idea(
    deps: FakeDeps, seeded_job
) -> None:
    deps.db.add(
        TouchOutcomeRow(
            recommendation_id="old-w",
            source="test",
            pro_id="pro_1",
            channel="sms",
            mechanism="invoice_delivery",
            journey_window="churn_risk",
            unsubscribed=True,
        )
    )
    await deps.db.commit()
    await one_round(deps, seeded_job.run_id)
    deps.gateway.responses["critics"] = critics_json(["none", "none", "none"])
    deps.gateway.responses["rank"] = rank_json(("c1", 0.9), ("c2", 0.4))
    await run_job(seeded_job.id, deps)
    assert deps.gateway.calls_for("critics") == 1
    prompt = deps.gateway.prompts_for("critics")[0]
    assert '"idea_index": 1' in prompt and '"idea_index": 2' in prompt
    assert '"idea_index": 0' not in prompt  # pre-gated: never reached the critic
    pre_gated = (
        await deps.db.execute(
            select(CandidateRow).where(
                CandidateRow.run_id == seeded_job.run_id, CandidateRow.status == "suppressed"
            )
        )
    ).scalar_one()
    assert pre_gated.critics["block_kind"] == "recently_failed"
    assert pre_gated.persona_evidence == {}  # zero persona spend on a blocked idea


async def test_blocked_ideas_are_suppressed_without_persona_spend(
    deps: FakeDeps, seeded_job
) -> None:
    deps.gateway.responses["critics"] = critics_json(["ungrounded", "none", "none"])
    deps.gateway.responses["rank"] = rank_json(("c1", 0.9), ("c2", 0.4))
    await one_round(deps, seeded_job.run_id)
    await run_job(seeded_job.id, deps)
    statuses = {
        c.recommendation["mechanism"]: c.status
        for c in (
            await deps.db.execute(
                select(CandidateRow).where(CandidateRow.run_id == seeded_job.run_id)
            )
        ).scalars()
    }
    assert statuses["invoice_delivery"] == "suppressed"
    assert deps.gateway.calls_for("screen") == 1  # only the ranked winner was screened
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].mechanism == "review_requests"  # the suppressed idea never competed


async def test_missing_verdicts_fail_closed_as_unreviewed(deps: FakeDeps, seeded_job) -> None:
    deps.gateway.responses["critics"] = critics_json(["none"])  # verdicts for idea 0 only
    await one_round(deps, seeded_job.run_id)
    await run_job(seeded_job.id, deps)
    unreviewed = [
        c
        for c in (
            await deps.db.execute(
                select(CandidateRow).where(CandidateRow.run_id == seeded_job.run_id)
            )
        ).scalars()
        if c.critics["block_kind"] == "unreviewed"
    ]
    assert len(unreviewed) == 2 and all(c.status == "suppressed" for c in unreviewed)
    assert deps.gateway.calls_for("rank") == 0  # one survivor left: nothing to rank


async def test_an_all_suppressed_batch_is_a_suppressed_round(deps: FakeDeps, seeded_job) -> None:
    deps.gateway.responses["critics"] = critics_json(["ungrounded"] * 3)
    await one_round(deps, seeded_job.run_id)
    await run_job(seeded_job.id, deps)
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].outcome == "suppressed"
    assert ledger[0].score_pp is None
    assert ledger[0].mechanism == DEFAULT_MECHANISMS[0]
    assert ledger[0].ranking["selection_reason"] == "all_candidates_suppressed"
    assert deps.gateway.calls_for("screen") == 0
    assert deps.gateway.calls_for("rank") == 0


# --- strict ranking -------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        rank_json(("c1", 0.9), ("cX", 0.5), ("c3", 0.2)),  # unknown token
        json.dumps(
            {
                "ranking": [
                    {"candidate_id": "c1", "rank": 1, "score": 0.9},
                    {"candidate_id": "c2", "rank": 1, "score": 0.5},
                    {"candidate_id": "c3", "rank": 3, "score": 0.2},
                ],
                "tie": False,
            }
        ),  # duplicate rank
        rank_json(("c1", 0.9), ("c2", 0.5)),  # missing candidate
    ],
)
async def test_invalid_rankings_are_re_asked_then_fail_the_round_honestly(
    deps: FakeDeps, seeded_job, bad: str
) -> None:
    deps.gateway.responses["rank"] = [bad]
    await one_round(deps, seeded_job.run_id)
    await run_job(seeded_job.id, deps)
    assert deps.gateway.calls_for("rank") == 3  # JSON_CALL_ATTEMPTS re-asks
    assert deps.gateway.calls_for("screen") == 0  # never screen an unranked candidate
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].outcome == "unavailable"
    assert ledger[0].ranking["selection_reason"] == "ranking_failed_champion_preserved"
    assert ledger[0].ranking["ranking_failure"]
    # A ranker that was PAID three times and failed must not read like one that
    # was never invoked.
    assert ledger[0].ranking["ranker_model"] == "model-fast"
    assert not (
        await deps.db.execute(
            select(CandidateRow).where(
                CandidateRow.run_id == seeded_job.run_id, CandidateRow.status == "champion"
            )
        )
    ).scalars().all()


async def test_a_ranking_failure_preserves_the_champion_and_a_later_round_wins(
    deps: FakeDeps, seeded_job
) -> None:
    bad = rank_json(("c1", 0.9), ("cX", 0.5), ("c3", 0.2))
    deps.gateway.responses["rank"] = [
        RANK_OK,  # round 1 ranks cleanly
        bad,
        bad,
        bad,  # round 2 never produces a valid ranking
        rank_json(("c2", 0.95), ("c1", 0.4), ("c3", 0.2)),  # round 3 promotes a new mechanism
    ]
    deps.gateway.responses["screen"] = [reactions_json(GOOD), reactions_json(GREAT)]
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=3, MAX_NO_IMPROVE=99)
    await run_job(seeded_job.id, deps)
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert [r.outcome for r in ledger] == ["win", "unavailable", "win"]
    assert ledger[1].candidate_id is not None  # the row still references a candidate
    champion = (
        await deps.db.execute(
            select(CandidateRow).where(
                CandidateRow.run_id == seeded_job.run_id, CandidateRow.status == "champion"
            )
        )
    ).scalar_one()
    assert champion.round == 3
    assert champion.recommendation["mechanism"] == DEFAULT_MECHANISMS[1]


async def test_the_ranker_call_is_deterministic(deps: FakeDeps, seeded_job) -> None:
    await one_round(deps, seeded_job.run_id)
    await run_job(seeded_job.id, deps)
    assert {c["temperature"] for c in deps.gateway.calls if c["stage"] == "rank"} == {0.0}


# --- tie screening ---------------------------------------------------------------


async def test_tied_finalists_are_both_screened_and_the_screen_breaks_the_tie(
    deps: FakeDeps, seeded_job
) -> None:
    deps.gateway.responses["rank"] = rank_json(
        ("c1", 0.90), ("c2", 0.88), ("c3", 0.20), tie=True, tie_reason="indistinguishable"
    )
    deps.gateway.responses["screen"] = [reactions_json(LOSE), reactions_json(GREAT)]
    await one_round(deps, seeded_job.run_id)
    await run_job(seeded_job.id, deps)
    assert deps.gateway.calls_for("screen") == 2  # the tie is decided by the panel
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].ranking["finalists"] == ["c1", "c2"]
    assert ledger[0].ranking["selection_reason"] == "tie_broken_by_screen_runner_up"
    assert ledger[0].mechanism == DEFAULT_MECHANISMS[1]  # rank 2 won the screen
    assert ledger[0].outcome == "win"


async def test_a_tie_the_top_candidate_still_wins_is_recorded_as_a_screened_tie(
    deps: FakeDeps, seeded_job
) -> None:
    deps.gateway.responses["rank"] = rank_json(("c1", 0.90), ("c2", 0.88), ("c3", 0.20), tie=True)
    deps.gateway.responses["screen"] = [reactions_json(GREAT), reactions_json(LOSE)]
    await one_round(deps, seeded_job.run_id)
    await run_job(seeded_job.id, deps)
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].ranking["selection_reason"] == "tie_within_margin_top_two_screened"
    assert ledger[0].mechanism == DEFAULT_MECHANISMS[0]


async def test_every_finalist_screen_failing_makes_the_round_unavailable(
    deps: FakeDeps, seeded_job
) -> None:
    deps.gateway.responses["screen"] = "not json at all"
    await one_round(deps, seeded_job.run_id)
    await run_job(seeded_job.id, deps)
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].outcome == "unavailable"
    assert ledger[0].score_pp is None
    assert "c1" in ledger[0].ranking["screen_failures"]


# --- ranking evidence ------------------------------------------------------------


async def test_ranking_evidence_records_the_whole_round_decision(
    deps: FakeDeps, seeded_job
) -> None:
    await one_round(deps, seeded_job.run_id)
    await run_job(seeded_job.id, deps)
    ranking = (await rounds(deps.db, seeded_job.run_id))[0].ranking
    assert [item["rank"] for item in ranking["order"]] == [1, 2, 3]
    assert [item["score"] for item in ranking["order"]] == [0.9, 0.5, 0.2]
    assert [item["mechanism"] for item in ranking["order"]] == DEFAULT_MECHANISMS
    assert ranking["tie"] is False
    assert ranking["tie_margin"] == 0.05
    assert ranking["finalists"] == ["c1"]
    assert ranking["ranker_model"] == "model-fast"
    ids = ranking["candidate_ids"]
    assert set(ids) == {"c1", "c2", "c3"}
    for token, candidate_id in ids.items():
        candidate = await deps.db.get(CandidateRow, candidate_id)
        assert candidate is not None and candidate.round == 1
        assert candidate.recommendation["mechanism"] == next(
            item["mechanism"] for item in ranking["order"] if item["token"] == token
        )


# --- round cost preflight ----------------------------------------------------------


async def test_a_round_that_cannot_fit_the_budget_spends_nothing(
    deps: FakeDeps, seeded_job
) -> None:
    run = await deps.db.get(RunRow, seeded_job.run_id)
    # Deliberately between the two bounds: the generation call's own worst case
    # (~$0.09) fits, so without the round preflight this run would pay for at
    # least one call; the round's worst case (~$1.2 — every JSON stage counted
    # at its full re-ask budget) does not.
    run.cost_limit = Decimal("0.20")
    await deps.db.commit()
    await run_job(seeded_job.id, deps)
    assert deps.gateway.call_count == 0  # the preflight stopped before any paid work
    await deps.db.refresh(run)
    assert run.status == "stopped"
    assert run.stop_reason == "budget_exhausted"
    assert run.cost_reserved == Decimal(0)  # the refused hold left nothing behind


async def test_the_preflight_hold_is_released_and_never_accumulates(
    deps: FakeDeps, seeded_job
) -> None:
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=3, MAX_NO_IMPROVE=99)
    await run_job(seeded_job.id, deps)
    run = await deps.db.get(RunRow, seeded_job.run_id)
    await deps.db.refresh(run)
    assert len(await rounds(deps.db, seeded_job.run_id)) == 3
    assert run.cost_spent > Decimal(0)  # the actual spend is recorded
    # Every preflight hold was released and every call reconciled: what is left
    # is sub-cent rounding dust from the numeric column, orders of magnitude
    # below the ~$1.2 a single round's preflight holds (after the
    # JSON_CALL_ATTEMPTS multiplier) — no accumulating hold.
    assert run.cost_reserved < Decimal("0.001")


# --- safety rails and replay ---------------------------------------------------------


async def test_kill_switch_mid_loop_stops_further_paid_calls(deps: FakeDeps, seeded_job) -> None:
    async def killed_once_a_round_landed() -> bool:
        return bool(await deps.store.rounds_for(seeded_job.run_id, seeded_job.pro_id))

    deps.queue.fleet_is_killed = killed_once_a_round_landed  # type: ignore[method-assign]
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=5, MAX_NO_IMPROVE=99)
    await run_job(seeded_job.id, deps)
    run = await deps.db.get(RunRow, seeded_job.run_id)
    await deps.db.refresh(run)
    assert run.status == "stopped"
    assert run.stop_reason == "fleet_killed"
    assert len(await rounds(deps.db, seeded_job.run_id)) == 1
    assert deps.gateway.calls_for("rank") == 1  # only round 1 was ever paid for
    assert deps.gateway.calls_for("evolve") == 1


async def test_resume_replays_committed_rounds_without_re_paying(
    deps: FakeDeps, seeded_job
) -> None:
    deps.fail_after("evolve")
    with pytest.raises(Exception, match="crashed after evolve"):
        await run_job(seeded_job.id, deps)
    before = {
        stage: deps.gateway.calls_for(stage) for stage in ("evolve", "critics", "rank", "screen")
    }
    candidates_before = await candidate_count(deps.db, seeded_job.run_id)
    rounds_before = len(await rounds(deps.db, seeded_job.run_id))
    assert before["rank"] > 0 and candidates_before == 3 * rounds_before

    deps.clear_failure()
    await run_job(seeded_job.id, deps)
    for stage, count in before.items():
        assert deps.gateway.calls_for(stage) == count  # replayed from recorded calls
    assert await candidate_count(deps.db, seeded_job.run_id) == candidates_before
    assert len(await rounds(deps.db, seeded_job.run_id)) == rounds_before
    recorded = (
        (
            await deps.db.execute(
                select(LlmCallRow).where(LlmCallRow.call_key.like(f"%:{seeded_job.pro_id}:r1:%"))
            )
        )
        .scalars()
        .all()
    )
    assert {row.status for row in recorded} == {"reconciled"}
