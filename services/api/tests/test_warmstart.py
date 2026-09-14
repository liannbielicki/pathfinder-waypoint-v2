"""Warm-start groundwork: sanitized fingerprints + outcome-earned eligibility.

The load-bearing rule under test: ONLY outcome ingestion of a real observed
7-day return may set warm_start_eligible.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import literal_column, select, table, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from waypoint import pipeline, warmstart
from waypoint.models import TouchOutcomeIn
from waypoint.n8n import ALLOWED_FIELDS, OrgBrief
from waypoint.outcomes import ingest
from waypoint.pipeline import run_job
from waypoint.tables import CandidateRow, RunRow, TouchOutcomeRow, WinnerRow
from waypoint.warmstart import (
    DEFAULT_SIMILARITY_WEIGHTS,
    FINGERPRINT_FIELDS,
    FINGERPRINT_VERSION,
    build_fingerprint,
    retrieve,
    similarity,
)

from .conftest import (
    DEFAULT_MECHANISMS,
    FakeDeps,
    InjectedCrash,
    batch_json,
    critics_json,
    rank_json,
    reactions_json,
)
from .test_pipeline import GOOD, LOSE, candidate_count, rounds, run_status, set_loop_config

BRIEF = OrgBrief(
    org_uuid="org-abc-123",
    segment="1A",
    vertical="hvac",
    plan_tier="basic",
    tenure_band="0-3m",
    open_ar_band="low",
    ar_aging_band="past_30",  # allowlisted for context, NOT a fingerprint field
    recommended_focus="online_booking",
)


@pytest.fixture
async def seeded_winner(db_session: AsyncSession) -> str:
    db_session.add(
        RunRow(
            id="run-ws", pro_ids=["pro_1"], audience_query="audience_v7",
            audience_run="2026-08-06T18:00:00Z", channels=["sms"],
            cost_limit=Decimal("100.00"),
        )
    )
    await db_session.flush()
    db_session.add(
        CandidateRow(
            id="cand-ws", run_id="run-ws", pro_id="pro_1",
            recommendation={"mechanism": "human_assist", "channel": "sms"},
        )
    )
    db_session.add(
        WinnerRow(
            id="win-ws", run_id="run-ws", pro_id="pro_1", kind="winner",
            candidate_id="cand-ws", fingerprint=build_fingerprint(BRIEF),
            fingerprint_version=FINGERPRINT_VERSION,
        )
    )
    await db_session.commit()
    return "win-ws"


async def _winner(session: AsyncSession, winner_id: str = "win-ws") -> WinnerRow:
    winner = (
        await session.execute(select(WinnerRow).where(WinnerRow.id == winner_id))
    ).scalar_one()
    await session.refresh(winner)
    return winner


def outcome(**kwargs) -> TouchOutcomeIn:
    # routing defaults to a real send: these tests are about promotion
    # mechanics, and only a real send can promote at all (see outcomes.py).
    return TouchOutcomeIn(
        recommendation_id="win-ws", source="iterable_n8n",
        routing=kwargs.pop("routing", "route-to-pro"), **kwargs,
    )


SENT_WS = datetime(2026, 8, 1, 12, tzinfo=UTC)


def returned_within_7d(**kwargs) -> TouchOutcomeIn:
    """V3: a 7d positive is derived from a confirmed send + real return event,
    never asserted by the caller."""
    return outcome(
        send_status="confirmed", sent_at=SENT_WS,
        first_return_at=SENT_WS + timedelta(days=3), **kwargs,
    )


def no_return_within_7d(**kwargs) -> TouchOutcomeIn:
    """First return long after day 7: a derived, measured 7d negative."""
    return outcome(
        send_status="confirmed", sent_at=SENT_WS,
        first_return_at=SENT_WS + timedelta(days=20), **kwargs,
    )


# --- sanitization -----------------------------------------------------------


def test_fingerprint_fields_are_a_strict_subset_of_the_context_allowlist() -> None:
    assert set(FINGERPRINT_FIELDS) < set(ALLOWED_FIELDS)
    assert "org_uuid" not in FINGERPRINT_FIELDS
    assert len(set(FINGERPRINT_FIELDS)) == len(FINGERPRINT_FIELDS)


def test_build_fingerprint_carries_only_allowlisted_bands() -> None:
    fingerprint = build_fingerprint(BRIEF)
    assert set(fingerprint) <= set(FINGERPRINT_FIELDS)
    assert fingerprint == {
        "segment": "1A", "vertical": "hvac", "plan_tier": "basic",
        "tenure_band": "0-3m", "open_ar_band": "low",
    }
    # No identifier, no free text, no non-fingerprint context field.
    assert "org_uuid" not in fingerprint
    assert "ar_aging_band" not in fingerprint
    assert "recommended_focus" not in fingerprint
    assert BRIEF.org_uuid not in fingerprint.values()


def test_build_fingerprint_omits_absent_fields() -> None:
    assert build_fingerprint(OrgBrief(org_uuid="org-1")) == {}


async def test_stamped_winner_carries_fingerprint_and_version(
    db_session: AsyncSession, seeded_winner: str
) -> None:
    winner = await _winner(db_session)
    assert winner.fingerprint == build_fingerprint(BRIEF)
    assert winner.fingerprint_version == FINGERPRINT_VERSION
    assert winner.warm_start_eligible is False  # persisting a fingerprint is not eligibility


# --- outcome-earned eligibility --------------------------------------------


async def test_observed_7d_return_promotes_the_winner(
    db_session: AsyncSession, seeded_winner: str
) -> None:
    await ingest(db_session, [returned_within_7d(channel="sms")])
    winner = await _winner(db_session)
    assert winner.warm_start_eligible is True
    assert winner.validation_status == "validated"
    assert winner.warm_start_evidence == {
        "returned_7d": True,
        "source": "iterable_n8n",
        "mechanism": "human_assist",
        "channel": "sms",
    }


async def test_observed_no_return_records_a_negative_and_stays_ineligible(
    db_session: AsyncSession, seeded_winner: str
) -> None:
    await ingest(db_session, [no_return_within_7d(channel="sms")])
    winner = await _winner(db_session)
    assert winner.warm_start_eligible is False
    assert winner.validation_status == "validated_negative"
    assert winner.warm_start_evidence["returned_7d"] is False


async def test_persona_scored_winner_without_an_outcome_is_never_eligible(
    db_session: AsyncSession, seeded_winner: str
) -> None:
    # Everything short of a measured 7d return: delivered, clicked, replied.
    await ingest(db_session, [outcome(delivered=True, clicked=True, replied=True)])
    winner = await _winner(db_session)
    assert winner.warm_start_eligible is False
    assert winner.validation_status is None
    assert winner.warm_start_evidence == {}


async def test_duplicate_ingestion_converges(
    db_session: AsyncSession, seeded_winner: str
) -> None:
    item = returned_within_7d(channel="sms")
    await ingest(db_session, [item])
    first = dict((await _winner(db_session)).warm_start_evidence)
    await ingest(db_session, [item])
    winner = await _winner(db_session)
    assert winner.warm_start_eligible is True
    assert winner.validation_status == "validated"
    assert winner.warm_start_evidence == first


async def test_late_7d_arrival_promotes_on_resubmission(
    db_session: AsyncSession, seeded_winner: str
) -> None:
    await ingest(db_session, [outcome(delivered=True, channel="sms")])
    assert (await _winner(db_session)).validation_status is None
    await ingest(db_session, [returned_within_7d(channel="sms")])
    assert (await _winner(db_session)).warm_start_eligible is True
    # A later flag-less record must not un-promote it.
    await ingest(db_session, [outcome(delivered=True, channel="sms")])
    winner = await _winner(db_session)
    assert winner.warm_start_eligible is True
    assert winner.validation_status == "validated"


async def test_unattributed_outcome_promotes_nothing(
    db_session: AsyncSession, seeded_winner: str
) -> None:
    result = await ingest(
        db_session,
        [TouchOutcomeIn(recommendation_id="no-such-winner", source="iterable_n8n",
                        delivered=True)],
    )
    assert result["unattributed"] == 1
    winner = await _winner(db_session)
    assert winner.warm_start_eligible is False
    assert winner.validation_status is None


async def test_no_action_winner_is_not_promoted(db_session: AsyncSession) -> None:
    db_session.add(
        RunRow(
            id="run-na", pro_ids=["pro_2"], audience_query="audience_v7",
            audience_run="2026-08-06T18:00:00Z", channels=["sms"],
            cost_limit=Decimal("100.00"),
        )
    )
    db_session.add(WinnerRow(id="win-na", run_id="run-na", pro_id="pro_2", kind="no_action"))
    await db_session.commit()
    await ingest(
        db_session,
        [TouchOutcomeIn(
            recommendation_id="win-na", source="iterable_n8n",
            send_status="confirmed", sent_at=SENT_WS,
            first_return_at=SENT_WS + timedelta(days=3),
        )],
    )
    winner = await _winner(db_session, "win-na")
    assert winner.warm_start_eligible is False
    assert winner.validation_status is None


async def test_promotion_survives_the_duplicate_commit_race(
    db_session: AsyncSession, db_session_factory: async_sessionmaker, seeded_winner: str
) -> None:
    # Two sibling requests submit the same (recommendation_id, source); the
    # loser rebuilds its batch after IntegrityError and must still promote.
    async def _ingest() -> None:
        async with db_session_factory() as session:
            await ingest(session, [returned_within_7d(channel="sms")])

    await asyncio.gather(_ingest(), _ingest())
    winner = await _winner(db_session)
    assert winner.warm_start_eligible is True
    assert winner.validation_status == "validated"


# --- migration --------------------------------------------------------------


async def test_warm_start_indexes_exist(db_session: AsyncSession) -> None:
    names = set(
        (
            await db_session.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
        ).scalars()
    )
    assert {"ix_winners_warm_start", "ix_llm_calls_run_pro_status"} <= names


# --- similarity (pure) ------------------------------------------------------


def test_similarity_of_an_identical_fingerprint_is_one() -> None:
    fingerprint = build_fingerprint(BRIEF)
    assert similarity(fingerprint, dict(fingerprint)) == 1.0


def test_empty_query_fingerprint_scores_zero_not_a_perfect_match() -> None:
    assert similarity({}, {"segment": "1A"}) == 0.0
    assert similarity({}, {}) == 0.0


def test_field_missing_on_the_candidate_is_a_mismatch() -> None:
    # segment weighs 2, vertical + plan_tier weigh 1 each => 4 total.
    query = {"segment": "1A", "vertical": "hvac", "plan_tier": "basic"}
    assert similarity(query, {"segment": "1A", "vertical": "hvac"}) == 0.75
    assert similarity(query, {"vertical": "hvac", "plan_tier": "basic"}) == 0.5
    assert similarity(query, {}) == 0.0


def test_similarity_reads_only_weighted_allowlisted_keys() -> None:
    # A hand-inserted winner row carrying non-allowlisted keys contributes
    # nothing: unweighted keys are invisible to the score, matching or not.
    query = {"segment": "1A", "vertical": "hvac"}
    polluted = {"segment": "1A", "vertical": "hvac", "org_uuid": "org-source", "notes": "raw"}
    assert similarity(query, polluted) == 1.0
    assert set(DEFAULT_SIMILARITY_WEIGHTS) == set(FINGERPRINT_FIELDS)


def test_custom_weights_reweigh_the_score() -> None:
    query = {"segment": "1A", "vertical": "hvac"}
    candidate = {"segment": "1A", "vertical": "plumbing"}
    assert similarity(query, candidate, {"segment": 3.0, "vertical": 1.0}) == 0.75
    assert similarity(query, candidate, {"segment": 1.0, "vertical": 3.0}) == 0.25
    # A weight map narrower than the fingerprint scores only what it names.
    assert similarity(query, candidate, {"segment": 1.0}) == 1.0


def test_query_fields_with_no_weight_are_ignored_entirely() -> None:
    assert similarity({"vertical": "hvac"}, {"vertical": "plumbing"}, {"segment": 1.0}) == 0.0


# --- retrieve ---------------------------------------------------------------

QUERY_BRIEF = OrgBrief(org_uuid="org-query", segment="1A", vertical="hvac", plan_tier="basic")


async def seed_source_winner(
    session: AsyncSession,
    *,
    winner_id: str,
    fingerprint: dict[str, str],
    mechanism: str | None = "human_assist",
    version: str | None = FINGERPRINT_VERSION,
    eligible: bool = True,
    run_id: str = "run-src",
    org_id: str = "org-source-xyz",
) -> str:
    """An eligible winner from ANOTHER org, exactly as outcomes.ingest leaves it."""
    pro_id = f"pro_source_{winner_id}"
    if await session.get(RunRow, run_id) is None:
        session.add(
            RunRow(
                id=run_id, pro_ids=[pro_id], audience_query="audience_v7",
                audience_run="2026-08-06T18:00:00Z", channels=["sms"],
                cost_limit=Decimal("100.00"),
            )
        )
        await session.flush()
    session.add(
        WinnerRow(
            id=winner_id, run_id=run_id, pro_id=pro_id, kind="winner",
            rationale="SOURCE_ORG_RATIONALE_SECRET",
            evidence={"org_id": org_id, "final": {"raw": "SOURCE_ORG_EVIDENCE_SECRET"}},
            fingerprint=fingerprint,
            fingerprint_version=version,
            warm_start_eligible=eligible,
            warm_start_evidence=(
                {"returned_7d": True, "source": "iterable_n8n", "mechanism": mechanism,
                 "channel": "sms"}
                if mechanism is not None
                else {"returned_7d": True, "source": "iterable_n8n"}
            ),
            validation_status="validated" if eligible else None,
        )
    )
    await session.commit()
    return winner_id


async def test_retrieve_returns_the_best_eligible_match(db_session: AsyncSession) -> None:
    await seed_source_winner(
        db_session, winner_id="win-far", fingerprint={"segment": "2B"}, mechanism="far_mechanism"
    )
    await seed_source_winner(
        db_session,
        winner_id="win-near",
        fingerprint={"segment": "1A", "vertical": "hvac", "plan_tier": "basic"},
    )
    match, telemetry = await retrieve(db_session, QUERY_BRIEF, threshold=0.75)
    assert match is not None
    assert (match.mechanism, match.winner_id) == ("human_assist", "win-near")
    assert match.score == 1.0
    assert match.fingerprint_version == FINGERPRINT_VERSION
    assert telemetry["outcome"] == "warm"
    assert telemetry["scanned"] == 2
    assert telemetry["best_score"] == 1.0
    assert telemetry["latency_ms"] >= 0


async def test_retrieve_with_no_eligible_winners_is_a_cold_start(db_session: AsyncSession) -> None:
    match, telemetry = await retrieve(db_session, QUERY_BRIEF, threshold=0.75)
    assert match is None
    assert telemetry == {
        "scanned": 0, "latency_ms": telemetry["latency_ms"], "best_score": None,
        "outcome": "cold",
    }


async def test_retrieve_matches_at_exactly_the_threshold(db_session: AsyncSession) -> None:
    # segment(2) + vertical(1) match, plan_tier(1) does not => exactly 0.75.
    await seed_source_winner(
        db_session,
        winner_id="win-edge",
        fingerprint={"segment": "1A", "vertical": "hvac", "plan_tier": "pro"},
    )
    match, telemetry = await retrieve(db_session, QUERY_BRIEF, threshold=0.75)
    assert match is not None and match.score == 0.75
    assert telemetry["outcome"] == "warm"


async def test_retrieve_just_below_the_threshold_is_a_cold_start(
    db_session: AsyncSession,
) -> None:
    await seed_source_winner(
        db_session,
        winner_id="win-edge",
        fingerprint={"segment": "1A", "vertical": "hvac", "plan_tier": "pro"},
    )
    match, telemetry = await retrieve(db_session, QUERY_BRIEF, threshold=0.7500001)
    assert match is None
    assert telemetry["outcome"] == "cold"
    assert telemetry["best_score"] == 0.75  # scored, just not similar enough


async def test_ineligible_winners_are_never_retrieved(db_session: AsyncSession) -> None:
    await seed_source_winner(
        db_session, winner_id="win-pending", fingerprint=build_fingerprint(QUERY_BRIEF),
        eligible=False,
    )
    match, telemetry = await retrieve(db_session, QUERY_BRIEF, threshold=0.75)
    assert match is None and telemetry["scanned"] == 0


@pytest.mark.parametrize("version", [None, "fp_v0_unknown"])
async def test_missing_or_unknown_fingerprint_versions_are_never_retrieved(
    db_session: AsyncSession, version: str | None
) -> None:
    # A briefless winner is promoted with version None and DOES sit in the
    # partial index (btree indexes NULLs) — the WHERE clause is what excludes it.
    await seed_source_winner(
        db_session, winner_id="win-unversioned",
        fingerprint={"segment": "1A", "vertical": "hvac", "plan_tier": "basic"},
        version=version,
    )
    match, telemetry = await retrieve(db_session, QUERY_BRIEF, threshold=0.75)
    assert match is None
    assert telemetry["scanned"] == 0
    assert telemetry["outcome"] == "cold"


async def test_winners_without_a_mechanism_label_are_skipped(db_session: AsyncSession) -> None:
    await seed_source_winner(
        db_session, winner_id="win-nomech",
        fingerprint=build_fingerprint(QUERY_BRIEF), mechanism=None,
    )
    match, telemetry = await retrieve(db_session, QUERY_BRIEF, threshold=0.75)
    assert match is None
    assert telemetry["scanned"] == 1  # scanned, but it carries nothing to seed with
    assert telemetry["best_score"] is None


async def test_non_allowlisted_keys_on_a_stored_fingerprint_never_score(
    db_session: AsyncSession,
) -> None:
    # Hand-inserted pollution: raw org data on the row must not raise similarity.
    await seed_source_winner(
        db_session, winner_id="win-polluted",
        fingerprint={
            "segment": "1A", "vertical": "hvac",
            "org_uuid": "org-source-xyz", "recommended_focus": "online_booking",
        },
    )
    match, telemetry = await retrieve(db_session, QUERY_BRIEF, threshold=0.5)
    assert match is not None
    # segment(2) + vertical(1) of 4: the two extra keys contribute nothing.
    assert telemetry["best_score"] == 0.75


async def test_broken_retrieval_degrades_visibly(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(brief):
        raise RuntimeError("fingerprint store is down")

    monkeypatch.setattr(warmstart, "build_fingerprint", boom)
    with caplog.at_level(logging.WARNING, logger="waypoint.warmstart"):
        match, telemetry = await retrieve(db_session, QUERY_BRIEF, threshold=0.75)
    assert match is None
    assert telemetry["outcome"] == "degraded"
    assert "fingerprint store is down" in telemetry["error"]
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_a_db_level_failure_leaves_the_session_writable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller keeps writing the round through this same session, so a DB
    failure inside retrieve must not leave it needing a rollback."""

    # The scan must fail at the DB layer, not in Python: only a real aborted
    # statement leaves the transaction needing a rollback. Still a Select, so
    # retrieve's .where/.order_by/.limit chain applies as usual.
    monkeypatch.setattr(
        warmstart,
        "select",
        lambda *_: select(literal_column("*")).select_from(table("no_such_table")),
    )
    match, telemetry = await retrieve(db_session, QUERY_BRIEF, threshold=0.75)
    assert match is None
    assert telemetry["outcome"] == "degraded"
    # Guards the injection itself: a Python-level failure here would leave the
    # session healthy and make the assertion below pass for the wrong reason.
    assert "no_such_table" in telemetry["error"]
    # Without the rollback this raises InFailedSQLTransactionError, and in the
    # real pipeline the round dies at commit with all its spend already made.
    assert (await db_session.execute(select(WinnerRow))).scalars().all() == []


async def test_every_retrieval_logs_one_structured_line(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="waypoint.warmstart"):
        await retrieve(db_session, QUERY_BRIEF, threshold=0.75)
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(lines) == 1
    assert "outcome=cold" in lines[0]
    for field in ("scanned=", "latency_ms=", "best_score=", "threshold="):
        assert field in lines[0]


async def test_retrieval_is_index_backed(db_session: AsyncSession) -> None:
    definition = (
        await db_session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_winners_warm_start'")
        )
    ).scalar_one()
    # The retrieval WHERE/ORDER BY must be exactly what the index covers.
    assert "fingerprint_version" in definition
    assert "created_at DESC" in definition
    assert "WHERE warm_start_eligible" in definition
    await seed_source_winner(
        db_session, winner_id="win-plan", fingerprint=build_fingerprint(QUERY_BRIEF)
    )
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        (
            await db_session.execute(
                text(
                    "EXPLAIN SELECT id, fingerprint, warm_start_evidence FROM winners "
                    "WHERE warm_start_eligible AND fingerprint_version = :v "
                    "ORDER BY created_at DESC LIMIT 200"
                ),
                {"v": FINGERPRINT_VERSION},
            )
        ).scalars()
    )
    assert "ix_winners_warm_start" in plan


# --- pipeline: warm round 1 -------------------------------------------------

WARM_MECHANISM = "human_assist"
WARM_BATCH = [WARM_MECHANISM, *DEFAULT_MECHANISMS]
# pro_1's own bands (tests/fixtures/n8n_context.json) — an identical
# fingerprint scores 1.0 and is unambiguously warm.
PRO_1_FINGERPRINT = {
    "segment": "1A", "plan_tier": "basic", "tenure_band": "0-3m",
    "org_size_band": "solo", "vertical": "hvac", "open_ar_band": "low",
    "feature_adoption_band": "medium",
}


def script_warm_batch(deps: FakeDeps, *, rank: tuple[tuple[str, float], ...] | None = None) -> None:
    """A four-idea round: the warm-start mechanism plus the usual three."""
    deps.gateway.responses["evolve"] = [batch_json(WARM_BATCH)]
    deps.gateway.responses["critics"] = critics_json(["none"] * 4)
    deps.gateway.responses["rank"] = rank_json(
        *(rank or (("c1", 0.9), ("c2", 0.5), ("c3", 0.3), ("c4", 0.1)))
    )


async def _count_retrievals(monkeypatch) -> list[int]:
    """Count pipeline retrievals in a one-element list the caller can assert on."""
    calls = [0]
    real = pipeline.retrieve

    async def counted(*args, **kwargs):
        calls[0] += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "retrieve", counted)
    return calls


async def warm_evidence(deps: FakeDeps, run_id: str) -> dict:
    return (await rounds(deps.db, run_id))[0].ranking["warm_start"]


async def test_eligible_similar_winner_produces_a_warm_round_one(
    deps: FakeDeps, seeded_job
) -> None:
    await seed_source_winner(deps.db, winner_id="win-warm", fingerprint=PRO_1_FINGERPRINT)
    script_warm_batch(deps)
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)
    await run_job(seeded_job.id, deps)

    prompt = deps.gateway.prompts_for("evolve")[0]
    assert "WARM START" in prompt
    assert WARM_MECHANISM in prompt
    assert "exactly 4 new ideas" in prompt  # candidate_count + ONE additional
    assert deps.gateway.calls_for("evolve") == 1  # no refill: the batch arrived whole
    assert await candidate_count(deps.db, seeded_job.run_id) == 4

    evidence = await warm_evidence(deps, seeded_job.run_id)
    assert evidence["outcome"] == "warm"
    assert evidence["mechanism"] == WARM_MECHANISM
    assert evidence["winner_id"] == "win-warm"
    assert evidence["score"] == 1.0
    assert evidence["best_score"] == 1.0
    assert evidence["mechanism_in_batch"] is True
    assert evidence["scanned"] == 1


async def test_no_eligible_winner_leaves_a_normal_cold_start(deps: FakeDeps, seeded_job) -> None:
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)
    await run_job(seeded_job.id, deps)
    prompt = deps.gateway.prompts_for("evolve")[0]
    assert "WARM START" not in prompt
    assert "exactly 3 new ideas" in prompt
    assert await candidate_count(deps.db, seeded_job.run_id) == 3
    evidence = await warm_evidence(deps, seeded_job.run_id)
    assert evidence["outcome"] == "cold"
    assert evidence["scanned"] == 0
    assert "mechanism" not in evidence


async def test_a_dissimilar_winner_below_threshold_stays_cold(
    deps: FakeDeps, seeded_job
) -> None:
    await seed_source_winner(
        deps.db, winner_id="win-far", fingerprint={"segment": "9Z", "vertical": "plumbing"}
    )
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)
    await run_job(seeded_job.id, deps)
    assert "WARM START" not in deps.gateway.prompts_for("evolve")[0]
    assert await candidate_count(deps.db, seeded_job.run_id) == 3
    evidence = await warm_evidence(deps, seeded_job.run_id)
    assert evidence["outcome"] == "cold"
    assert evidence["best_score"] == 0.0


async def test_warm_start_threshold_is_per_run_configurable(
    deps: FakeDeps, seeded_job
) -> None:
    # segment(2)+plan_tier(1) of 8 => 0.375: warm only under a lowered threshold.
    await seed_source_winner(
        deps.db, winner_id="win-loose", fingerprint={"segment": "1A", "plan_tier": "basic"}
    )
    script_warm_batch(deps)
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1, WARM_START_THRESHOLD=0.3)
    await run_job(seeded_job.id, deps)
    evidence = await warm_evidence(deps, seeded_job.run_id)
    assert evidence["outcome"] == "warm"
    assert evidence["score"] == 0.375


async def test_only_round_one_retrieves(deps: FakeDeps, seeded_job, monkeypatch) -> None:
    await seed_source_winner(deps.db, winner_id="win-warm", fingerprint=PRO_1_FINGERPRINT)
    calls = await _count_retrievals(monkeypatch)
    deps.gateway.responses["evolve"] = [
        batch_json(WARM_BATCH), batch_json(["m4", "m5", "m6"]), batch_json(["m7", "m8", "m9"]),
    ]
    deps.gateway.responses["critics"] = [critics_json(["none"] * 4), critics_json(["none"] * 3)]
    deps.gateway.responses["rank"] = [
        rank_json(("c1", 0.9), ("c2", 0.5), ("c3", 0.3), ("c4", 0.1)),
        rank_json(("c1", 0.9), ("c2", 0.5), ("c3", 0.3)),
    ]
    deps.gateway.responses["screen"] = [reactions_json(GOOD), reactions_json(LOSE)]
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=3)
    await run_job(seeded_job.id, deps)
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert len(ledger) > 1
    assert calls == [1]  # exactly one retrieval for the whole loop
    assert "warm_start" not in ledger[1].ranking


# --- pipeline: the warm candidate competes, it never wins by default ---------


async def test_warm_candidate_ranked_last_is_not_selected(deps: FakeDeps, seeded_job) -> None:
    await seed_source_winner(deps.db, winner_id="win-warm", fingerprint=PRO_1_FINGERPRINT)
    # c1 is the warm candidate (batch index 0); the ranker puts it dead last.
    script_warm_batch(deps, rank=(("c2", 0.9), ("c3", 0.5), ("c4", 0.3), ("c1", 0.1)))
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)
    await run_job(seeded_job.id, deps)
    assert deps.gateway.calls_for("screen") == 1  # clear winner: only rank 1 screened
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].mechanism != WARM_MECHANISM  # never an automatic winner
    champion = await deps.db.get(CandidateRow, ledger[0].candidate_id)
    assert champion is not None and champion.recommendation["mechanism"] == DEFAULT_MECHANISMS[0]
    warm_row = (
        await deps.db.execute(
            select(CandidateRow).where(
                CandidateRow.run_id == seeded_job.run_id,
                CandidateRow.recommendation["mechanism"].astext == WARM_MECHANISM,
            )
        )
    ).scalar_one()
    assert warm_row.status == "discarded"
    assert warm_row.critics["block_kind"] == "none"  # it was critiqued like the rest


async def test_warm_candidate_ranked_first_is_screened_like_any_finalist(
    deps: FakeDeps, seeded_job
) -> None:
    await seed_source_winner(deps.db, winner_id="win-warm", fingerprint=PRO_1_FINGERPRINT)
    script_warm_batch(deps)  # c1 (warm) ranks first
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)
    await run_job(seeded_job.id, deps)
    assert deps.gateway.calls_for("critics") == 1
    assert deps.gateway.calls_for("rank") == 1
    assert deps.gateway.calls_for("screen") == 1
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].mechanism == WARM_MECHANISM
    assert ledger[0].score_pp is not None  # it earned its place through the screen
    champion = await deps.db.get(CandidateRow, ledger[0].candidate_id)
    assert champion is not None and champion.persona_evidence["screen"]["reactions"]


async def test_warm_candidate_is_suppressed_by_the_critic_like_any_other(
    deps: FakeDeps, seeded_job
) -> None:
    await seed_source_winner(deps.db, winner_id="win-warm", fingerprint=PRO_1_FINGERPRINT)
    script_warm_batch(deps)
    deps.gateway.responses["critics"] = critics_json(["ungrounded", "none", "none", "none"])
    deps.gateway.responses["rank"] = rank_json(("c1", 0.9), ("c2", 0.5), ("c3", 0.3))
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)
    await run_job(seeded_job.id, deps)
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].mechanism != WARM_MECHANISM
    assert len(ledger[0].ranking["order"]) == 3  # the warm candidate never reached the ranker


# --- pipeline: guards and degradation ---------------------------------------


async def test_a_recently_failed_warm_mechanism_is_skipped(deps: FakeDeps, seeded_job) -> None:
    await seed_source_winner(deps.db, winner_id="win-warm", fingerprint=PRO_1_FINGERPRINT)
    deps.db.add(
        TouchOutcomeRow(
            recommendation_id="old-touch", source="iterable_n8n", pro_id="pro_1",
            mechanism=WARM_MECHANISM, channel="sms", returned_7d=False,
        )
    )
    await deps.db.commit()
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)
    await run_job(seeded_job.id, deps)
    assert "WARM START" not in deps.gateway.prompts_for("evolve")[0]
    assert await candidate_count(deps.db, seeded_job.run_id) == 3  # cold start
    evidence = await warm_evidence(deps, seeded_job.run_id)
    assert evidence["outcome"] == "cold"
    assert evidence["skipped"] == "recently_failed"
    assert evidence["mechanism"] == WARM_MECHANISM  # audit: what was skipped, and why


async def test_broken_retrieval_yields_a_degraded_cold_start(
    deps: FakeDeps, seeded_job, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    await seed_source_winner(deps.db, winner_id="win-warm", fingerprint=PRO_1_FINGERPRINT)

    def boom(brief):
        raise RuntimeError("retrieval is down")

    monkeypatch.setattr(warmstart, "build_fingerprint", boom)
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)
    with caplog.at_level(logging.WARNING, logger="waypoint.warmstart"):
        await run_job(seeded_job.id, deps)
    # The round still completed, cold.
    assert await run_status(deps.db, seeded_job.run_id) == "complete"
    assert "WARM START" not in deps.gateway.prompts_for("evolve")[0]
    assert await candidate_count(deps.db, seeded_job.run_id) == 3
    evidence = await warm_evidence(deps, seeded_job.run_id)
    assert evidence["outcome"] == "degraded"
    assert "retrieval is down" in evidence["error"]
    assert any("degrading to a cold start" in r.getMessage() for r in caplog.records)


async def test_retrieval_adds_no_paid_call(deps: FakeDeps, seeded_job) -> None:
    await seed_source_winner(deps.db, winner_id="win-warm", fingerprint=PRO_1_FINGERPRINT)
    script_warm_batch(deps)
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)
    await run_job(seeded_job.id, deps)
    stages = {c["stage"] for c in deps.gateway.calls}
    assert stages <= {"evolve", "critics", "rank", "screen", "final", "measure", "wargame"}
    assert deps.gateway.calls_for("evolve") == 1  # the warm idea rode the SAME call


# --- cross-org isolation ----------------------------------------------------

SOURCE_ONLY_BAND = "SOURCE_ONLY_CHURN_BAND"


async def test_a_warm_start_leaks_nothing_but_the_mechanism(
    deps: FakeDeps, seeded_job
) -> None:
    # 6 of 8 weighted fields match (0.75); churn_risk_state exists only on the
    # SOURCE org, so it must appear nowhere in this run.
    await seed_source_winner(
        deps.db,
        winner_id="win-warm",
        fingerprint={
            "segment": "1A", "plan_tier": "basic", "tenure_band": "0-3m",
            "org_size_band": "solo", "vertical": "hvac",
            "churn_risk_state": SOURCE_ONLY_BAND,
        },
    )
    script_warm_batch(deps)
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)
    await run_job(seeded_job.id, deps)

    secrets = (
        SOURCE_ONLY_BAND, "SOURCE_ORG_RATIONALE_SECRET", "SOURCE_ORG_EVIDENCE_SECRET",
        "org-source-xyz", "pro_source_win-warm", "run-src",
    )
    for prompt in (c["prompt"] for c in deps.gateway.calls):
        for secret in secrets:
            assert secret not in prompt

    ledger = await rounds(deps.db, seeded_job.run_id)
    stored = json.dumps(
        [r.ranking for r in ledger]
        + [
            c.recommendation
            for c in (
                await deps.db.execute(
                    select(CandidateRow).where(CandidateRow.run_id == seeded_job.run_id)
                )
            ).scalars()
        ]
    )
    for secret in secrets:
        assert secret not in stored
    # winner_id is the one identifier kept, deliberately: an internal audit key
    # on the round row, never in a prompt and never org data.
    assert ledger[0].ranking["warm_start"]["winner_id"] == "win-warm"
    assert all("win-warm" not in c["prompt"] for c in deps.gateway.calls)


# --- replay -----------------------------------------------------------------


async def test_resumed_round_one_does_not_retrieve_again(
    deps: FakeDeps, seeded_job, monkeypatch
) -> None:
    await seed_source_winner(deps.db, winner_id="win-warm", fingerprint=PRO_1_FINGERPRINT)
    script_warm_batch(deps)
    calls = await _count_retrievals(monkeypatch)
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)
    deps.fail_after("evolve")  # crash once the round ledger row is committed
    with pytest.raises(InjectedCrash):
        await run_job(seeded_job.id, deps)
    committed = await warm_evidence(deps, seeded_job.run_id)
    assert committed["outcome"] == "warm"
    assert calls == [1]

    # The source winner is gone by the time the job resumes: a second retrieval
    # would now decide "cold". The committed round row must be untouched.
    deps.clear_failure()
    await deps.db.execute(text("DELETE FROM winners WHERE id = 'win-warm'"))
    await deps.db.commit()
    await run_job(seeded_job.id, deps)
    assert calls == [1]  # never retrieved a second time
    assert await warm_evidence(deps, seeded_job.run_id) == committed
    assert deps.gateway.calls_for("evolve") == 1  # the round was not regenerated
