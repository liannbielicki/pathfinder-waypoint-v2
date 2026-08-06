"""Interpretation guidance for the org context pack — the "when to apply" half.

The v2 pack carries 29 enums. Enums alone repeat v1's mistake at higher
resolution: ``feature_adoption_band = 'high'`` is a true statement that steers
straight into the wrong play, because a COUNT of attached add-ons cannot tell
"adopted nine" from "bought nine, uses three". The fix is not another Snowflake
column. It is telling the model what each value MEANS and when it should drive
the message.

This module is that guidance, and nothing else. Three rules keep it honest:

1. **It asserts no new facts.** Every line is a reading instruction for a value
   already in the pack. Nothing here can make the grounding critic's job
   harder, because nothing here is a claim about the Pro.
2. **It is data-driven and emitted only for fields actually present.** A pack
   missing ``open_ar_band`` gets no AR guidance, so the prompt does not grow a
   legend for factors we cannot see.
3. **It lives here, not in SQL.** Guidance is prose about a vocabulary, not a
   per-org measurement; putting it in the query would ship the same sentence
   926,837 times and put free text through the field contract.

The cross-field notes are the part that carries real weight. The single most
important is ``recommended_focus == top_unused_paid_feature``: the model's
top-ranked opportunity is a feature the Pro ALREADY PAYS FOR. Read without that
note, the pack invites an upsell pitch for something already on the invoice.
"""
from __future__ import annotations

from typing import Any

from pathfinder.action_console.org_context_contract import (
    PER_FEATURE_STATE_FEATURES,
    PER_FEATURE_STATE_FIELDS,
)

# The shared vocabulary of every feature_<name>_state field. Emitted once, not
# ten times, because it is one domain.
FEATURE_STATE_GUIDANCE: dict[str, str] = {
    "not_attached": (
        "they do not have this feature. If it is also the recommended_focus, "
        "this is a genuine new-value opportunity."
    ),
    "attached_unused": (
        "they ALREADY PAY for this and are not using it. Lead with activation "
        "help, never a pitch — offering to sell what is already on their "
        "invoice reads as not knowing the account."
    ),
    "attached_active": (
        "attached and actively used in the last 90 days. Do not pitch it; it "
        "is a proof point you can build the touch on."
    ),
    "attached_usage_unknown": (
        "attached, but we have NO usage signal for it. Never assert whether "
        "they use it. This may motivate a question, never a stated fact."
    ),
}

# One short line per non-feature field. Keyed by field name; a field absent from
# the pack contributes nothing.
FIELD_GUIDANCE: dict[str, str] = {
    "feature_adoption_band": (
        "COUNT of paid add-ons attached — it says NOTHING about usage. Never "
        "read 'high' as 'well adopted'; the feature_*_state fields are the "
        "only authority on whether anything is actually used."
    ),
    "plan_gap_band": (
        "size of the unrealized value gap on their current plan. Motivates "
        "how ambitious the ask can be, not a number to quote."
    ),
    "ltv_score_band": (
        "their lifetime value against all Pros, in quartiles. Informs how much "
        "effort the touch is worth; never mention a score or ranking to them."
    ),
    "open_ar_band": (
        "how much they are owed by their own customers, banded. A real "
        "operational pain you can help with — never quote a figure."
    ),
    "ar_aging_band": (
        "age of the oldest OVERDUE invoice. 'past_30' means 1-30 days past "
        "due, NOT more than 30 days; 'past_90_plus' is the severe case. "
        "'current' means nothing is overdue."
    ),
    "jobs_created_28d_band": (
        "job volume over 28 days. '0' with a paid plan is a disengagement "
        "signal, not a reason to pitch more features."
    ),
    "estimates_created_28d_band": "estimate volume over 28 days.",
    "invoices_sent_28d_band": "invoice volume over 28 days.",
    "outreach_count_28d_band": (
        "how many times WE have contacted them in 28 days. 'over_20' means "
        "they have been contacted heavily: be brief, and do not re-pitch."
    ),
    "sms_consent_state": (
        "'opted_out' means SMS is unavailable — never choose sms. "
        "'never_asked' means no consent exists yet, so SMS needs opt-in first."
    ),
    "email_consent_state": (
        "'opted_out' means email is unavailable — never choose email. There is "
        "no positive email opt-in signal at HCP, so 'never_asked' is the "
        "normal state and does not block email."
    ),
    "recommended_focus": (
        "the single feature the LTV model ranks first for this Pro. Use it to "
        "choose the SUBJECT of the touch. Always check its "
        "feature_<name>_state before deciding whether the play is activation "
        "or new value."
    ),
    "recommended_focus_value_band": (
        "how large that top gap is, in quartiles of every Pro with a positive "
        "gap. 'none'/'low' means do not build the whole touch around it; "
        "'high'/'very_high' means it is worth leading with."
    ),
    "recommended_focus_retention_lift_band": (
        "expected retention lift from closing that gap. 'high'/'very_high' is "
        "the strongest retention case available for this Pro — but that "
        "reasoning belongs in manager_rationale, never in what the Pro reads."
    ),
    "top_unused_paid_feature": (
        "the highest-value feature they already pay for and do not use. When "
        "present this is usually the best available play: activation of "
        "something already bought beats introducing something new."
    ),
    "vertical": (
        "their trade, mapped to a fixed vocabulary. Use it for concrete "
        "job-level language ('pre-season tune-ups' for hvac). 'other' means "
        "the source trade did not map — stay trade-neutral."
    ),
    "plan_tier": (
        "their subscription tier: basic < essentials < max < max+ < max++. "
        "Never propose an action that requires a tier above theirs."
    ),
    "org_size_band": (
        "'owner_operator' is a solo Pro doing the work themselves — keep asks "
        "to minutes and assume no back office. '2_9' and '10_plus' have staff "
        "who can absorb setup work."
    ),
    "tenure_band": (
        "how long they have been enrolled. 'under_1y' means onboarding habits "
        "are still forming; 'over_4y' means long-standing habits and a "
        "credible relationship to reference."
    ),
}


def _feature_from_field(field: str) -> str:
    return field[len("feature_") : -len("_state")]


def _cross_field_notes(known: dict[str, str]) -> list[str]:
    """Notes that only exist as a RELATION between two pack values.

    These are the ones a per-field legend cannot express, and they are where
    the v1 misreading actually gets corrected.
    """
    notes: list[str] = []

    focus = (known.get("recommended_focus") or "").strip()
    top_unused = (known.get("top_unused_paid_feature") or "").strip()
    focus_state = known.get(f"feature_{focus}_state") if focus else None

    if focus and top_unused and focus == top_unused:
        notes.append(
            f"recommended_focus ({focus}) is the SAME feature as "
            f"top_unused_paid_feature: they already pay for it and are not "
            "using it. This is an activation play, not a pitch. Do not offer "
            "to sell it to them."
        )
    elif focus_state == "attached_unused":
        notes.append(
            f"recommended_focus ({focus}) is already attached and unused — "
            "help them turn it on rather than introducing it as new."
        )
    elif focus_state == "not_attached":
        notes.append(
            f"recommended_focus ({focus}) is not attached, so it is genuinely "
            "new to them. Weigh recommended_focus_value_band before leading "
            "the whole touch with it."
        )
    elif focus_state == "attached_usage_unknown":
        notes.append(
            f"recommended_focus ({focus}) is attached but we cannot see "
            "whether they use it. Ask; do not assert either way."
        )

    unused = sorted(
        _feature_from_field(field)
        for field in PER_FEATURE_STATE_FIELDS
        if known.get(field) == "attached_unused"
    )
    adoption = known.get("feature_adoption_band")
    if unused and adoption in {"medium", "high"}:
        notes.append(
            f"feature_adoption_band is '{adoption}' but "
            f"{len(unused)} attached feature(s) sit UNUSED "
            f"({', '.join(unused)}). Do not describe this Pro as a strong "
            "adopter: they are paying for capability they have not switched "
            "on, and that gap is the opportunity."
        )
    elif unused:
        notes.append(
            "attached but unused, so already paid for: " + ", ".join(unused)
        )

    if known.get("outreach_count_28d_band") == "over_20":
        notes.append(
            "They have received over 20 touches in 28 days. Keep this short "
            "and make it useful; another pitch will land badly."
        )

    blocked = [
        channel
        for channel, field in (("sms", "sms_consent_state"), ("email", "email_consent_state"))
        if known.get(field) == "opted_out"
    ]
    if blocked:
        notes.append(
            "Channel unavailable (opted out): "
            + ", ".join(blocked)
            + ". Do not choose it as recommended_channel."
        )

    return notes


def pack_semantics(pack: dict[str, Any]) -> dict[str, Any]:
    """Build the interpretation block for one context pack.

    Returns only what applies: guidance for fields actually present in
    ``pack["known"]``, the feature-state vocabulary only if at least one
    per-feature field is present, and the cross-field notes this pack earns.
    An empty pack yields an empty dict, and the prompt then omits the block
    entirely rather than printing a legend for nothing.
    """
    known_raw = (pack or {}).get("known") or {}
    known = {str(k): "" if v is None else str(v).strip() for k, v in known_raw.items()}

    fields = {
        name: text
        for name, text in FIELD_GUIDANCE.items()
        if known.get(name)
    }

    out: dict[str, Any] = {}
    if any(known.get(field) for field in PER_FEATURE_STATE_FIELDS):
        out["feature_state_meanings"] = dict(FEATURE_STATE_GUIDANCE)
    if fields:
        out["field_meanings"] = fields
    notes = _cross_field_notes(known)
    if notes:
        out["notes_for_this_pro"] = notes
    return out


__all__ = [
    "FEATURE_STATE_GUIDANCE",
    "FIELD_GUIDANCE",
    "PER_FEATURE_STATE_FEATURES",
    "pack_semantics",
]
