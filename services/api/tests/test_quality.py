"""Deterministic concept-quality gate (v5 spike)."""

import json
from pathlib import Path

from waypoint.models import Recommendation
from waypoint.n8n import OrgBrief
from waypoint.quality import PASS_SCORE, evaluate

FIXTURE = Path(__file__).parent / "fixtures" / "n8n_context.json"


def _brief(index: int = 0) -> OrgBrief:
    orgs = json.loads(FIXTURE.read_text())["organizations"]
    return OrgBrief.model_validate(orgs[index])


def _idea(**overrides) -> Recommendation:
    base = {
        "title": "Turn on online booking",
        "mechanism": "online_booking_activation",
        "actions": ["Text a one-tap link that switches online booking on"],
        "pro_facing_concept": (
            "Your online booking page is attached but unused — one tap turns it on so "
            "hvac jobs can be booked while you are on a roof."
        ),
        "manager_rationale": "Pro has online_booking attached_unused; it reduces churn risk.",
        "channel": "sms",
        "risk": "low",
    }
    return Recommendation.model_validate({**base, **overrides})


def test_grounded_single_ask_concept_passes():
    result = evaluate(_idea(), _brief())
    assert result.passed, result
    assert result.score >= PASS_SCORE


def test_internal_jargon_on_pro_facing_surface_is_a_hard_block():
    result = evaluate(_idea(pro_facing_concept="We noticed your churn risk is high."), _brief())
    assert result.block_kind == "internal_jargon"
    assert not result.passed


def test_manager_rationale_may_say_churn():
    # The baseline idea's rationale already says "churn"; grading it would
    # bench every well-reasoned idea.
    assert evaluate(_idea(), _brief()).block_kind is None


def test_consent_ask_is_a_hard_block():
    result = evaluate(_idea(pro_facing_concept="Can we get your opt-in to text you?"), _brief())
    assert result.block_kind == "consent_ask"


def test_near_duplicate_of_a_prior_touch_is_blocked():
    idea = _idea()
    result = evaluate(idea, _brief(), prior_concepts=[idea.pro_facing_concept])
    assert result.block_kind == "repetition"


def test_ungrounded_boilerplate_scores_below_the_floor():
    result = evaluate(
        _idea(
            pro_facing_concept="Check out some helpful tips to grow your business today.",
            actions=["Open the tips page", "Watch a video", "Reply with questions"],
        ),
        _brief(),
    )
    assert not result.passed
    assert result.dimensions["grounded_in_brief"] < 1.0


def test_oversized_sms_concept_loses_channel_fit():
    result = evaluate(_idea(pro_facing_concept="Book more hvac jobs. " * 40), _brief())
    assert result.dimensions["channel_fit"] < 1.0


def test_same_input_scores_identically():
    a, b = evaluate(_idea(), _brief()), evaluate(_idea(), _brief())
    assert a == b


def test_pro_without_brief_signals_fails_open_on_grounding():
    # pro_2 has fewer populated bands; grounding must not punish our data gap
    # into a block.
    assert evaluate(_idea(), _brief(1)).block_kind is None
