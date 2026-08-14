from waypoint.models import Recommendation
from waypoint.prompts import (
    PROMPT_VERSION,
    UNTRUSTED_END,
    UNTRUSTED_START,
    critic_prompt,
    fenced_context,
    generator_prompt,
)

RECOMMENDATION_FIXTURE = {
    "title": "Send open invoices reminder",
    "mechanism": "invoice_delivery",
    "actions": ["send_open_invoices"],
    "pro_facing_concept": "Get paid faster by sending your two open invoices today.",
    "manager_rationale": "Open AR is the strongest churn signal for this org.",
    "channel": "sms",
    "risk": "Pro may have intentionally paused invoicing.",
}


def test_prompt_version_is_pinned() -> None:
    assert PROMPT_VERSION == "waypoint_v2"


def test_fenced_context_wraps_untrusted_input() -> None:
    fenced = fenced_context('{"org_id": "org_1"}')
    assert fenced.startswith(UNTRUSTED_START)
    assert fenced.endswith(UNTRUSTED_END)


def test_generator_prompt_fences_org_context_and_keeps_layer_split() -> None:
    prompt = generator_prompt(
        org_context='{"open_due_usd": "430.25"}', count=3, channels=["sms", "email"]
    )
    assert UNTRUSTED_START in prompt and UNTRUSTED_END in prompt
    # The two-layer rule from the frozen legacy prompt survives the port.
    assert "pro_facing_concept" in prompt
    assert "manager_rationale" in prompt
    # Internal jargon stays banned from the pro-facing layer.
    assert "churn" in prompt


def test_channel_directive_gates_sms_only_with_brevity() -> None:
    from waypoint.prompts import channel_directive

    sms = channel_directive(["sms"])
    assert '"sms"' in sms and '"email"' not in sms
    assert "160" in sms  # SMS brevity constraint is stated
    # Both-channels case gates to the set but adds no SMS brevity rule.
    both = channel_directive(["sms", "email"])
    assert '"sms"' in both and '"email"' in both
    assert "160" not in both


def test_critic_prompt_fences_untrusted_ideas() -> None:
    prompt = critic_prompt(org_context="{}", ideas_json="[]")
    assert UNTRUSTED_START in prompt
    assert "ungrounded" in prompt


def test_recommendation_is_structured_not_preformatted_prose() -> None:
    value = Recommendation.model_validate(RECOMMENDATION_FIXTURE)
    assert value.mechanism == "invoice_delivery"
    assert value.actions == ["send_open_invoices"]


HISTORY = '[{"round": 1, "mechanism": "invoice_delivery", "score_pp": 2.0, "outcome": "win"}]'
BEST = '{"title": "Send open invoices reminder", "mechanism": "invoice_delivery"}'


def test_evolve_prompt_stay_refines_the_best_mechanism() -> None:
    from waypoint.prompts import evolve_prompt

    prompt = evolve_prompt(
        '{"open_due_usd": "430.25"}',
        mode="stay",
        best_json=BEST,
        history_json=HISTORY,
        tried_mechanisms=["invoice_delivery"],
        channels=["sms"],
        journey_window="churn_risk",
        evidence="No historical outcome evidence is available for this journey window yet.",
    )
    assert UNTRUSTED_START in prompt and UNTRUSTED_END in prompt
    assert "160" in prompt  # sms-only run shapes the idea for a brief text
    assert "exactly ONE" in prompt or "exactly one" in prompt
    assert "invoice_delivery" in prompt  # the mechanism being refined
    assert BEST in prompt
    assert HISTORY in prompt
    assert "refine" in prompt.lower()
    # The two-layer + grounding rules survive in the evolve prompt too.
    assert "pro_facing_concept" in prompt
    assert "manager_rationale" in prompt


def test_evolve_prompt_shift_forbids_tried_mechanisms() -> None:
    from waypoint.prompts import evolve_prompt

    prompt = evolve_prompt(
        "{}",
        mode="shift",
        best_json=None,
        history_json="[]",
        tried_mechanisms=["invoice_delivery", "review_requests"],
        channels=["email"],
        journey_window="churn_risk",
        evidence="No historical outcome evidence is available for this journey window yet.",
    )
    assert "forbidden" in prompt.lower()
    assert "invoice_delivery" in prompt and "review_requests" in prompt


def test_search_directive_prompt_is_deleted() -> None:
    from waypoint import prompts

    assert not hasattr(prompts, "search_directive_prompt")


def test_evolve_prompt_carries_window_and_evidence() -> None:
    from waypoint.prompts import evolve_prompt

    prompt = evolve_prompt(
        "{}",
        mode="stay",
        best_json=None,
        history_json="[]",
        tried_mechanisms=[],
        channels=["sms"],
        journey_window="churn_risk",
        evidence="- invoice_delivery via sms: 4 sent, 7d return 2/3",
    )
    assert "churn_risk" in prompt
    assert "invoice_delivery via sms" in prompt


def test_war_game_prompt_demands_bounded_branches() -> None:
    from waypoint.prompts import war_game_prompt

    prompt = war_game_prompt("{}", '{"title": "t"}', ["sms"])
    for branch in ("on_return", "on_click_no_use", "on_no_interaction", "on_negative"):
        assert branch in prompt
    assert "stop" in prompt
