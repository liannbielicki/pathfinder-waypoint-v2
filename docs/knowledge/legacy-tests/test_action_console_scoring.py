from __future__ import annotations

import dataclasses

from pathfinder.action_console.models import (
    AudienceBoundary,
    GeneratedIdea,
    OrgUuidEvidence,
)
from pathfinder.action_console.scoring import (
    ActionIdeaScorer,
    ranked_ideas,
    top_three_ranked,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_idea(**overrides):
    base = dict(
        run_id="run-1",
        idea_id="idea-1",
        test_number=1,
        title="Reach out about renewal",
        prose_action="Send a personalized renewal nudge.",
        outreach_action_type="email",
        idea_category="retention",
        generation_source="llm",
        source_kind="generated",
        llm_estimate_pp=4.0,
        health_delta=1.0,
        churn_risk_reduction_pp=0.0,
        persona_reduction_pp=None,
        persona_ci_lower=None,
        persona_ci_upper=None,
        persona_panel_n=0,
        in_calibrated_range=False,
        persona_status="pending",
        persona_error=None,
        historical_corroboration={"observed_pp": 3.0, "n": 50},
        rationale="",
        risk_reasoning="",
        evidence_summary="Strong signal.",
        exact_org_uuid_sample=["uuid-a"],
        final_choice_panel=[],
        created_at="2026-06-25T00:00:00Z",
    )
    base.update(overrides)
    return GeneratedIdea(**base)


def _make_audience(**overrides):
    base = dict(
        audience_id="aud-1",
        label="At-risk Pros – West",
        filters={"region": ["west"], "plan_tier": ["starter"]},
        factor_summary=[{"factor": "low_usage", "weight": "high"}],
        org_uuid_evidence=[
            OrgUuidEvidence(
                org_uuid="org-uuid-aaa",
                org_id="111",
                matched_factors={"low_usage": "yes"},
                evidence_source="extract",
                observed_at="2026-06-20T00:00:00Z",
            ),
            OrgUuidEvidence(
                org_uuid="org-uuid-bbb",
                org_id="222",
                matched_factors={"region": "west"},
                evidence_source="extract",
                observed_at="2026-06-20T00:00:00Z",
            ),
        ],
        extract_manifest={"extract_id": "ex-1", "rows": 200},
    )
    base.update(overrides)
    return AudienceBoundary(**base)


# ---------------------------------------------------------------------------
# Test 1: top_three returns exactly three ideas sorted by final score
# ---------------------------------------------------------------------------

def test_top_three_sorted_order_and_tiebreakers():
    """top_three returns ≤3 ideas; sorted by score desc, then health_delta desc,
    then llm_estimate_pp desc, then lower test_number first."""
    scorer = ActionIdeaScorer()
    audience = _make_audience()

    # Build >3 distinct scored ideas
    ideas_raw = [
        _make_idea(
            idea_id="idea-a", test_number=1,
            persona_status="scored", persona_reduction_pp=10.0,
            llm_estimate_pp=5.0, health_delta=2.0,
        ),
        _make_idea(
            idea_id="idea-b", test_number=2,
            persona_status="scored", persona_reduction_pp=7.0,
            llm_estimate_pp=4.0, health_delta=1.5,
        ),
        _make_idea(
            idea_id="idea-c", test_number=3,
            persona_status="scored", persona_reduction_pp=5.0,
            llm_estimate_pp=3.0, health_delta=1.0,
        ),
        _make_idea(
            idea_id="idea-d", test_number=4,
            persona_status="scored", persona_reduction_pp=2.0,
            llm_estimate_pp=1.0, health_delta=0.5,
        ),
    ]

    scored = scorer.score_many(audience=audience, ideas=ideas_raw)
    top = scorer.top_three(scored)

    assert len(top) == 3
    assert top[0].idea_id == "idea-a"
    assert top[1].idea_id == "idea-b"
    assert top[2].idea_id == "idea-c"
    # The worst idea should be excluded
    assert all(i.idea_id != "idea-d" for i in top)

    # Tiebreaker: two ideas with equal score, different health_delta
    tie_ideas_raw = [
        _make_idea(
            idea_id="tie-1", test_number=10,
            persona_status="scored", persona_reduction_pp=8.0,
            llm_estimate_pp=5.0, health_delta=3.0,
        ),
        _make_idea(
            idea_id="tie-2", test_number=11,
            persona_status="scored", persona_reduction_pp=8.0,
            llm_estimate_pp=5.0, health_delta=1.0,   # lower health_delta
        ),
        _make_idea(
            idea_id="tie-3", test_number=12,
            persona_status="scored", persona_reduction_pp=8.0,
            llm_estimate_pp=6.0, health_delta=1.0,   # same health as tie-2, higher llm
        ),
        _make_idea(
            idea_id="tie-4", test_number=5,           # lower test_number beats tie-3
            persona_status="scored", persona_reduction_pp=8.0,
            llm_estimate_pp=4.0, health_delta=1.0,
        ),
    ]
    scored_tie = scorer.score_many(audience=audience, ideas=tie_ideas_raw)
    top_tie = scorer.top_three(scored_tie)

    # All four have score 8.0; top_three picks first 3
    assert len(top_tie) == 3
    # health_delta 3.0 wins first
    assert top_tie[0].idea_id == "tie-1"
    # Among tie-2/tie-3/tie-4 (health_delta 1.0), higher llm_estimate_pp wins:
    #   tie-3 llm=6.0 → 2nd, tie-2 llm=5.0 → 3rd, tie-4 llm=4.0 → 4th (excluded)
    assert top_tie[1].idea_id == "tie-3"
    assert top_tie[2].idea_id == "tie-2"
    # tie-4 (lowest llm among tied group) is excluded as 4th
    assert all(i.idea_id != "tie-4" for i in top_tie)


# ---------------------------------------------------------------------------
# Screen-then-confirm (Task 11): abstained-with-screening ranking
# ---------------------------------------------------------------------------


def test_screening_estimates_sort_below_scored_ordered_by_screening_value():
    """Abstained ideas carrying a screening estimate (Task 11) sort among
    themselves by screening_reduction_pp descending, always below any scored
    idea, and above a bare abstained/failed idea with no numeric estimate."""
    scored = _make_idea(
        idea_id="scored-1", test_number=1,
        persona_status="scored", persona_reduction_pp=1.0,
        churn_risk_reduction_pp=1.0,
    )
    screening_low = _make_idea(
        idea_id="screening-low", test_number=2,
        persona_status="abstained", persona_error="reaction_not_significant",
        screening_reduction_pp=2.0, screening_direction_ci_lower_pp=0.1,
        churn_risk_reduction_pp=0.0,
    )
    screening_high = _make_idea(
        idea_id="screening-high", test_number=3,
        persona_status="abstained", persona_error="reaction_not_significant",
        screening_reduction_pp=9.0, screening_direction_ci_lower_pp=0.2,
        churn_risk_reduction_pp=0.0,
    )
    bare_abstained = _make_idea(
        idea_id="bare-abstained", test_number=4,
        persona_status="abstained", persona_error="thin_support",
        churn_risk_reduction_pp=0.0,
    )
    failed = _make_idea(
        idea_id="failed-1", test_number=5,
        persona_status="failed", persona_error="service down",
        churn_risk_reduction_pp=0.0,
    )

    ranked = ranked_ideas([bare_abstained, screening_low, failed, scored, screening_high])

    assert [idea.idea_id for idea in ranked] == [
        "scored-1",       # any scored idea always first
        "screening-high",  # among screening ideas, higher screening value first
        "screening-low",
        "bare-abstained",  # no numeric estimate at all -- below screening ideas
        "failed-1",
    ]


def test_screening_estimate_does_not_perturb_scored_idea_ordering():
    """Two scored ideas' relative order must be unaffected by the new
    screening tie-break key (it is a constant (0, 0.0) for scored ideas)."""
    better = _make_idea(
        idea_id="better", test_number=1,
        persona_status="scored", persona_reduction_pp=8.0,
        churn_risk_reduction_pp=8.0,
    )
    worse = _make_idea(
        idea_id="worse", test_number=2,
        persona_status="scored", persona_reduction_pp=3.0,
        churn_risk_reduction_pp=3.0,
    )
    ranked = ranked_ideas([worse, better])
    assert [idea.idea_id for idea in ranked] == ["better", "worse"]


# ---------------------------------------------------------------------------
# Test 2: Winner explanation contains required substrings
# ---------------------------------------------------------------------------

def test_winner_explanation_contains_required_substrings():
    """enrich_winner_reasoning embeds LLM estimate, generation_source, and caveat."""
    scorer = ActionIdeaScorer()
    audience = _make_audience()

    winner_raw = _make_idea(
        idea_id="winner-1",
        persona_status="scored",
        persona_reduction_pp=12.0,
        llm_estimate_pp=9.5,
        health_delta=2.3,
        generation_source="historical_replay",
    )
    scored = scorer.score_many(audience=audience, ideas=[winner_raw])
    winner = scored[0]
    enriched = scorer.enrich_winner_reasoning(audience=audience, winner=winner)

    assert "LLM estimate" in enriched.rationale
    assert "generation_source" in enriched.rationale
    assert "Why it might not be good" in enriched.rationale


def test_winner_explanation_flags_directional_prior_confidence():
    """enrich_winner_reasoning appends a directional-estimate caveat to the risk
    reasoning when the winner's calibration_confidence is 'directional_prior'."""
    scorer = ActionIdeaScorer()
    audience = _make_audience()

    winner_raw = _make_idea(
        idea_id="winner-directional",
        persona_status="scored",
        persona_reduction_pp=12.0,
        calibration_confidence="directional_prior",
    )
    scored = scorer.score_many(audience=audience, ideas=[winner_raw])
    winner = scored[0]
    enriched = scorer.enrich_winner_reasoning(audience=audience, winner=winner)

    assert "directional estimate" in enriched.risk_reasoning
    assert "not yet validated" in enriched.risk_reasoning


# ---------------------------------------------------------------------------
# Test 3: Persona failure/abstention cannot move final score or clear goal
# ---------------------------------------------------------------------------

def test_persona_failure_abstention_cannot_move_score():
    """Ideas with failed/abstained persona get churn_risk_reduction_pp=0.0,
    cannot outrank a genuinely scored idea, and their reasoning surfaces the state."""
    scorer = ActionIdeaScorer()
    audience = _make_audience()

    # High LLM estimate but failed persona
    failed_idea = _make_idea(
        idea_id="failed-1",
        persona_status="failed",
        persona_reduction_pp=None,
        persona_error="Persona service timeout",
        llm_estimate_pp=20.0,   # very high, should NOT dominate ranking
        health_delta=5.0,
    )
    abstained_idea = _make_idea(
        idea_id="abstained-1",
        persona_status="abstained",
        persona_reduction_pp=None,
        llm_estimate_pp=15.0,
        health_delta=4.0,
    )
    # A genuinely scored idea with modest persona result
    scored_idea = _make_idea(
        idea_id="scored-1",
        persona_status="scored",
        persona_reduction_pp=3.0,
        llm_estimate_pp=2.0,
        health_delta=0.5,
    )

    results = scorer.score_many(
        audience=audience,
        ideas=[failed_idea, abstained_idea, scored_idea],
    )

    by_id = {i.idea_id: i for i in results}

    # Failed and abstained collapse to 0.0
    assert by_id["failed-1"].churn_risk_reduction_pp == 0.0
    assert by_id["abstained-1"].churn_risk_reduction_pp == 0.0

    # Scored idea retains its persona value
    assert by_id["scored-1"].churn_risk_reduction_pp == 3.0

    # top_three: scored_idea must rank above failed and abstained
    top = scorer.top_three(results)
    assert top[0].idea_id == "scored-1"

    # Enrich winner reasoning on a failed idea surfaces unavailable/abstained state
    enriched_failed = scorer.enrich_winner_reasoning(
        audience=audience, winner=by_id["failed-1"]
    )
    # Either rationale or risk_reasoning must mention unavailability
    combined = enriched_failed.rationale + enriched_failed.risk_reasoning
    assert any(
        token in combined.lower()
        for token in ("unavailable", "abstained", "failed", "not scored")
    ), f"Expected unavailability mention. Got rationale={enriched_failed.rationale!r}, risk={enriched_failed.risk_reasoning!r}"


# ---------------------------------------------------------------------------
# Test 4: idea_category and outreach_action_type are preserved by score_many
# ---------------------------------------------------------------------------

def test_score_many_preserves_category_and_outreach_type():
    """score_many must not drop idea_category or outreach_action_type."""
    scorer = ActionIdeaScorer()
    audience = _make_audience()

    ideas_raw = [
        _make_idea(
            idea_id="cat-1",
            idea_category="upsell",
            outreach_action_type="sms",
            persona_status="scored",
            persona_reduction_pp=6.0,
        ),
        _make_idea(
            idea_id="cat-2",
            idea_category="reactivation",
            outreach_action_type="in_app",
            persona_status="abstained",
            persona_reduction_pp=None,
        ),
    ]

    results = scorer.score_many(audience=audience, ideas=ideas_raw)
    by_id = {i.idea_id: i for i in results}

    assert by_id["cat-1"].idea_category == "upsell"
    assert by_id["cat-1"].outreach_action_type == "sms"
    assert by_id["cat-2"].idea_category == "reactivation"
    assert by_id["cat-2"].outreach_action_type == "in_app"


# ---------------------------------------------------------------------------
# Test 5: Coverage math, suppression floor, and applicability stamping
# ---------------------------------------------------------------------------


def _audience_with_states(states):
    # states: list of values for factor "online_booking_status"
    rows = [
        OrgUuidEvidence(
            org_uuid=f"org-{i}",
            org_id=str(i),
            matched_factors={"online_booking_status": s},
            evidence_source="extract",
            observed_at="2026-06-20T00:00:00Z",
        )
        for i, s in enumerate(states)
    ]
    return _make_audience(org_uuid_evidence=rows)


def test_empty_condition_is_full_coverage():
    idea = _make_idea(applicability_condition={})
    aud = _audience_with_states(["active", "inactive"])
    from pathfinder.action_console.scoring import applicable_coverage_fraction
    assert applicable_coverage_fraction(idea, aud) == 1.0


def test_partial_condition_coverage_fraction():
    idea = _make_idea(applicability_condition={"online_booking_status": "inactive"})
    aud = _audience_with_states(["active", "active", "active", "active", "inactive"])
    from pathfinder.action_console.scoring import applicable_coverage_fraction
    assert applicable_coverage_fraction(idea, aud) == 0.2


def test_unknown_when_factor_absent_from_extract():
    idea = _make_idea(applicability_condition={"nonexistent_factor": "x"})
    aud = _audience_with_states(["active", "inactive"])
    from pathfinder.action_console.scoring import applicable_coverage_fraction
    assert applicable_coverage_fraction(idea, aud) is None


def test_is_suppressed_below_floor():
    idea = _make_idea(applicable_coverage=0.2)
    from pathfinder.action_console.scoring import is_suppressed
    suppressed, reason = is_suppressed(idea)
    assert suppressed is True
    assert "20" in reason


def test_is_suppressed_at_or_above_floor_not_suppressed():
    from pathfinder.action_console.scoring import (
        COVERAGE_SUPPRESSION_FLOOR,
        is_suppressed,
    )
    idea = _make_idea(applicable_coverage=COVERAGE_SUPPRESSION_FLOOR)
    assert is_suppressed(idea)[0] is False


def test_is_suppressed_unknown_coverage_neutral():
    idea = _make_idea(applicable_coverage=None)
    from pathfinder.action_console.scoring import is_suppressed
    assert is_suppressed(idea)[0] is False


def test_is_suppressed_breadth_false():
    idea = _make_idea(applicable_coverage=1.0, breadth_ok=False, breadth_reason="assumes month 1")
    from pathfinder.action_console.scoring import is_suppressed
    suppressed, reason = is_suppressed(idea)
    assert suppressed is True
    assert reason == "assumes month 1"


def test_is_suppressed_block_kind_per_pro_data():
    idea = _make_idea(
        applicable_coverage=1.0, breadth_ok=False,
        breadth_block_kind="per_pro_data", breadth_reason="needs each Pro's AR balance",
    )
    from pathfinder.action_console.scoring import is_suppressed
    suppressed, reason = is_suppressed(idea)
    assert suppressed is True
    assert reason == "needs each Pro's AR balance"


def test_is_suppressed_block_kind_concentration():
    idea = _make_idea(
        applicable_coverage=1.0, breadth_ok=False,
        breadth_block_kind="concentration", breadth_reason="only team accounts",
    )
    from pathfinder.action_console.scoring import is_suppressed
    assert is_suppressed(idea)[0] is True


def test_is_suppressed_framing_is_not_suppressed():
    # Framing is a fixable-in-post copy note, not a hard block — even though
    # breadth_ok is False, the concept fits the whole audience.
    idea = _make_idea(
        applicable_coverage=1.0, breadth_ok=False,
        breadth_block_kind="framing", breadth_reason="copy says 'you're solo'",
    )
    from pathfinder.action_console.scoring import is_suppressed
    assert is_suppressed(idea)[0] is False


def test_is_suppressed_block_kind_none_not_suppressed():
    idea = _make_idea(
        applicable_coverage=1.0, breadth_ok=True, breadth_block_kind="none",
    )
    from pathfinder.action_console.scoring import is_suppressed
    assert is_suppressed(idea)[0] is False


def test_is_suppressed_legacy_breadth_false_without_kind_still_suppressed():
    # Ideas judged by the old critic (or stored before this change) have no
    # block_kind — the legacy breadth_ok=False path must still bench them.
    idea = _make_idea(
        applicable_coverage=1.0, breadth_ok=False,
        breadth_block_kind=None, breadth_reason="legacy narrow",
    )
    from pathfinder.action_console.scoring import is_suppressed
    assert is_suppressed(idea)[0] is True


def test_is_suppressed_framing_below_coverage_floor_still_suppressed():
    # The coverage floor is independent of block_kind: a framing idea with a
    # declared low-coverage condition is still suppressed on coverage grounds.
    idea = _make_idea(
        applicable_coverage=0.2, breadth_ok=False, breadth_block_kind="framing",
    )
    from pathfinder.action_console.scoring import is_suppressed
    suppressed, reason = is_suppressed(idea)
    assert suppressed is True
    assert "20" in reason


def test_stamp_applicability_sets_fraction():
    idea = _make_idea(applicability_condition={"online_booking_status": "inactive"})
    aud = _audience_with_states(["active", "inactive"])
    from pathfinder.action_console.scoring import stamp_applicability
    stamped = stamp_applicability(idea, aud)
    assert stamped.applicable_coverage == 0.5


# ---------------------------------------------------------------------------
# Test 6: Ranking honors suppression + coverage tie-break
# ---------------------------------------------------------------------------


def _scored(idea_id, *, score, coverage=None, breadth_ok=None):
    return _make_idea(
        idea_id=idea_id,
        persona_status="scored",
        persona_reduction_pp=score,
        churn_risk_reduction_pp=score,
        generation_source="llm_grounded",
        applicable_coverage=coverage,
        breadth_ok=breadth_ok,
    )


def test_low_coverage_idea_suppressed_from_top():
    good = _scored("broad", score=3.0, coverage=0.9)
    narrow = _scored("narrow", score=9.0, coverage=0.2)  # higher score, sub-floor
    ranked = ranked_ideas([narrow, good])
    assert ranked[0].idea_id == "broad"
    assert ranked[-1].idea_id == "narrow"


def test_breadth_false_suppressed_from_top():
    good = _scored("broad", score=3.0, coverage=1.0)
    narrow = _scored("narrow", score=9.0, coverage=1.0, breadth_ok=False)
    ranked = ranked_ideas([narrow, good])
    assert ranked[0].idea_id == "broad"


def test_coverage_breaks_ties_below_score():
    a = _scored("a", score=5.0, coverage=0.6)
    b = _scored("b", score=5.0, coverage=0.95)
    ranked = ranked_ideas([a, b])
    assert ranked[0].idea_id == "b"  # equal score, higher coverage wins


def test_unknown_coverage_is_neutral_not_suppressed():
    known_broad = _scored("broad", score=5.0, coverage=1.0)
    unknown = _scored("unknown", score=6.0, coverage=None)
    ranked = ranked_ideas([known_broad, unknown])
    assert ranked[0].idea_id == "unknown"  # neutral coverage + higher score


def test_all_suppressed_still_yields_a_winner():
    n1 = _scored("n1", score=4.0, coverage=0.1)
    n2 = _scored("n2", score=8.0, coverage=0.2)
    top = top_three_ranked([n1, n2])
    assert len(top) == 2
    assert top[0].idea_id == "n2"  # least-bad by score, honest last resort


def test_score_generated_ideas_stamps_coverage():
    idea = _make_idea(applicability_condition={"online_booking_status": "inactive"})
    aud = _audience_with_states(["active", "active", "active", "inactive"])
    from pathfinder.action_console.scoring import score_generated_ideas
    out = score_generated_ideas(audience=aud, ideas=[idea])
    assert out[0].applicable_coverage == 0.25


# ---------------------------------------------------------------------------
# Test 7: Scorer and free-function ranking parity
# ---------------------------------------------------------------------------


def test_scorer_and_free_function_agree():
    ideas = [
        _scored("a", score=5.0, coverage=0.9),
        _scored("b", score=8.0, coverage=0.2),
        _scored("c", score=6.0, coverage=None),
    ]
    assert [i.idea_id for i in ActionIdeaScorer().top_three(ideas)] == \
           [i.idea_id for i in top_three_ranked(ideas)]


# ---------------------------------------------------------------------------
# Test 8: Org-mode suppression (grounding verdict, skips breadth/coverage)
# ---------------------------------------------------------------------------


def test_org_mode_ungrounded_is_suppressed():
    from pathfinder.action_console.scoring import is_suppressed

    idea = _make_idea(
        audience_mode="org",
        grounding_block_kind="ungrounded",
        grounding_reason="states an AR balance not in the context pack",
    )
    suppressed, reason = is_suppressed(idea)
    assert suppressed is True
    assert "AR balance" in reason


def test_org_mode_generic_and_unjudged_pass():
    from pathfinder.action_console.scoring import is_suppressed

    for kind in ("generic", "none", None):
        idea = _make_idea(audience_mode="org", grounding_block_kind=kind)
        assert is_suppressed(idea) == (False, "")


def test_org_mode_ignores_breadth_and_coverage_gates():
    from pathfinder.action_console.scoring import is_suppressed

    idea = _make_idea(
        audience_mode="org",
        grounding_block_kind="none",
        breadth_block_kind="per_pro_data",   # would bench a segment idea
        breadth_ok=False,
        applicable_coverage=0.01,            # far below the 0.5 floor
    )
    assert is_suppressed(idea) == (False, "")


def test_segment_mode_suppression_unchanged():
    from pathfinder.action_console.scoring import is_suppressed

    benched = _make_idea(breadth_block_kind="per_pro_data", breadth_reason="needs per-Pro data")
    assert is_suppressed(benched)[0] is True
    low_cov = _make_idea(applicable_coverage=0.2)
    assert is_suppressed(low_cov)[0] is True
