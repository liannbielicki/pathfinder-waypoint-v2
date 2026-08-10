"""Typed proposal-specific measurement plans.

The loop picks one or two leading indicators from a finite, human-owned metric
catalog. The catalog defines direction, source, and window — the model only
selects keys. An unknown key abstains; a source is never invented. Iterable
outcome readback is deliberately out of launch scope.
"""

from typing import Protocol

from pydantic import BaseModel

from waypoint.llm import LLMResult, extract_json
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
}


class UnmeasurableWinner(Exception):
    pass


class WinnerLike(Protocol):
    run_id: str
    mechanism: str
    title: str


class LLMLike(Protocol):
    async def complete(self, tier: str, prompt: str, run_id: str, stage: str,
                       system: str | None = None, max_tokens: int = 1200) -> LLMResult: ...


class _Selected(BaseModel):
    key: str


class MeasurementSelection(BaseModel):
    indicators: list[_Selected]


def measurement_prompt(winner: WinnerLike, keys: list[str]) -> str:
    return f"""A retention proposal was selected for one Pro:

Title: {winner.title}
Mechanism: {winner.mechanism}

Pick the ONE or TWO leading indicators that best express this proposal's
mechanism — the metrics that would move first if the proposal works. Churn
remains the long-term outcome and is not selectable here.

Available metric keys (choose ONLY from this list):
{", ".join(keys)}

Return JSON of the form {{"indicators": [{{"key": "<metric_key>"}}]}} and
nothing else.
"""


async def create_measurement_plan(
    winner: WinnerLike, llm: LLMLike,
    catalog: dict[str, MeasurementIndicator],
) -> MeasurementPlan:
    proposal = await llm.complete(
        "fast", measurement_prompt(winner, list(catalog)), winner.run_id, "measure",
    )
    try:
        # extract_json tolerates markdown fences and surrounding prose — raw
        # model_validate_json here turned real winners into "unmeasurable".
        selected = MeasurementSelection.model_validate(extract_json(proposal.text))
    except ValueError as error:
        raise UnmeasurableWinner(f"unparseable indicator selection: {error}") from error
    if not 1 <= len(selected.indicators) <= 2:
        raise UnmeasurableWinner("the loop must select one or two indicators")
    try:
        return MeasurementPlan(indicators=[catalog[item.key] for item in selected.indicators])
    except KeyError as error:
        raise UnmeasurableWinner(f"unknown metric: {error.args[0]}") from error
