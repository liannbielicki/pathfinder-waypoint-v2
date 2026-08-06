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
    assert PROMPT_VERSION == "waypoint_v1"


def test_fenced_context_wraps_untrusted_input() -> None:
    fenced = fenced_context('{"org_id": "org_1"}')
    assert fenced.startswith(UNTRUSTED_START)
    assert fenced.endswith(UNTRUSTED_END)


def test_generator_prompt_fences_org_context_and_keeps_layer_split() -> None:
    prompt = generator_prompt(org_context='{"open_due_usd": "430.25"}', count=3)
    assert UNTRUSTED_START in prompt and UNTRUSTED_END in prompt
    # The two-layer rule from the frozen legacy prompt survives the port.
    assert "pro_facing_concept" in prompt
    assert "manager_rationale" in prompt
    # Internal jargon stays banned from the pro-facing layer.
    assert "churn" in prompt


def test_critic_prompt_fences_untrusted_ideas() -> None:
    prompt = critic_prompt(org_context="{}", ideas_json="[]")
    assert UNTRUSTED_START in prompt
    assert "ungrounded" in prompt


def test_recommendation_is_structured_not_preformatted_prose() -> None:
    value = Recommendation.model_validate(RECOMMENDATION_FIXTURE)
    assert value.mechanism == "invoice_delivery"
    assert value.actions == ["send_open_invoices"]
