from waypoint.catalog import CATALOG, _first_sentence, feature_context
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


def test_feature_context_resolves_union_and_marks_top_unused():
    brief = _brief(
        feature_voip_state="attached_unused",
        top_unused_paid_feature="wisetack",
    )
    block = feature_context(brief, feasibility=False)
    assert "wisetack" in block and "TOP UNUSED PAID FEATURE" in block
    assert "voip (state: attached_unused" in block  # state passed verbatim


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
