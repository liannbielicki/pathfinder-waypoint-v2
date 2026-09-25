import asyncio
import json
from decimal import Decimal
from functools import partial

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint import queue as queue_module
from waypoint.calls import BudgetExhausted
from waypoint.llm import RateLimitExhausted
from waypoint.models import PENDING_AUDIENCE_QUERY, Recommendation
from waypoint.n8n import ContextConfigurationError, OrgContextBatch
from waypoint.personas import PanelItem, PanelSelection
from waypoint.pipeline import (
    JSON_CALL_ATTEMPTS,
    LeaseLost,
    PipelineFailure,
    PipelineState,
    _dedupe_ideas,
    _heartbeat,
    _model_authored_url,
    _reaction_cache_key,
    _valid_json_call,
    finalize_run,
    run_job,
)
from waypoint.prompts import UNTRUSTED_END
from waypoint.queue import claim_job, enqueue, set_kill
from waypoint.tables import (
    CandidateRow,
    EvolveRoundRow,
    FleetControlRow,
    JobRow,
    MeasurementRow,
    PersonaEvalRow,
    RunRow,
    TouchOutcomeRow,
    WinnerRow,
)
from waypoint.warmstart import FINGERPRINT_VERSION

from .conftest import (
    CRITIC_BLOCK,
    CRITIC_OK,
    PERSONAS,
    FakeContext,
    FakeDeps,
    idea_json,
    reactions_json,
)

# Reaction → reduction_pp under the fixture calibration (3 identical reactions):
# 4.0 → -0.92 · 4.6 → 1.19 · 5.0 → 2.40 · 5.1 → 2.68 · 5.3 → 3.22 · 6.0 → 4.87
LOSE, FIRST_WIN, BETTER, NEAR_MISS, GOOD, GREAT = 4.0, 4.6, 5.0, 5.1, 5.3, 6.0


def test_model_authored_product_link_cannot_hide_under_null_feature_key() -> None:
    idea = Recommendation(
        title="Review service plans", mechanism="service_plans", actions=["Open https://example.com/service-plans"],
        pro_facing_concept="Review your service plans", manager_rationale="Ask about recurring work",
        channel="call", feature_key=None,
    )
    assert _model_authored_url(idea)
    idea.actions = ["Open https://example.net/service-plans"]
    assert _model_authored_url(idea)


async def run_status(session: AsyncSession, run_id: str) -> str:
    return (await session.execute(select(RunRow.status).where(RunRow.id == run_id))).scalar_one()


async def candidate_count(session: AsyncSession, run_id: str) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(CandidateRow).where(CandidateRow.run_id == run_id)
        )
    ).scalar_one()


async def set_loop_config(deps: FakeDeps, run_id: str, **overrides) -> None:
    run = await deps.db.get(RunRow, run_id)
    assert run is not None
    run.loop_config = {**(run.loop_config or {}), **overrides}
    await deps.db.commit()


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
    # Scoring stamps the sanitized fingerprint but NEVER eligibility — that is
    # earned only from an observed 7d return in outcome ingestion.
    assert winner.fingerprint == {
        "segment": "1A", "vertical": "hvac", "plan_tier": "basic",
        "tenure_band": "0-3m", "org_size_band": "solo", "open_ar_band": "low",
        "feature_adoption_band": "medium",
    }
    assert winner.fingerprint_version == FINGERPRINT_VERSION
    assert winner.warm_start_eligible is False
    assert winner.validation_status is None
    measurement = (
        await deps.db.execute(
            select(MeasurementRow).where(MeasurementRow.run_id == seeded_job.run_id)
        )
    ).scalar_one()
    assert measurement.indicators[0]["key"] == "invoices_sent"
    tiers = {(c["stage"], c["tier"]) for c in deps.gateway.calls}
    assert ("screen", "fast") in tiers
    assert ("final", "deep") in tiers


async def test_deep_run_routes_every_reasoning_stage_to_the_deep_model(
    deps: FakeDeps, seeded_job
) -> None:
    run = await deps.db.get(RunRow, seeded_job.run_id)
    run.model_tier = "deep"
    run.loop_config = {"MAX_ROUNDS": 1}
    await deps.db.commit()

    await run_job(seeded_job.id, deps)

    by_stage = {call["stage"]: call["tier"] for call in deps.gateway.calls}
    for stage in ("evolve", "critics", "rank", "screen", "wargame", "final"):
        assert by_stage[stage] == "deep"


async def test_staging_run_starts_async_context_and_waits_without_retrying(
    deps: FakeDeps,
    seeded_job,
) -> None:
    run = await deps.db.get(RunRow, seeded_job.run_id)
    assert run is not None
    run.context_source = "staging"
    run.audience_query = "workbench:promotion-ready"
    job = await deps.db.get(JobRow, seeded_job.id)
    assert job is not None
    job.attempts = 2
    await deps.db.commit()
    standard = deps.context
    staging = FakeContext()
    deps.staging_context = staging

    await run_job(seeded_job.id, deps)

    assert standard.fetches == []
    assert staging.fetches == []
    assert staging.starts == [("pro_1", seeded_job.id, "promotion-ready")]
    assert job.status == "waiting"
    assert job.attempts == 1
    assert job.checkpoint["staging_request"] == {"promotion_id": "promotion-ready"}


async def test_staging_run_reserves_waiting_slot_before_dispatch(
    deps: FakeDeps,
    seeded_job,
) -> None:
    run = await deps.db.get(RunRow, seeded_job.run_id)
    assert run is not None
    run.context_source = "staging"
    run.audience_query = "workbench:promotion-ready"

    class ReservedContext(FakeContext):
        async def start(
            self, organization_id: str, request_id: str, promotion_id: str
        ) -> None:
            job = await deps.db.get(JobRow, request_id)
            assert job is not None
            await deps.db.refresh(job)
            assert job.status == "waiting"
            assert job.checkpoint["staging_request"] == {
                "promotion_id": "promotion-ready"
            }
            await super().start(organization_id, request_id, promotion_id)

    deps.staging_context = ReservedContext()
    await deps.db.commit()

    await run_job(seeded_job.id, deps)

    assert deps.staging_context.starts == [
        ("pro_1", seeded_job.id, "promotion-ready")
    ]


async def test_staging_dispatch_failure_keeps_admission_reserved(
    deps: FakeDeps,
    seeded_job,
) -> None:
    run = await deps.db.get(RunRow, seeded_job.run_id)
    assert run is not None
    run.context_source = "staging"
    run.audience_query = "workbench:promotion-ready"
    staging = FakeContext()
    staging.unavailable = True
    deps.staging_context = staging
    await deps.db.commit()

    await run_job(seeded_job.id, deps)

    job = await deps.db.get(JobRow, seeded_job.id)
    assert job is not None
    await deps.db.refresh(job)
    assert job.status == "waiting"
    assert job.checkpoint["staging_request"] == {
        "promotion_id": "promotion-ready"
    }


async def test_staging_request_marker_is_never_dispatched_twice(
    deps: FakeDeps,
    seeded_job,
) -> None:
    run = await deps.db.get(RunRow, seeded_job.run_id)
    job = await deps.db.get(JobRow, seeded_job.id)
    assert run is not None and job is not None
    run.context_source = "staging"
    run.audience_query = "workbench:promotion-ready"
    job.checkpoint = {"staging_request": {"promotion_id": "promotion-ready"}}
    staging = FakeContext()
    deps.staging_context = staging
    await deps.db.commit()

    await run_job(seeded_job.id, deps)

    assert staging.starts == []
    await deps.db.refresh(job)
    assert job.status == "waiting"


async def test_staging_run_resumes_from_compact_callback_checkpoint(
    deps: FakeDeps,
    seeded_job,
) -> None:
    run = await deps.db.get(RunRow, seeded_job.run_id)
    job = await deps.db.get(JobRow, seeded_job.id)
    assert run is not None and job is not None
    run.context_source = "staging"
    run.audience_query = "workbench:promotion-ready"
    staging = FakeContext()
    deps.staging_context = staging
    source_brief = staging.batch.organizations[0]
    brief = {
        **source_brief.model_dump(mode="json"),
        "curated_context": source_brief.curated_context,
    }
    job.checkpoint = {
        "staging_request": {"promotion_id": "promotion-ready"},
        "staging_context": {
            "promotion_id": "promotion-ready",
            "brief": brief,
        },
    }
    await deps.db.commit()

    await run_job(seeded_job.id, deps)

    assert staging.starts == []
    assert staging.fetches == []
    assert await deps.store.stage_complete(seeded_job.id, "context")


async def test_winner_carries_canonical_item_identity(deps: FakeDeps, seeded_job) -> None:
    """V3: every fresh winner resolves to a canonical corpus item at creation."""
    from waypoint.tables import ItemRow

    await run_job(seeded_job.id, deps)
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "winner"
    assert winner.item_id is not None
    assert winner.item_version == "v1"
    assert winner.legacy_unresolved is False
    item = await deps.db.get(ItemRow, winner.item_id)
    assert item is not None
    candidate = await deps.db.get(CandidateRow, winner.candidate_id)
    assert item.mechanism == candidate.recommendation["mechanism"]


async def test_evaluation_calls_run_at_temperature_zero(deps: FakeDeps, seeded_job) -> None:
    await run_job(seeded_job.id, deps)
    by_stage: dict[str, set] = {}
    for c in deps.gateway.calls:
        by_stage.setdefault(c["stage"], set()).add(c["temperature"])
    assert by_stage["screen"] == {0.0}
    assert by_stage["final"] == {0.0}
    assert by_stage["evolve"] == {None}  # generation stays creative


async def test_reaction_prompts_carry_full_persona_cards(deps: FakeDeps, seeded_job) -> None:
    """A bare label+role panel produced constant role-driven ratings; the
    reaction prompt must carry each member's card substance."""
    await run_job(seeded_job.id, deps)
    for stage in ("screen", "final"):
        prompt = deps.gateway.prompts_for(stage)[0]
        assert '"card"' in prompt
        assert "trade_bucket" in prompt  # a card fact, not just a label
        assert "BECOME that persona" in prompt  # embodiment, not outside judging
        assert "SMS text message" in prompt  # the idea's channel frames delivery


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
    # One idea per round: this is a loop-sequencing test, so the generation
    # prompts map 1:1 to rounds and the ranker stays out of the way.
    await set_loop_config(deps, seeded_job.run_id, CANDIDATE_COUNT=1)
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
    assert "Concept 2 the pro would experience." in prompts[2]  # past losing idea is visible


async def test_keep_delta_rejects_a_small_improvement(deps: FakeDeps, seeded_job) -> None:
    deps.gateway.responses["screen"] = [
        reactions_json(BETTER),  # r1 win: 2.40pp
        reactions_json(NEAR_MISS),  # r2: 2.68pp, +0.28 under the 0.6 delta → lose
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
    assert "Mode: COLD" in prompts[0]
    assert "Mode: REFINE" in prompts[1]  # second try, never shifted
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
    # No round ever won the screen — recorded distinctly from a champion that
    # failed (or never reached) its final check.
    assert winner.rationale == "no_round_cleared_screen"


async def test_audience_query_stamped_once_from_sentinel(deps: FakeDeps, seeded_job) -> None:
    run = await deps.db.get(RunRow, seeded_job.run_id)
    run.audience_query = PENDING_AUDIENCE_QUERY
    await deps.db.commit()
    deps.context.audience_query_version = "audience_v8"
    await run_job(seeded_job.id, deps)
    await deps.db.refresh(run)
    assert run.audience_query == "audience_v8"


async def test_reported_audience_version_never_rewrites_a_real_value(
    deps: FakeDeps, seeded_job
) -> None:
    # Stamp-once: a mid-run flow redeploy (or an operator-asserted lineage on a
    # backfill) must not be clobbered by a later job's self-report.
    run = await deps.db.get(RunRow, seeded_job.run_id)
    original = run.audience_query
    assert original != PENDING_AUDIENCE_QUERY
    deps.context.audience_query_version = "audience_v9"
    await run_job(seeded_job.id, deps)
    await deps.db.refresh(run)
    assert run.audience_query == original


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
    await set_loop_config(deps, seeded_job.run_id, CANDIDATE_COUNT=1)
    deps.gateway.responses["critics"] = [CRITIC_BLOCK, CRITIC_OK]
    deps.gateway.responses["screen"] = [reactions_json(LOSE)]
    await run_job(seeded_job.id, deps)
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].outcome == "suppressed"
    # The suppressed round itself never spends on personas. Every subsequent
    # one-candidate round is screened because SHIFT now produces a distinct touch.
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


async def test_all_blocked_rounds_end_inconclusive_without_a_panel_verdict(
    deps: FakeDeps, seeded_job
) -> None:
    await set_loop_config(
        deps, seeded_job.run_id, MAX_NO_IMPROVE=2, MAX_ROUNDS=5, CANDIDATE_COUNT=1
    )
    deps.gateway.responses["evolve"] = [idea_json("first"), idea_json("second")]
    deps.gateway.responses["critics"] = [CRITIC_BLOCK, CRITIC_BLOCK]
    await run_job(seeded_job.id, deps)

    assert deps.gateway.calls_for("evolve") == 2
    assert deps.gateway.calls_for("screen") == 0
    assert [r.outcome for r in await rounds(deps.db, seeded_job.run_id)] == [
        "suppressed", "suppressed"
    ]
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "abstained"
    assert winner.rationale == "inconclusive_no_screen"


async def test_consent_ask_idea_is_suppressed_deterministically(
    deps: FakeDeps, seeded_job
) -> None:
    # The critic approves ("none") but the idea's pro-facing surface asks for
    # SMS opt-in: the deterministic gate must bench it with no persona spend.
    # Pinned to a single-idea round so the whole batch is the consent idea and
    # the round's only outcome is its deterministic suppression.
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1, CANDIDATE_COUNT=1)
    consent_idea = json.loads(idea_json("invoice_delivery"))
    consent_idea["pro_facing_concept"] = "Reply YES to opt in to text updates."
    deps.gateway.responses["evolve"] = [json.dumps(consent_idea)]
    deps.gateway.responses["critics"] = CRITIC_OK
    await run_job(seeded_job.id, deps)
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].outcome == "suppressed"
    suppressed = (
        (
            await deps.db.execute(
                select(CandidateRow).where(
                    CandidateRow.run_id == seeded_job.run_id, CandidateRow.status == "suppressed"
                )
            )
        )
        .scalars()
        .one()
    )
    assert suppressed.critics["block_kind"] == "consent_ask"


async def test_generation_failure_mid_loop_keeps_the_existing_champion(
    deps: FakeDeps, seeded_job
) -> None:
    """Regression: 713346 won 2.4pp at round 3, then the next round's generation
    returned bad JSON three times and the Pro was recorded as
    'failed - no result recorded', discarding the win."""
    # One idea per round so the generation prompts map 1:1 to rounds, and a win
    # threshold nothing can clear so the loop keeps going into the bad round.
    await set_loop_config(
        deps, seeded_job.run_id, CANDIDATE_COUNT=1, WIN_THRESHOLD_PP=99.0, MAX_ROUNDS=4
    )
    deps.gateway.responses["evolve"] = [idea_json("invoice_delivery", 1), "not json at all"]
    deps.gateway.responses["screen"] = [reactions_json(BETTER)]  # round 1 wins 2.40pp
    await run_job(seeded_job.id, deps)  # must not raise

    job = await deps.db.get(JobRow, seeded_job.id)
    await deps.db.refresh(job)
    assert "failure" not in job.checkpoint  # no PipelineFailure escaped the loop
    assert job.checkpoint["evolve"]["stop"].startswith("generation_unavailable")
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "winner"  # the round-1 champion shipped
    champion = await deps.db.get(CandidateRow, winner.candidate_id)
    assert champion is not None and champion.round == 1


async def test_generation_failure_with_no_champion_records_inconclusive(
    deps: FakeDeps, seeded_job
) -> None:
    """Nothing was ever won: the Pro still must not vanish — _stage_score
    records an inconclusive abstention rather than the job failing."""
    deps.gateway.responses["evolve"] = [IDEA_MISSING_ACTIONS]  # single entry -> always invalid
    await run_job(seeded_job.id, deps)  # must not raise
    assert deps.gateway.calls_for("evolve") == 3  # JSON_CALL_ATTEMPTS, then give up
    assert await candidate_count(deps.db, seeded_job.run_id) == 0
    assert await run_status(deps.db, seeded_job.run_id) == "abstained"
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "abstained"
    # NOT "no_round_cleared_screen": nothing was ever generated, so nothing was
    # ever screened. The row must not claim we looked and found nothing.
    assert winner.rationale == "inconclusive_no_round_generated"


async def test_no_action_after_a_screened_round_is_not_a_generation_failure(
    deps: FakeDeps, seeded_job
) -> None:
    """The sibling of the test above: rounds really were generated and screened
    and none cleared the bar. The two endings must stay distinguishable on the
    WinnerRow alone — a caller reading it never sees the evolve stop reason."""
    deps.gateway.responses["screen"] = [reactions_json(LOSE)]
    await run_job(seeded_job.id, deps)
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "no_action"
    assert winner.rationale == "no_round_cleared_screen"
    assert await rounds(deps.db, seeded_job.run_id)  # rounds were in fact screened


async def test_unhandled_crash_records_reason_and_requeues(deps: FakeDeps, seeded_job) -> None:
    """A raw dependency crash (the persona-429 / deep-400 incident) must burn
    the attempt immediately with a recorded cause — never an anonymous
    lease-expiry loop."""

    async def boom(segment: str):
        raise RuntimeError("persona service exploded")

    deps.get_personas = boom
    await run_job(seeded_job.id, deps)  # must not raise
    job = await deps.db.get(JobRow, seeded_job.id)
    await deps.db.refresh(job)
    assert job.status == "queued"  # attempts remain: retriable
    assert "unhandled at evolve" in job.checkpoint["failure"]["reason"]
    assert "persona service exploded" in job.checkpoint["failure"]["reason"]

    job.attempts = job.max_attempts  # last attempt burned
    await deps.db.commit()
    await run_job(seeded_job.id, deps)
    await deps.db.refresh(job)
    assert job.status == "failed"
    run = await deps.db.get(RunRow, seeded_job.run_id)
    await deps.db.refresh(run)
    assert run.status == "failed"
    assert "unhandled at evolve" in (run.stop_reason or "")


async def test_critic_failure_fails_closed(deps: FakeDeps, seeded_job) -> None:
    deps.gateway.fail_stage("critics")
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "failed"


async def test_rate_limit_failure_is_labeled_for_attribution(
    deps: FakeDeps, seeded_job
) -> None:
    """A 429 storm (MAX_LLM_IN_FLIGHT too high for the tier) must be
    attributable — the failure reason says rate_limited, not a generic fail."""
    deps.gateway.fail_stage("evolve")  # the fake raises RateLimitExhausted
    await run_job(seeded_job.id, deps)
    job = await deps.db.get(JobRow, seeded_job.id)
    await deps.db.refresh(job)
    assert "evolve_rate_limited" in job.checkpoint["failure"]["reason"]


async def test_rate_limited_generation_fails_loudly_instead_of_shipping(
    deps: FakeDeps, seeded_job
) -> None:
    """The champion-preserving catch must NOT swallow a 429: a rate limit means
    MAX_LLM_IN_FLIGHT is too high for the tier — an operator signal that must
    stay loud, unlike a model returning bad JSON."""
    # Round 1 wins, so there IS a champion to ship; the loop is still running
    # when the 429 lands, which is exactly the path the new catch guards.
    await set_loop_config(
        deps, seeded_job.run_id, CANDIDATE_COUNT=1, WIN_THRESHOLD_PP=99.0, MAX_ROUNDS=4
    )
    deps.gateway.responses["evolve"] = [
        idea_json("invoice_delivery", 1),
        RateLimitExhausted("injected 429 storm"),
    ]
    deps.gateway.responses["screen"] = [reactions_json(BETTER)]
    await run_job(seeded_job.id, deps)
    job = await deps.db.get(JobRow, seeded_job.id)
    await deps.db.refresh(job)
    assert "evolve_rate_limited" in job.checkpoint["failure"]["reason"]
    assert job.status == "failed"
    # No tidy no_action/winner papering over the misconfiguration.
    assert (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalars().first() is None


async def test_rate_limited_refill_fails_loudly_instead_of_being_absorbed(
    deps: FakeDeps, seeded_job
) -> None:
    """The bounded-refill catch (`except PipelineFailure: break`) must not
    swallow a 429 either: if the primary batch dedupes down below `count` and
    a refill then hits a rate-limit storm, that storm must still fail the job
    loudly, not get folded into a quiet generation_unavailable."""
    await set_loop_config(deps, seeded_job.run_id, CANDIDATE_COUNT=2)
    # Two ideas sharing a mechanism collapse to one after dedupe, so
    # _generate_batch is one idea short of `count` and must refill.
    duplicate_mechanism_batch = json.dumps(
        [json.loads(idea_json("invoice_delivery", 1)), json.loads(idea_json("invoice_delivery", 2))]
    )
    deps.gateway.responses["evolve"] = [
        duplicate_mechanism_batch,
        RateLimitExhausted("injected 429 storm on refill"),
    ]
    await run_job(seeded_job.id, deps)
    job = await deps.db.get(JobRow, seeded_job.id)
    await deps.db.refresh(job)
    assert "evolve_rate_limited" in job.checkpoint["failure"]["reason"]
    assert job.status == "failed"
    # No tidy no_action/winner papering over the misconfiguration.
    assert (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalars().first() is None


# A model returning valid JSON that OMITS a required field (the prod
# `evolve_failed: 1 validation error for Recommendation actions` incident).
IDEA_MISSING_ACTIONS = json.dumps(
    {
        "title": "Operational reminder",
        "mechanism": "invoice_delivery",
        "pro_facing_concept": "Concept the pro would experience.",
        "manager_rationale": "Rationale for the manager.",
        "channel": "sms",
        "risk": "May not land.",
    }
)


async def test_evolve_retries_model_output_missing_a_required_field(
    deps: FakeDeps, seeded_job
) -> None:
    """A dropped required field must not kill the Pro: the round re-asks under a
    fresh call key (the same key would replay the cached bad response) and the
    run completes."""
    run = await deps.db.get(RunRow, seeded_job.run_id)
    # win on round 1 → single round; one idea per round → one re-ask, no refills
    run.loop_config = {"WIN_THRESHOLD_PP": 3.0, "CANDIDATE_COUNT": 1}
    await deps.db.commit()
    deps.gateway.responses["evolve"] = [IDEA_MISSING_ACTIONS, idea_json("invoice_delivery")]
    deps.gateway.responses["screen"] = [reactions_json(GREAT)]
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "complete"
    assert deps.gateway.calls_for("evolve") == 2  # bad output → exactly one retry, then win


async def test_malformed_reactions_are_unavailable_not_a_crash(
    deps: FakeDeps,
    seeded_job,
) -> None:
    deps.gateway.responses["screen"] = "not json at all"
    deps.gateway.responses["final"] = "not json at all"
    await run_job(seeded_job.id, deps)  # must not raise
    assert await run_status(deps.db, seeded_job.run_id) == "abstained"
    assert {r.outcome for r in await rounds(deps.db, seeded_job.run_id)} == {"unavailable"}
    assert deps.gateway.calls_for("evolve") == 1


async def test_out_of_range_reactions_are_unavailable_not_scores(
    deps: FakeDeps, seeded_job
) -> None:
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1, CANDIDATE_COUNT=1)
    deps.gateway.responses["screen"] = json.dumps(
        [{"persona_id": persona.persona_id, "reaction": 8} for persona in PERSONAS]
    )

    await run_job(seeded_job.id, deps)

    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].outcome == "unavailable"
    assert ledger[0].score_pp is None


async def test_invalid_reactions_are_retried_before_candidate_is_unavailable(
    deps: FakeDeps, seeded_job
) -> None:
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1, CANDIDATE_COUNT=1)
    invalid = json.dumps(
        [{"persona_id": persona.persona_id, "reaction": 8} for persona in PERSONAS]
    )
    deps.gateway.responses["screen"] = [invalid, reactions_json(GREAT)]

    await run_job(seeded_job.id, deps)

    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].outcome == "win"
    assert deps.gateway.calls_for("screen") == 2


async def test_flat_reactions_resolve_to_no_action(deps: FakeDeps, seeded_job) -> None:
    flat = reactions_json(deps.calibration.pivot)
    deps.gateway.responses["screen"] = flat
    deps.gateway.responses["final"] = flat
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "no_action"


async def test_short_panel_degrades_with_flagged_output_instead_of_abstaining(
    deps: FakeDeps, seeded_job
) -> None:
    # Only one family qualifies (2 personas, no counterweight): the Pro still
    # gets an output, flagged as evaluated on a short-handed panel.
    solo = [p for p in PERSONAS if p.family == "solo_operators"]

    async def _solo(segment: str):
        return solo

    deps.get_personas = _solo
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "complete"
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "winner"
    disclaimer = winner.evidence["panel_disclaimer"]
    assert "only 2 of" in disclaimer["final"]
    # The notes name what the short panel actually voids: no dissenting
    # family, and a "held-out" final check that reused the screen's personas.
    assert "no counterweight" in disclaimer["final"]
    assert "reused 2 screen persona" in disclaimer["final"]


async def test_available_persona_fallback_never_abstains_for_no_exact_match(
    deps: FakeDeps, seeded_job
) -> None:
    # Even a one-card pool stays runnable and labels the short/reused panel.
    lone = [p for p in PERSONAS if p.family == "solo_operators"][:1]

    async def _lone(segment: str):
        return lone

    deps.get_personas = _lone
    await run_job(seeded_job.id, deps)
    assert await run_status(deps.db, seeded_job.run_id) == "complete"
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "winner"
    assert "panel_disclaimer" in winner.evidence


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
    run = await deps.db.get(RunRow, seeded_job.run_id)
    assert run is not None
    assert "standard" in str(run.stop_reason)


async def test_context_configuration_error_fails_without_requeue(
    deps: FakeDeps, seeded_job
) -> None:
    class MisconfiguredContext:
        async def fetch(self, pro_ids: list[str], on_retry=None) -> OrgContextBatch:
            raise ContextConfigurationError("async webhook cannot return rows")

    deps.context = MisconfiguredContext()
    await run_job(seeded_job.id, deps)

    job = await deps.db.get(JobRow, seeded_job.id)
    assert job is not None
    await deps.db.refresh(job)
    assert job.status == "failed"
    assert "context_configuration" in job.checkpoint["failure"]["reason"]


async def test_staging_context_outage_is_labeled_in_diagnostics(
    deps: FakeDeps, seeded_job
) -> None:
    run = await deps.db.get(RunRow, seeded_job.run_id)
    job = await deps.db.get(JobRow, seeded_job.id)
    assert run is not None and job is not None
    run.context_source = "staging"
    run.audience_query = "workbench:promotion-ready"
    job.attempts = job.max_attempts
    staging = FakeContext()
    staging.unavailable = True
    deps.staging_context = staging
    await deps.db.commit()

    await run_job(seeded_job.id, deps)

    await deps.db.refresh(job)
    assert "context_unavailable: staging:" in job.checkpoint["failure"]["reason"]


async def test_context_outage_for_one_pro_never_fails_the_run(deps: FakeDeps) -> None:
    # The 504-for-one-id incident: when one Pro's context flow stays dead
    # through its last retry, that Pro's job fails — the run must NOT go
    # terminal while sibling Pros are still working.
    run = RunRow(
        id="run-isolated",
        pro_ids=["pro_1", "pro_2"],
        audience_query="audience_v7",
        audience_run="2026-08-06T18:00:00Z",
        channels=["sms"],
        cost_limit=Decimal("100.00"),
    )
    deps.db.add(run)
    deps.db.add(FleetControlRow(id=1, day_cost_limit=Decimal("1000.00")))
    await deps.db.flush()
    doomed = await enqueue(deps.db, run.id, stage="pro", pro_id="pro_1")
    healthy = await enqueue(deps.db, run.id, stage="pro", pro_id="pro_2")
    doomed_job = await deps.db.get(JobRow, doomed)
    doomed_job.attempts = doomed_job.max_attempts  # retries already spent
    await deps.db.commit()

    deps.context.unavailable = True
    await run_job(doomed, deps)

    await deps.db.refresh(doomed_job)
    assert doomed_job.status == "failed"
    assert "context_unavailable" in doomed_job.checkpoint["failure"]["reason"]
    # The run stays live for the sibling; it only aggregates once ALL jobs
    # are terminal — and then to degraded, not failed.
    assert await run_status(deps.db, run.id) not in ("failed", "stopped")
    healthy_job = await deps.db.get(JobRow, healthy)
    assert healthy_job.status == "queued"
    healthy_job.status = "done"
    await deps.db.commit()
    assert await finalize_run(deps.db, run.id) == "degraded"


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


async def test_measurement_needs_no_paid_call_and_is_idempotent(deps: FakeDeps, seeded_job) -> None:
    await run_job(seeded_job.id, deps)
    assert deps.gateway.calls_for("measure") == 0  # V3: deterministic selection
    measurements = (
        await deps.db.execute(
            select(MeasurementRow).where(MeasurementRow.run_id == seeded_job.run_id)
        )
    ).scalars().all()
    assert len(measurements) == 1
    assert [i["key"] for i in measurements[0].indicators] == ["invoices_sent", "app_return"]
    await run_job(seeded_job.id, deps)  # terminal -> no-op, no duplicate plan
    measurements = (
        await deps.db.execute(
            select(MeasurementRow).where(MeasurementRow.run_id == seeded_job.run_id)
        )
    ).scalars().all()
    assert len(measurements) == 1


async def test_deep_final_failure_falls_back_to_fast_tier(deps: FakeDeps, seeded_job) -> None:
    # The deep tier dying must not lose the Pro: the held-out check downgrades
    # to the fast tier, honestly labeled, and the run still produces a winner.
    deps.gateway.responses["final"] = [RateLimitExhausted("deep tier down"), reactions_json(GOOD)]
    await run_job(seeded_job.id, deps)
    final_tiers = [c["tier"] for c in deps.gateway.calls if c["stage"] == "final"]
    assert final_tiers == ["deep", "fast"]
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "winner"
    champion = (
        await deps.db.execute(
            select(CandidateRow).where(
                CandidateRow.run_id == seeded_job.run_id, CandidateRow.status == "champion"
            )
        )
    ).scalar_one()
    assert champion.persona_evidence["final"]["tier"] == "fast"
    assert "deep tier down" in champion.persona_evidence["final"]["deep_failure"]


async def test_both_final_tiers_failing_abstains_with_both_reasons(
    deps: FakeDeps, seeded_job
) -> None:
    deps.gateway.responses["final"] = [RateLimitExhausted("deep down"), "no json here at all"]
    await run_job(seeded_job.id, deps)
    champion = (
        await deps.db.execute(
            select(CandidateRow).where(
                CandidateRow.run_id == seeded_job.run_id, CandidateRow.status == "champion"
            )
        )
    ).scalar_one()
    final_score = champion.score["final"]
    assert final_score["abstained"] is True
    assert "deep down" in final_score["abstain_reason"]  # the deep failure survives
    assert "unparseable" in final_score["abstain_reason"]  # and the fast one
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "abstained"
    assert winner.rationale.startswith("inconclusive_final_unavailable")
    # The winner-level rationale carries the real cause, not just the label
    # the incident was named after.
    assert "deep down" in winner.rationale


async def test_budget_exhausted_on_deep_final_never_falls_back(
    deps: FakeDeps, seeded_job
) -> None:
    # The fallback must not spend past an exhausted budget: BudgetExhausted
    # re-raises untouched, with no fast-tier attempt.
    deps.gateway.responses["final"] = [BudgetExhausted("out of budget")]
    await run_job(seeded_job.id, deps)
    assert [c["tier"] for c in deps.gateway.calls if c["stage"] == "final"] == ["deep"]
    assert await run_status(deps.db, seeded_job.run_id) == "stopped"


async def test_gate_blocked_pro_abstains_without_spend(deps: FakeDeps, seeded_job) -> None:
    # Make the only run channel affirmatively non-consented for this pro.
    brief = deps.context.batch.organizations[0]
    deps.context.batch.organizations[0] = brief.model_copy(
        update={"sms_consent_state": "opted_out"}
    )
    await run_job(seeded_job.id, deps)
    winner = (
        await deps.db.execute(select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id))
    ).scalar_one()
    assert winner.kind == "abstained"
    assert winner.rationale.startswith("infeasible:")
    assert deps.gateway.call_count == 0  # zero LLM spend before the gate


async def test_infeasible_channel_candidate_is_suppressed_without_panel(
    deps: FakeDeps, seeded_job
) -> None:
    # Generator ignores the directive and emits an email idea on an sms-only,
    # email-blocked pro: suppressed without critic or persona spend.
    brief = deps.context.batch.organizations[0]
    deps.context.batch.organizations[0] = brief.model_copy(
        update={"email_consent_state": "unsubscribed"}
    )
    await set_loop_config(deps, seeded_job.run_id, CANDIDATE_COUNT=1)
    email_idea = json.loads(idea_json("invoice_delivery"))
    email_idea["channel"] = "email"
    deps.gateway.responses["evolve"] = [json.dumps(email_idea)]
    await run_job(seeded_job.id, deps)
    candidate = (
        await deps.db.execute(select(CandidateRow).where(CandidateRow.run_id == seeded_job.run_id))
    ).scalars().first()
    assert candidate is not None
    assert candidate.status == "suppressed"
    assert candidate.critics["block_kind"] == "infeasible_channel"
    assert deps.gateway.calls_for("critics") == 0
    assert deps.gateway.calls_for("screen") == 0


async def test_null_feature_product_url_is_blocked_before_critic(
    deps: FakeDeps, seeded_job
) -> None:
    await set_loop_config(deps, seeded_job.run_id, CANDIDATE_COUNT=1, MAX_ROUNDS=1)
    idea = json.loads(idea_json("service_plan_question"))
    idea["feature_key"] = None
    idea["actions"] = ["Open https://example.net/service-plans"]
    deps.gateway.responses["evolve"] = [json.dumps(idea)]
    await run_job(seeded_job.id, deps)
    candidate = (
        await deps.db.execute(select(CandidateRow).where(CandidateRow.run_id == seeded_job.run_id))
    ).scalars().first()
    assert candidate is not None
    assert candidate.status == "suppressed"
    assert candidate.critics["block_kind"] == "infeasible_execution"
    assert deps.gateway.calls_for("critics") == 0
    assert deps.gateway.calls_for("screen") == 0


async def test_recently_failed_mechanism_is_suppressed(
    db_session: AsyncSession, deps: FakeDeps, seeded_job
) -> None:
    db_session.add(TouchOutcomeRow(
        recommendation_id="old-w", source="test", pro_id="pro_1",
        channel="sms", mechanism="invoice_delivery", journey_window="churn_risk",
        unsubscribed=True,
    ))
    await db_session.commit()
    await set_loop_config(deps, seeded_job.run_id, CANDIDATE_COUNT=1)
    # FakeLLM's default evolve batch leads with mechanism "invoice_delivery".
    await run_job(seeded_job.id, deps)
    candidates = (await db_session.execute(
        select(CandidateRow).where(CandidateRow.run_id == seeded_job.run_id)
    )).scalars().all()
    suppressed = [c for c in candidates if c.critics.get("block_kind") == "recently_failed"]
    assert suppressed  # the failed mechanism never reached the panel
    assert all(c.persona_evidence == {} for c in suppressed)


async def test_evidence_reaches_the_evolve_prompt(
    db_session: AsyncSession, deps: FakeDeps, seeded_job
) -> None:
    db_session.add(TouchOutcomeRow(
        recommendation_id="old-w", source="test", pro_id="someone_else",
        channel="sms", mechanism="review_boost", journey_window="churn_risk",
        returned_7d=True,
    ))
    await db_session.commit()
    await run_job(seeded_job.id, deps)
    prompts = deps.gateway.prompts_for("evolve")
    assert prompts and "review_boost via sms" in prompts[0]


async def test_persona_reactions_are_cached_across_jobs(
    db_session: AsyncSession, deps: FakeDeps, seeded_job
) -> None:
    await run_job(seeded_job.id, deps)
    screen_calls_first = deps.gateway.calls_for("screen")
    assert screen_calls_first > 0
    cached = (await db_session.execute(select(PersonaEvalRow))).scalars().all()
    assert cached  # every paid reaction round left a cache row

    # Second identical run: same brief, same fake ideas -> same panel+concept.
    run2 = RunRow(
        id="run-pipe-2",
        pro_ids=["pro_1"],
        audience_query="audience_v7",
        audience_run="2026-08-06T18:00:00Z",
        channels=["sms"],
        model_tier="fast",
        cost_limit=Decimal("100.00"),
    )
    db_session.add(run2)
    await db_session.flush()
    job2 = await enqueue(db_session, run2.id, stage="pro", pro_id="pro_1")
    await db_session.commit()
    await run_job(job2, deps)
    # No new screen/final spend: reactions came from the cache.
    assert deps.gateway.calls_for("screen") == screen_calls_first


def test_reaction_cache_key_changes_with_snapshot_version_and_tier() -> None:
    panel = PanelSelection(
        items=[
            PanelItem(
                persona_id="p1",
                label="Persona 1",
                family="fam",
                role="closest",
                fit_score=0.9,
                rationale="r",
            )
        ],
        fit_threshold=0.5,
        snapshot_version="v1",
        match_features={},
    )
    base = _reaction_cache_key(panel, "concept", "sms", "deep")

    # Persona cards changed (new snapshot) but PROMPT_VERSION didn't bump —
    # the key must still change or a stale reaction is served forever.
    newer_panel = panel.model_copy(update={"snapshot_version": "v2"})
    assert _reaction_cache_key(newer_panel, "concept", "sms", "deep") != base

    # Same panel/concept/channel but a different tier must not collide —
    # a fast-tier reaction can't satisfy a later deep-tier lookup.
    assert _reaction_cache_key(panel, "concept", "sms", "fast") != base


async def test_winner_carries_bounded_follow_up(
    db_session: AsyncSession, deps: FakeDeps, seeded_job
) -> None:
    await run_job(seeded_job.id, deps)
    winner = (
        await db_session.execute(
            select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id, WinnerRow.kind == "winner")
        )
    ).scalar_one()
    follow_up = winner.evidence["follow_up"]
    assert set(follow_up) == {"on_return", "on_click_no_use", "on_no_interaction", "on_negative"}
    assert follow_up["on_negative"] == {"action": "stop", "channel": "none"}


async def test_war_game_failure_does_not_block_the_winner(
    db_session: AsyncSession, deps: FakeDeps, seeded_job
) -> None:
    deps.gateway.responses["wargame"] = "not json at all"
    await run_job(seeded_job.id, deps)
    winner = (
        await db_session.execute(
            select(WinnerRow).where(WinnerRow.run_id == seeded_job.run_id, WinnerRow.kind == "winner")
        )
    ).scalar_one()
    assert "follow_up" not in winner.evidence
    assert "follow_up_unavailable" in winner.evidence
    # The measurement plan still landed: the war game is additive, never blocking.
    measurement = (
        await db_session.execute(
            select(MeasurementRow).where(MeasurementRow.winner_id == winner.id)
        )
    ).scalar_one()
    assert measurement.indicators


async def test_war_game_prompt_omits_channels_this_pro_opted_out_of(
    db_session: AsyncSession, deps: FakeDeps
) -> None:
    # sms is opted out for this pro but the run still offers sms + email;
    # _stage_evolve gates its own prompt, but the war game must re-gate too,
    # or a follow_up branch can recommend the un-consented channel verbatim.
    brief = deps.context.batch.organizations[0]
    deps.context.batch.organizations[0] = brief.model_copy(
        update={"sms_consent_state": "opted_out"}
    )
    run = RunRow(
        id="run-wargame-gate",
        pro_ids=["pro_1"],
        audience_query="audience_v7",
        audience_run="2026-08-06T18:00:00Z",
        channels=["sms", "email"],
        cost_limit=Decimal("100.00"),
        loop_config={"CANDIDATE_COUNT": 1, "MAX_ROUNDS": 1},
    )
    db_session.add(run)
    db_session.add(FleetControlRow(id=1, day_cost_limit=Decimal("1000.00")))
    await db_session.flush()
    job_id = await enqueue(db_session, run.id, stage="pro", pro_id="pro_1")
    await db_session.commit()

    email_idea = json.loads(idea_json("invoice_delivery"))
    email_idea["channel"] = "email"
    deps.gateway.responses["evolve"] = [json.dumps(email_idea)]

    await run_job(job_id, deps)
    prompts = deps.gateway.prompts_for("wargame")
    assert prompts  # the war game did run (winner exists, gate wasn't blocked)
    assert '"sms"' not in prompts[0]


FEATURE_SUBSTR = "without calling the office"  # from online_booking's trimmed description
FEATURE_BLOCK_HEADER = "HCP features referenced in this Pro's context (reference data):"


@pytest.mark.asyncio
async def test_feature_block_shared_by_generate_critic_rank(deps: FakeDeps, seeded_job) -> None:
    """The resolved feature block must reach generation, critic, AND ranker
    identically — a critic that can't see it wrongly blocks grounded ideas.
    Byte-identical, not just "contains the same substring": a stage that
    wrapped or altered the block would still pass a substring check."""
    await run_job(seeded_job.id, deps)
    blocks: dict[str, str] = {}
    for stage in ("evolve", "critics", "rank"):
        prompts = deps.gateway.prompts_for(stage)
        assert prompts, f"no {stage} prompt captured"
        assert all(FEATURE_SUBSTR in p for p in prompts), f"{stage} missing feature block"
        first = prompts[0]
        start = first.index(FEATURE_BLOCK_HEADER)
        # Bounded at the closing fence tag, not "to end of string": critic_prompt
        # embeds the fenced context mid-prompt (followed by "Ideas:"), so
        # slicing to end-of-string would capture stage-specific trailing text
        # and never compare equal even when the injected block itself matches.
        end = first.index(UNTRUSTED_END, start)
        blocks[stage] = first[start:end]
    assert blocks["evolve"] == blocks["critics"] == blocks["rank"]


@pytest.mark.asyncio
async def test_feasibility_hints_off_by_default_in_context(deps: FakeDeps, seeded_job) -> None:
    await run_job(seeded_job.id, deps)
    assert all("reachable on" not in p for p in deps.gateway.prompts_for("evolve"))


def _idea(mechanism: str, concept: str = "") -> Recommendation:
    return Recommendation(
        title="t",
        mechanism=mechanism,
        actions=["a"],
        pro_facing_concept=concept or f"concept for {mechanism}",
        manager_rationale="r",
        channel="sms",
    )


def test_refine_keeps_ideas_whose_mechanism_label_was_reworded() -> None:
    """Regression: the model rewords its label, so an exact-match filter threw
    the entire paid batch away and fell through to shift-mode refills."""
    batch = [
        _idea("Simplify payment collection workflow to lower barriers and billing confidence"),
        _idea("Streamline payment collection to reduce friction"),
        _idea("Make collecting payment effortless for this Pro"),
    ]
    kept = _dedupe_ideas(batch, 3, mode="refine")
    assert len(kept) == 3


def test_refine_still_deduplicates_identical_concepts() -> None:
    batch = [
        _idea("payment collection", concept="Store the card once, charge every time"),
        _idea("payment collection reworded", concept="store the card once, charge every time"),
        _idea("payment collection again", concept="Send a reminder the day after the job"),
    ]
    kept = _dedupe_ideas(batch, 3, mode="refine")
    assert len(kept) == 2, "same concept in different words is one candidate, not two"


def test_shift_still_forbids_already_tried_mechanisms() -> None:
    batch = [_idea("payment collection"), _idea("online booking adoption")]
    kept = _dedupe_ideas(
        batch, 3, mode="shift", forbidden_mechanisms=["Payment Collection"]
    )
    assert [i.mechanism for i in kept] == ["online booking adoption"]


# --- lease renewal during a round -------------------------------------------
# Three retry layers multiply around every paid call (JSON_CALL_ATTEMPTS x
# retry_rate_limit x the SDK's own retries) while _guard only heartbeats
# BETWEEN rounds, so a single stage could outlive the lease, get the job
# re-claimed, and be paid for twice. Every paid attempt now renews it.


def _heartbeat_spy(monkeypatch, *, alive_for: int | None = None) -> list[str]:
    """Record every lease renewal, optionally losing the lease after N of them."""
    beats: list[str] = []
    real = queue_module.heartbeat_job

    async def spy(session, job_id, worker_id, lease_seconds=600):
        beats.append(job_id)
        if alive_for is not None and len(beats) > alive_for:
            return False
        return await real(session, job_id, worker_id, lease_seconds)

    monkeypatch.setattr(queue_module, "heartbeat_job", spy)
    return beats


async def _owned_state(deps: FakeDeps, seeded_job) -> PipelineState:
    job = await claim_job(deps.db, "worker-owner", lease_seconds=60)
    assert job is not None
    await deps.db.commit()
    deps.worker_id = "worker-owner"
    run = await deps.db.get(RunRow, seeded_job.run_id)
    return PipelineState(job=job, run=run, pro_id=seeded_job.pro_id)


def _never_valid(text: str) -> object:
    raise ValueError("unparseable output")


async def _json_call(state: PipelineState, deps: FakeDeps):
    return await _valid_json_call(
        state,
        deps,
        base_key=f"{state.run.id}:{state.pro_id}:probe",
        tier="fast",
        prompt="prompt",
        run_id=state.run.id,
        pro_id=state.pro_id,
        stage="evolve",
        system="system",
        parse=_never_valid,
    )


async def test_every_paid_json_attempt_renews_the_lease(
    deps: FakeDeps, seeded_job, monkeypatch
) -> None:
    state = await _owned_state(deps, seeded_job)
    beats = _heartbeat_spy(monkeypatch)

    with pytest.raises(PipelineFailure):
        await _json_call(state, deps)

    # One renewal per paid attempt, not one for the whole re-ask budget.
    assert deps.gateway.calls_for("evolve") == JSON_CALL_ATTEMPTS
    assert len(beats) == JSON_CALL_ATTEMPTS


async def test_a_mid_call_lease_loss_stops_the_spend_and_propagates(
    deps: FakeDeps, seeded_job, monkeypatch
) -> None:
    state = await _owned_state(deps, seeded_job)
    # The first renewal succeeds; a second worker takes the job before the
    # re-ask. LeaseLost must escape _valid_json_call's broad failure handling —
    # swallowing it into a PipelineFailure would keep this worker paying for a
    # job it no longer owns.
    beats = _heartbeat_spy(monkeypatch, alive_for=1)

    with pytest.raises(LeaseLost):
        await _json_call(state, deps)

    assert len(beats) == 2
    assert deps.gateway.calls_for("evolve") == 1  # no further paid attempt


async def test_a_mid_round_lease_loss_never_fails_the_job(
    deps: FakeDeps, seeded_job, monkeypatch
) -> None:
    await _owned_state(deps, seeded_job)
    _heartbeat_spy(monkeypatch, alive_for=1)

    await run_job(seeded_job.id, deps)

    # The new owner resumes from the durable checkpoint: this worker must not
    # burn an attempt, fail the job, or terminalize the run on its way out.
    job = await deps.db.get(JobRow, seeded_job.id)
    await deps.db.refresh(job)
    assert job.status not in ("failed", "done")
    assert await run_status(deps.db, seeded_job.run_id) not in ("failed", "complete")


async def test_workers_without_an_id_still_run_without_heartbeating(
    deps: FakeDeps, seeded_job, monkeypatch
) -> None:
    # Tests and the non-worker paths run with worker_id None: there is no lease
    # to renew, so heartbeating must be skipped rather than crash.
    beats = _heartbeat_spy(monkeypatch)
    assert deps.worker_id is None

    await run_job(seeded_job.id, deps)

    assert beats == []
    assert await run_status(deps.db, seeded_job.run_id) == "complete"


async def test_an_owned_run_renews_the_lease_before_every_paid_call(
    deps: FakeDeps, seeded_job, monkeypatch
) -> None:
    # The persona screen path (_react) does not go through _valid_json_call, so
    # it carries its own renewal; without it the count falls short of the paid
    # calls and a long screen would silently lapse the lease.
    await _owned_state(deps, seeded_job)
    beats = _heartbeat_spy(monkeypatch)

    await run_job(seeded_job.id, deps)

    assert await run_status(deps.db, seeded_job.run_id) == "complete"
    assert len(beats) >= deps.gateway.call_count
    # run_job wires the slot-queue heartbeat too, so a call parked waiting for
    # a fleet slot renews the lease as well as one in flight.
    assert deps.llm.on_wait is not None
    assert deps.context.on_retry is not None  # and the context flow's retries


async def test_concurrent_heartbeats_never_race_the_one_job_session(
    deps: FakeDeps, seeded_job
) -> None:
    # What deps.heartbeat_lock exists for. Concurrent screen stacks each own
    # their paid-call session, but _heartbeat always runs on the ONE job
    # session, and an AsyncSession is strictly one-statement-at-a-time:
    # unserialized renewals raise mid-round and kill a job that is fine.
    state = await _owned_state(deps, seeded_job)

    await asyncio.gather(*[_heartbeat(state, deps) for _ in range(8)])

    job = await deps.db.get(JobRow, seeded_job.id)
    await deps.db.refresh(job)
    assert job.worker_id == "worker-owner"


async def test_a_starved_slot_wait_renews_the_lease(
    deps: FakeDeps, seeded_job, monkeypatch
) -> None:
    # The wait for a fleet slot is unfair and unbounded, and it sits INSIDE
    # MeteredLLM.complete — i.e. after _valid_json_call's heartbeat. A call
    # parked in that queue would otherwise burn lease time no one renews.
    state = await _owned_state(deps, seeded_job)
    beats = _heartbeat_spy(monkeypatch)
    polls = 0

    async def starved(on_wait=None) -> int:
        nonlocal polls
        while polls < 3:  # three trips round the poll loop, then a slot frees
            polls += 1
            if on_wait is not None:
                await on_wait()
        return 0

    monkeypatch.setattr(deps.llm.slots, "acquire", starved)
    deps.llm.on_wait = partial(_heartbeat, state, deps)

    with pytest.raises(PipelineFailure):
        await _json_call(state, deps)

    # One per paid attempt (3) plus one per poll iteration of the first
    # attempt's wait (3): the queue no longer eats lease time silently.
    assert polls == 3
    assert len(beats) == JSON_CALL_ATTEMPTS + 3


async def test_a_lease_lost_while_queued_for_a_slot_is_not_relabelled(
    deps: FakeDeps, seeded_job, monkeypatch
) -> None:
    # LeaseLost now reaches _valid_json_call from INSIDE complete(). Its blanket
    # "any other failure is an honest job failure" arm must not swallow it into
    # a PipelineFailure — that would keep this worker paying for a job a second
    # worker already owns.
    state = await _owned_state(deps, seeded_job)
    # alive_for=1, NOT 0: the attempt's own top-of-attempt heartbeat (which sits
    # OUTSIDE the try) must succeed, so that the beat which loses the lease is
    # the slot-wait one raising from inside complete(). With 0 the lease is lost
    # before complete() is ever entered and this test proves nothing.
    _heartbeat_spy(monkeypatch, alive_for=1)
    waited = False

    async def starved(on_wait=None) -> int:
        nonlocal waited
        assert on_wait is not None
        waited = True
        await on_wait()
        raise AssertionError("unreachable: the heartbeat above lost the lease")

    monkeypatch.setattr(deps.llm.slots, "acquire", starved)
    deps.llm.on_wait = partial(_heartbeat, state, deps)

    with pytest.raises(LeaseLost):
        await _json_call(state, deps)

    assert waited  # the slot queue really was the thing that lost the lease
    assert deps.gateway.calls_for("evolve") == 0  # never reached the provider
