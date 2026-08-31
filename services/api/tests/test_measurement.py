"""V3 deterministic measurement selection: no model in the loop.

The catalog is human-owned; selection is a pure function of the winner's
mechanism. Every winner is measurable — the primary objective (app_return)
is always tracked, plus at most one mechanism-mapped metric.
"""

from waypoint.measurement import METRIC_CATALOG, select_indicators


def test_invoice_mechanism_selects_the_invoice_indicator_first() -> None:
    plan = select_indicators("invoice_delivery", METRIC_CATALOG)
    assert [item.key for item in plan.indicators] == ["invoices_sent", "app_return"]
    assert plan.indicators[0].direction == "increase"
    assert plan.indicators[0].window_days == 30


def test_unknown_mechanism_still_measures_the_primary_objective() -> None:
    plan = select_indicators("quantum_alignment", METRIC_CATALOG)
    assert [item.key for item in plan.indicators] == ["app_return"]


def test_every_mapped_mechanism_resolves_to_a_catalog_key() -> None:
    cases = {
        "invoice_delivery": "invoices_sent",
        "estimate_follow_up": "estimates_sent",
        "review_requests": "reviews_requested",
        "online_booking_setup": "online_booking_usage",
        "feature_adoption": "feature_activations",
    }
    for mechanism, expected in cases.items():
        plan = select_indicators(mechanism, METRIC_CATALOG)
        assert plan.indicators[0].key == expected, mechanism
        assert plan.indicators[1].key == "app_return"


def test_selection_is_deterministic() -> None:
    first = select_indicators("review_requests", METRIC_CATALOG)
    second = select_indicators("review_requests", METRIC_CATALOG)
    assert [i.key for i in first.indicators] == [i.key for i in second.indicators]


def test_plan_never_exceeds_the_two_indicator_bound() -> None:
    # ck_measurement_count allows 1-2 indicators; selection must respect it.
    for mechanism in ("invoice_delivery", "unmapped", "", "invoice estimate review"):
        assert 1 <= len(select_indicators(mechanism, METRIC_CATALOG).indicators) <= 2


def test_return_to_app_indicators_exist_and_label_their_limitation() -> None:
    assert METRIC_CATALOG["app_return"].window_days == 7
    assert METRIC_CATALOG["app_continued_use"].window_days == 30
