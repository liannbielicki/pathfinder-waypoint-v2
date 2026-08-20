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
    assert PROMPT_VERSION == "waypoint_v4"


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


def test_channel_directive_forbids_sms_consent_asks() -> None:
    from waypoint.prompts import channel_directive

    # Any run that can deliver over SMS carries the no-consent-ask rule.
    assert "consent" in channel_directive(["sms"])
    assert "consent" in channel_directive(["sms", "email"])
    assert "consent" not in channel_directive(["email"])


def test_critic_prompt_fences_untrusted_ideas() -> None:
    prompt = critic_prompt(org_context="{}", ideas_json="[]")
    assert UNTRUSTED_START in prompt
    assert "ungrounded" in prompt


def test_critic_prompt_hard_blocks_consent_asks() -> None:
    prompt = critic_prompt(org_context="{}", ideas_json="[]")
    assert "consent_ask" in prompt


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
    assert "exactly 1" in prompt
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


def test_evolve_prompt_batch_demands_distinct_mechanisms() -> None:
    from waypoint.prompts import evolve_prompt

    prompt = evolve_prompt(
        "{}",
        mode="shift",
        best_json=None,
        history_json="[]",
        tried_mechanisms=["invoice_delivery"],
        channels=["sms", "email"],
        journey_window="churn_risk",
        evidence="No historical outcome evidence is available for this journey window yet.",
        count=3,
    )
    assert "exactly 3" in prompt
    assert "JSON array" in prompt
    assert "distinct" in prompt.lower()
    # Fences + grounding + two-layer split all survive at count > 1.
    assert UNTRUSTED_START in prompt and UNTRUSTED_END in prompt
    assert "GROUNDING" in prompt
    assert "pro_facing_concept" in prompt
    assert "manager_rationale" in prompt


def test_evolve_prompt_stay_batch_refines_first_and_diversifies_rest() -> None:
    from waypoint.prompts import evolve_prompt

    prompt = evolve_prompt(
        "{}",
        mode="stay",
        best_json=BEST,
        history_json=HISTORY,
        tried_mechanisms=["invoice_delivery"],
        channels=["sms"],
        journey_window="churn_risk",
        evidence="No historical outcome evidence is available for this journey window yet.",
        count=3,
    )
    assert "FIRST idea" in prompt
    assert "DIFFERENT grounded mechanism" in prompt
    assert "exactly 3" in prompt


def test_ranker_prompt_fences_candidates_and_states_output_contract() -> None:
    from waypoint.prompts import ranker_prompt

    prompt = ranker_prompt(
        org_context="{}",
        candidates_json='[{"candidate_id": "c1"}, {"candidate_id": "c2"}]',
        journey_window="churn_risk",
        evidence="- invoice_delivery via sms: 4 sent, 7d return 2/3",
    )
    assert UNTRUSTED_START in prompt and UNTRUSTED_END in prompt
    assert "c1" in prompt and "c2" in prompt
    assert "churn_risk" in prompt
    assert "invoice_delivery via sms" in prompt
    assert "exactly once" in prompt
    assert "0-1 scale" in prompt
    assert '"tie": bool' in prompt
    assert "EXPLICIT" in prompt


def test_war_game_prompt_demands_bounded_branches() -> None:
    from waypoint.prompts import war_game_prompt

    prompt = war_game_prompt("{}", '{"title": "t"}', ["sms"])
    for branch in ("on_return", "on_click_no_use", "on_no_interaction", "on_negative"):
        assert branch in prompt
    assert "stop" in prompt
