import json

from waypoint import catalog
from waypoint.catalog import (
    CATALOG,
    _first_sentence,
    available_feature_keys,
    feature_context,
    resolve_cta,
    waypoint_context,
)
from waypoint.n8n import OrgBrief


def _brief(**fields) -> OrgBrief:
    return OrgBrief(org_uuid="org-1", **fields)


def test_first_sentence_trims_at_capital_boundary():
    text = "A does X. Distinct from Y and Z."
    assert _first_sentence(text) == "A does X."


def test_first_sentence_ignores_eg_period():
    # "e.g. quarterly" must NOT be treated as a sentence end (lowercase follows).
    text = "Plans (e.g. quarterly tune-ups) sold to a customer. Distinct from a job."
    assert _first_sentence(text) == "Plans (e.g. quarterly tune-ups) sold to a customer."


def test_first_sentence_keeps_unbroken_text():
    assert _first_sentence("no boundary here") == "no boundary here"


def test_catalog_loads_and_groups_multi_row_feature():
    entry = CATALOG["online_booking"]
    assert entry.description.startswith("Lets a customer request or schedule")
    assert entry.description.endswith("office.")  # trimmed to first sentence
    assert len(entry.ctas) >= 2  # primary + replacement CTA rows


def test_resolve_cta_returns_only_a_real_reachable_catalog_destination():
    cta = resolve_cta("online_booking")
    assert cta is not None
    assert cta["label"]
    assert cta["url"]
    assert cta["works_on"] in {"web", "ios", "mobile"}
    assert resolve_cta("payment_processing") is None  # sentinel-only broken row
    assert resolve_cta("sales_proposal") is None  # every destination is explicitly untested
    assert resolve_cta("made_up_feature") is None


def test_feature_context_resolves_union_and_marks_top_unused():
    brief = _brief(
        feature_voip_state="attached_unused",
        top_unused_paid_feature="wisetack",
    )
    block = feature_context(brief, feasibility=False)
    assert "wisetack" in block and "TOP UNUSED PAID FEATURE" in block
    assert "voip (state: attached_unused" in block  # state passed verbatim


def test_unattached_legacy_feature_has_no_direct_destination():
    brief = _brief(
        feature_service_agreements_state="not_attached",
        feature_online_booking_state="attached_unused",
    )

    assert available_feature_keys(brief) == {"online_booking"}
    context = json.loads(waypoint_context(brief, feasibility=False).splitlines()[0])
    assert "service_agreements" not in context["verified_destination_keys"]
    assert context["plan_eligibility"] == "unverified"


def test_unrecognized_attached_state_does_not_authorize_destination():
    brief = _brief(feature_online_booking_state="attached_unverified")
    assert "online_booking" not in available_feature_keys(brief)
    assert "online_booking" not in json.loads(waypoint_context(brief, feasibility=False).splitlines()[0])["verified_destination_keys"]


def test_feature_context_empty_when_no_features():
    assert feature_context(_brief(), feasibility=False) == ""


def test_feature_context_skips_unresolvable_feature():
    # A pointer to a feature absent from the catalog must not crash or emit a line.
    # (Brief used "customer_portal" here, but the packaged CSV has a description
    # for every one of its 26 features, so that key resolves instead of skipping.
    # Swapped to a key genuinely absent from the catalog to keep the test's intent.)
    block = feature_context(_brief(top_unused_paid_feature="loyalty_program"), feasibility=False)
    assert block == ""


def test_feasibility_toggle_changes_payload():
    brief = _brief(feature_voip_state="attached_unused")
    off = feature_context(brief, feasibility=False)
    on = feature_context(brief, feasibility=True)
    assert "reachable on" not in off
    assert "reachable on" in on  # works_on summary only when feasibility=True


def test_feasibility_suffix_omits_sentinel_works_on():
    # payment_processing's only CSV row has works_on=broken, a catalog sentinel,
    # not a real delivery channel — must yield no "reachable on" hint at all.
    brief = _brief(top_unused_paid_feature="payment_processing")
    on = feature_context(brief, feasibility=True)
    assert "payment_processing" in on
    payment_line = next(line for line in on.splitlines() if line.startswith("- payment_processing"))
    assert "reachable on" not in payment_line


def test_waypoint_context_uses_promoted_packet_without_legacy_catalog_dump():
    brief = _brief(
        feature_voip_state="attached_unused",
        curated_context={
            "v": {"jobs_created_t28": 12},
            "pc": {"jobs": {"a": "Jobs", "v": "Manage job workflows."}},
        },
    )

    context = waypoint_context(brief, feasibility=False)

    assert context == '{"feature_topic_keys":[],"pc":{"jobs":{"a":"Jobs","v":"Manage job workflows."}},"v":{"jobs_created_t28":12},"verified_destination_keys":[]}'
    assert "voip" not in context


def test_promoted_product_card_does_not_prove_entitlement():
    brief = _brief(
        feature_voip_state="attached_unused",
        curated_context={"v": {}, "pc": {"online_booking": {"e": "available"}}},
    )

    assert available_feature_keys(brief) == set()


def test_plan_compatible_card_is_a_topic_without_proving_access():
    brief = _brief(curated_context={"v": {}, "pc": {
        "card_on_file": {"e": "available", "v": "Store a customer's payment card."},
        "service_agreements": {"e": "not_in_current_plan", "v": "Offer service plans."},
        "sales_proposal": {"e": "unknown", "v": "Build proposals."},
        "invented_product": {"e": "available"},
    }})

    assert catalog.feature_topic_keys(brief) == {"card_on_file"}
    assert available_feature_keys(brief) == set()
    context = json.loads(waypoint_context(brief, feasibility=False))
    assert context["feature_topic_keys"] == ["card_on_file"]
    assert context["verified_destination_keys"] == []


def test_promoted_direct_feature_requires_attachment_and_plan_eligibility():
    base = {"v": {}, "pc": {"online_booking": {"e": "available"}}}
    brief = _brief(feature_online_booking_state="attached_unused", curated_context=base)
    assert available_feature_keys(brief) == {"online_booking"}
    assert json.loads(waypoint_context(brief, feasibility=False))["verified_destination_keys"] == [
        "online_booking"
    ]

    for card in ({"e": "unknown"}, {"e": "not_in_current_plan"}, None):
        context = {"v": {}, "pc": {"online_booking": card} if card else {}}
        brief = _brief(feature_online_booking_state="attached_unused", curated_context=context)
        assert available_feature_keys(brief) == set()


def test_promoted_paid_feature_needs_explicit_plan_eligibility():
    brief = _brief(
        top_unused_paid_feature="online_booking",
        curated_context={"v": {}, "pc": {"online_booking": {"e": "available"}}},
    )
    assert available_feature_keys(brief) == {"online_booking"}


def test_feature_explicitly_outside_current_plan_cannot_get_direct_cta():
    brief = _brief(feature_online_booking_state="attached_unused", curated_context={
        "v": {"core_saas_plan": "Core SaaS Basic"},
        "pc": {
            "service_agreements": {"e": "not_in_current_plan"},
            "online_booking": {"e": "available"},
        },
    })

    assert available_feature_keys(brief) == {"online_booking"}


def test_context_lists_only_features_with_verified_destinations():
    brief = _brief(feature_online_booking_state="attached_unused", curated_context={
        "v": {},
        "pc": {
            "service_agreements": {"e": "not_in_current_plan"},
            "sales_proposal": {"e": "available"},
            "online_booking": {"e": "available"},
        },
    })

    context = json.loads(waypoint_context(brief, feasibility=False))
    assert "sales_proposal" in context["feature_topic_keys"]
    assert context["verified_destination_keys"] == ["online_booking"]


def test_waypoint_context_keeps_legacy_behavior_without_active_promotion():
    brief = _brief(feature_voip_state="attached_unused")

    context = waypoint_context(brief, feasibility=False)

    facts = json.loads(context.splitlines()[0])
    assert facts["known"]["feature_voip_state"] == "attached_unused"
    assert "invoices_sent_28d_band" in facts["unknown"]
    assert "invoices_sent_28d_band" not in facts["known"]
    assert "voip (state: attached_unused" in context
