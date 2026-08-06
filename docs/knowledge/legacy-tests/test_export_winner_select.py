from __future__ import annotations

from pathfinder.action_console.models import GeneratedIdea
from pathfinder.export.winner_select import WinnerRow, select_winner, select_winners


def make_idea(**overrides) -> GeneratedIdea:
    # Defaults describe a genuinely SUPPORTED idea (scored, CI entirely above
    # zero, in calibrated range, never subjected to a not-confirmed downgrade)
    # so that a bare make_idea() is, by default, a legitimate export candidate
    # under the confidence gate. Tests that need an abstained/provisional/
    # not_confirmed idea override the relevant fields explicitly.
    base = dict(
        run_id="r1", idea_id="i1", test_number=1, title="Send estimate follow-ups",
        prose_action="Do X", outreach_action_type="email_campaign",
        idea_category="Workflow Activation", generation_source="llm_grounded",
        source_kind="grounded", llm_estimate_pp=1.0, health_delta=0.0,
        churn_risk_reduction_pp=1.0, persona_reduction_pp=1.0,
        persona_ci_lower=0.5, persona_ci_upper=2.0, persona_panel_n=12,
        in_calibrated_range=True, persona_status="scored", persona_error=None,
        historical_corroboration={}, rationale="r", risk_reasoning="rr",
        evidence_summary="es", exact_org_uuid_sample=[], final_choice_panel=[],
        created_at="2026-07-28T00:00:00+00:00", audience_mode="org",
    )
    base.update(overrides)
    return GeneratedIdea(**base)


def idea_row(idea: GeneratedIdea) -> dict:
    return {"run_id": idea.run_id, "idea_id": idea.idea_id, "payload": idea.to_dict()}


def make_run_row(run_id="r1", status="completed", n_orgs=1) -> dict:
    evidence = [
        {"org_uuid": f"uu-{i}", "org_id": str(100 + i), "matched_factors": {},
         "evidence_source": "extract", "observed_at": "2026-07-28"}
        for i in range(n_orgs)
    ]
    return {"run_id": run_id, "status": status,
            "audience": {"org_uuid_evidence": evidence}}


def test_missing_run_row_is_run_failed():
    row = select_winner("r9", None, [])
    assert row.status == "run_failed" and row.run_id == "r9"


def test_incomplete_run_is_run_failed():
    row = select_winner("r1", make_run_row(status="canceled"), [])
    assert row.status == "run_failed"


def test_multi_org_run_is_not_org_mode_and_never_guesses():
    row = select_winner("r1", make_run_row(n_orgs=3), [idea_row(make_idea())])
    assert row.status == "not_org_mode"
    assert row.org_id == "" and row.theme == ""


def test_all_suppressed_is_no_winner_but_keeps_org():
    ideas = [idea_row(make_idea(grounding_block_kind="ungrounded"))]
    row = select_winner("r1", make_run_row(), ideas)
    assert row.status == "no_winner"
    assert row.org_id == "100" and row.org_uuid == "uu-0"


def test_winner_uses_canonical_rank_scored_beats_higher_llm_estimate():
    scored = make_idea(idea_id="a", title="Scored winner",
                       persona_status="scored", persona_reduction_pp=2.0,
                       churn_risk_reduction_pp=2.0)
    unscored = make_idea(idea_id="b", title="Loud unscored",
                         llm_estimate_pp=9.0, churn_risk_reduction_pp=9.0,
                         persona_status="abstained", persona_reduction_pp=None,
                         persona_ci_lower=None, persona_ci_upper=None)
    row = select_winner("r1", make_run_row(), [idea_row(unscored), idea_row(scored)])
    assert row.status == "ready_for_review"
    assert row.theme == "Scored winner"
    assert row.theme_category == "Workflow Activation"


def test_suppressed_ideas_are_excluded_not_just_ranked_low():
    blocked = make_idea(idea_id="a", title="Blocked", persona_status="scored",
                        persona_reduction_pp=9.0, churn_risk_reduction_pp=9.0,
                        grounding_block_kind="ungrounded")
    ok = make_idea(idea_id="b", title="Clean")
    row = select_winner("r1", make_run_row(), [idea_row(blocked), idea_row(ok)])
    assert row.theme == "Clean"


def test_third_place_captured_when_three_candidates():
    a = make_idea(idea_id="a", title="First", churn_risk_reduction_pp=3.0)
    b = make_idea(idea_id="b", title="Second", churn_risk_reduction_pp=2.0)
    c = make_idea(idea_id="c", title="Third", churn_risk_reduction_pp=1.0,
                  idea_category="Pricing")
    row = select_winner("r1", make_run_row(), [idea_row(x) for x in (a, b, c)])
    assert row.theme == "First"
    assert row.third_theme == "Third" and row.third_theme_category == "Pricing"


def test_third_place_blank_when_fewer_than_three():
    row = select_winner("r1", make_run_row(), [idea_row(make_idea())])
    assert row.status == "ready_for_review" and row.third_theme == ""


def test_malformed_payload_skipped_not_fatal():
    good = idea_row(make_idea(title="Good"))
    row = select_winner("r1", make_run_row(), [{"payload": {"junk": 1}}, good])
    assert row.theme == "Good"


class FakeSink:
    def __init__(self, runs, ideas):
        self.runs, self.ideas = runs, ideas

    def get_action_run(self, run_id):
        return self.runs.get(run_id)

    def list_action_generated_ideas(self, run_id, limit=500):
        return self.ideas.get(run_id)


def test_select_winners_keeps_input_order_and_isolates_failures():
    sink = FakeSink(
        runs={"r1": make_run_row("r1"), "r2": None},
        ideas={"r1": [idea_row(make_idea())], "r2": None},
    )
    rows = select_winners(sink, ["r1", "r2"])
    assert [r.run_id for r in rows] == ["r1", "r2"]
    assert rows[0].status == "ready_for_review"
    assert rows[1].status == "run_failed"


def test_select_winners_ideas_fetch_failure_is_run_failed():
    sink = FakeSink(runs={"r1": make_run_row("r1")}, ideas={"r1": None})
    assert select_winners(sink, ["r1"])[0].status == "run_failed"


# ---------------------------------------------------------------------------
# Confidence gate: the send path must honor support_tier_for_idea, the same
# verdict the display path already applies. A "not_confirmed" idea kept its
# ORIGINAL (often inflated) search-panel score -- exactly why it must never
# rank its way into an export.
# ---------------------------------------------------------------------------


def test_not_confirmed_idea_that_would_rank_first_is_not_selected():
    not_confirmed = make_idea(
        idea_id="a", title="Lucky search-panel number",
        churn_risk_reduction_pp=9.0, persona_reduction_pp=9.0,
        confirmation_status="not_confirmed",
    )
    row = select_winner("r1", make_run_row(), [idea_row(not_confirmed)])
    assert row.status == "no_winner"


def test_supported_idea_beats_higher_scoring_not_confirmed_idea():
    not_confirmed = make_idea(
        idea_id="a", title="Lucky search-panel number",
        churn_risk_reduction_pp=9.0, persona_reduction_pp=9.0,
        confirmation_status="not_confirmed",
    )
    supported = make_idea(
        idea_id="b", title="Sober confirmed number",
        churn_risk_reduction_pp=2.0, persona_reduction_pp=2.0,
    )
    row = select_winner(
        "r1", make_run_row(), [idea_row(not_confirmed), idea_row(supported)]
    )
    assert row.status == "ready_for_review"
    assert row.theme == "Sober confirmed number"


def test_only_provisional_or_unsupported_candidates_yield_no_winner():
    provisional = make_idea(
        idea_id="a", title="CI crosses zero", persona_ci_lower=-1.0,
    )
    unsupported = make_idea(
        idea_id="b", title="Never scored",
        persona_status="abstained", persona_reduction_pp=None,
        persona_ci_lower=None, persona_ci_upper=None,
    )
    row = select_winner(
        "r1", make_run_row(), [idea_row(provisional), idea_row(unsupported)]
    )
    assert row.status == "no_winner"


def test_genuinely_supported_idea_still_exports_like_before():
    idea = make_idea(title="Solid winner")
    row = select_winner("r1", make_run_row(), [idea_row(idea)])
    assert row.status == "ready_for_review"
    assert row.theme == "Solid winner"


def test_support_tier_for_idea_importable_from_scoring_and_live_view():
    from pathfinder.action_console.live_view import (
        support_tier_for_idea as from_live_view,
    )
    from pathfinder.action_console.scoring import (
        support_tier_for_idea as from_scoring,
    )

    assert from_scoring is from_live_view
