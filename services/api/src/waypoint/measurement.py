"""Typed proposal-specific measurement plans.

V3: selection is deterministic — no model in the loop. The catalog is a
finite, human-owned metric set defining direction, source, and window. Every
winner measures the primary objective (app_return) plus at most one
mechanism-mapped metric; an unmapped mechanism measures the primary objective
alone, so a winner is never "unmeasurable". Iterable outcome readback is
deliberately out of launch scope.
"""

from waypoint.models import MeasurementIndicator, MeasurementPlan

METRIC_CATALOG: dict[str, MeasurementIndicator] = {
    "invoices_sent": MeasurementIndicator(
        key="invoices_sent", label="Invoices sent", direction="increase",
        source="billing", window_days=30,
        rationale="Count of invoices sent in the window.",
    ),
    "estimates_sent": MeasurementIndicator(
        key="estimates_sent", label="Estimates sent", direction="increase",
        source="billing", window_days=30,
        rationale="Count of estimates sent in the window.",
    ),
    "reviews_requested": MeasurementIndicator(
        key="reviews_requested", label="Review requests", direction="increase",
        source="product_activity", window_days=30,
        rationale="Review-request product activity in the window.",
    ),
    "online_booking_usage": MeasurementIndicator(
        key="online_booking_usage", label="Online booking usage", direction="increase",
        source="product_activity", window_days=90,
        rationale="Online booking product activity in the window.",
    ),
    "feature_activations": MeasurementIndicator(
        key="feature_activations", label="Feature activations", direction="increase",
        source="product_activity", window_days=30,
        rationale="Newly attached features in the window.",
    ),
    "app_return": MeasurementIndicator(
        key="app_return", label="Returned to app (7d)", direction="increase",
        source="amplitude", window_days=7,
        rationale="The pro returns to and uses the app within 7 days — the primary "
        "objective. Canonical Amplitude active-use event contract pending (TODOS.md).",
    ),
    "app_continued_use": MeasurementIndicator(
        key="app_continued_use", label="Continued app usage (30d)", direction="increase",
        source="amplitude", window_days=30,
        rationale="Sustained app usage within 30 days of the touch. Canonical "
        "Amplitude active-use event contract pending (TODOS.md).",
    ),
}


# Mechanism substring → mechanism-specific catalog key. First match wins;
# expanding the catalog means adding a row here, never touching the pipeline.
_MECHANISM_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("invoice", "invoices_sent"),
    ("estimate", "estimates_sent"),
    ("quote", "estimates_sent"),
    ("review", "reviews_requested"),
    ("booking", "online_booking_usage"),
    ("feature", "feature_activations"),
    ("adoption", "feature_activations"),
    ("activation", "feature_activations"),
)


def select_indicators(
    mechanism: str, catalog: dict[str, MeasurementIndicator] = METRIC_CATALOG
) -> MeasurementPlan:
    """Mechanism-mapped metric first (when one maps), then the primary
    objective. Bounded to two indicators (ck_measurement_count)."""
    indicators: list[MeasurementIndicator] = []
    lowered = mechanism.lower()
    for keyword, key in _MECHANISM_KEYWORDS:
        if keyword in lowered and key in catalog:
            indicators.append(catalog[key])
            break
    indicators.append(catalog["app_return"])
    return MeasurementPlan(indicators=indicators)
