import json
from dataclasses import dataclass
from decimal import Decimal

import pytest

from waypoint.llm import LLMResult
from waypoint.measurement import (
    METRIC_CATALOG,
    UnmeasurableWinner,
    create_measurement_plan,
)


@dataclass
class WinnerFixture:
    run_id: str
    pro_id: str
    winner_id: str
    mechanism: str
    title: str


INVOICE_WINNER = WinnerFixture(
    run_id="run-1", pro_id="pro_1", winner_id="win-1",
    mechanism="invoice_delivery", title="Send open invoices reminder",
)
UNKNOWN_MECHANISM_WINNER = WinnerFixture(
    run_id="run-1", pro_id="pro_1", winner_id="win-2",
    mechanism="quantum_alignment", title="Mystery proposal",
)


class FakeMeasureLLM:
    def __init__(self) -> None:
        self._text: str | None = None
        self.prompts: list[str] = []

    def respond(self, payload: dict) -> None:
        self._text = json.dumps(payload)

    def respond_raw(self, text: str) -> None:
        self._text = text

    async def complete(self, tier: str, prompt: str, run_id: str, stage: str,
                       system: str | None = None, max_tokens: int = 1200) -> LLMResult:
        self.prompts.append(prompt)
        assert stage == "measure"
        return LLMResult(text=self._text or "", model="fake",
                         input_tokens=5, output_tokens=5, cost_usd=Decimal("0.001"))


@pytest.fixture
def fake_llm() -> FakeMeasureLLM:
    return FakeMeasureLLM()


async def test_loop_selects_invoice_indicator_for_invoice_proposal(
    fake_llm: FakeMeasureLLM,
) -> None:
    fake_llm.respond({"indicators": [{"key": "invoices_sent", "window_days": 30}]})
    plan = await create_measurement_plan(INVOICE_WINNER, fake_llm, METRIC_CATALOG)
    assert [item.key for item in plan.indicators] == ["invoices_sent"]
    assert plan.indicators[0].direction == "increase"
    assert plan.indicators[0].window_days == 30


async def test_unmapped_metric_abstains_instead_of_inventing_source(
    fake_llm: FakeMeasureLLM,
) -> None:
    fake_llm.respond({"indicators": [{"key": "imaginary_metric", "window_days": 30}]})
    with pytest.raises(UnmeasurableWinner):
        await create_measurement_plan(UNKNOWN_MECHANISM_WINNER, fake_llm, METRIC_CATALOG)


async def test_zero_indicators_is_unmeasurable(fake_llm: FakeMeasureLLM) -> None:
    fake_llm.respond({"indicators": []})
    with pytest.raises(UnmeasurableWinner):
        await create_measurement_plan(INVOICE_WINNER, fake_llm, METRIC_CATALOG)


async def test_three_indicators_is_unmeasurable(fake_llm: FakeMeasureLLM) -> None:
    fake_llm.respond({"indicators": [
        {"key": "invoices_sent"}, {"key": "estimates_sent"}, {"key": "reviews_requested"},
    ]})
    with pytest.raises(UnmeasurableWinner):
        await create_measurement_plan(INVOICE_WINNER, fake_llm, METRIC_CATALOG)


async def test_two_catalog_indicators_are_allowed(fake_llm: FakeMeasureLLM) -> None:
    fake_llm.respond({"indicators": [{"key": "invoices_sent"}, {"key": "estimates_sent"}]})
    plan = await create_measurement_plan(INVOICE_WINNER, fake_llm, METRIC_CATALOG)
    assert len(plan.indicators) == 2


async def test_fenced_selection_json_still_parses(fake_llm: FakeMeasureLLM) -> None:
    # Production incident: the model wrapped its selection in ```json fences
    # and a real winner came out "unmeasurable".
    fake_llm.respond_raw('```json\n{"indicators": [{"key": "invoices_sent"}]}\n```')
    plan = await create_measurement_plan(INVOICE_WINNER, fake_llm, METRIC_CATALOG)
    assert [item.key for item in plan.indicators] == ["invoices_sent"]


async def test_non_json_selection_is_still_unmeasurable(fake_llm: FakeMeasureLLM) -> None:
    fake_llm.respond_raw("I would pick the invoices metric.")
    with pytest.raises(UnmeasurableWinner, match="unparseable"):
        await create_measurement_plan(INVOICE_WINNER, fake_llm, METRIC_CATALOG)


async def test_prompt_offers_only_the_finite_catalog(fake_llm: FakeMeasureLLM) -> None:
    fake_llm.respond({"indicators": [{"key": "invoices_sent"}]})
    await create_measurement_plan(INVOICE_WINNER, fake_llm, METRIC_CATALOG)
    prompt = fake_llm.prompts[0]
    for key in METRIC_CATALOG:
        assert key in prompt
    assert "invoice_delivery" in prompt
