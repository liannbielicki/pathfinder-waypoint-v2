from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import waypoint.llm as llm_module
from waypoint.llm import LLMGateway, Pricing, RateLimitExhausted, UsageMissing, extract_json
from waypoint.tables import UsageRow


def test_extract_json_handles_clean_fenced_and_prose_wrapped_output() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go:\n```json\n[{"a": 1}]\n```\nHope that helps!') == [{"a": 1}]
    assert extract_json('Sure! {"a": 1} is my pick.') == {"a": 1}
    assert extract_json('{"a": 1} (note the trailing "}" in prose)') == {"a": 1}


def test_extract_json_raises_on_no_json_at_all() -> None:
    with pytest.raises(ValueError):
        extract_json("I would pick the invoices metric.")

TEST_PRICING = Pricing(
    models={"fast": "model-fast", "deep": "model-deep"},
    usd_per_mtok={
        "model-fast": (Decimal(10), Decimal(20)),
        "model-deep": (Decimal(30), Decimal(60)),
    },
)


class FakeAnthropic:
    """Stands in for AsyncAnthropic messages.create."""

    def __init__(self, responses: list[object] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._responses = responses or []

    @property
    def messages(self) -> FakeAnthropic:
        return self

    async def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if not self._responses:
            return _response("one idea", 100, 20)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _response(text: str, input_tokens: int, output_tokens: int, usage: bool = True) -> object:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        model="model-fast",
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
        if usage
        else None,
    )


class Fake429(Exception):
    status_code = 429


async def usage_count(session: AsyncSession, run_id: str, stage: str) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(UsageRow)
            .where(UsageRow.run_id == run_id, UsageRow.stage == stage)
        )
    ).scalar_one()


async def test_every_completion_persists_usage(db_session: AsyncSession) -> None:
    gateway = LLMGateway(FakeAnthropic(), db_session, pricing=TEST_PRICING)
    result = await gateway.complete("fast", "Return one idea", "run-1", "generate")
    assert result.input_tokens == 100
    assert result.output_tokens == 20
    assert result.cost_usd == Decimal("0.0014")
    await db_session.commit()
    assert await usage_count(db_session, "run-1", "generate") == 1


async def test_missing_usage_block_fails_closed(db_session: AsyncSession) -> None:
    fake = FakeAnthropic([_response("x", 0, 0, usage=False)])
    gateway = LLMGateway(fake, db_session, pricing=TEST_PRICING)
    with pytest.raises(UsageMissing):
        await gateway.complete("fast", "p", "run-1", "generate")


async def test_unpriced_model_is_rejected_at_construction(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError):
        Pricing(
            models={"fast": "mystery-model", "deep": "model-deep"},
            usd_per_mtok={"model-deep": (Decimal(1), Decimal(2))},
        )


async def test_429_retries_with_backoff_then_succeeds(db_session: AsyncSession) -> None:
    fake = FakeAnthropic([Fake429(), Fake429(), _response("ok", 10, 5)])
    gateway = LLMGateway(fake, db_session, pricing=TEST_PRICING, backoff_seconds=0)
    result = await gateway.complete("fast", "p", "run-1", "generate")
    assert result.text == "ok"
    assert len(fake.calls) == 3


async def test_429_exhaustion_raises_and_never_fabricates(db_session: AsyncSession) -> None:
    fake = FakeAnthropic([Fake429(), Fake429(), Fake429()])
    gateway = LLMGateway(fake, db_session, pricing=TEST_PRICING, backoff_seconds=0)
    with pytest.raises(RateLimitExhausted):
        await gateway.complete("fast", "p", "run-1", "generate")
    assert len(fake.calls) == 3


async def test_usage_survives_a_caller_rollback(db_session: AsyncSession) -> None:
    # The gateway owns its transaction: a paid call's usage row must survive
    # the pipeline rolling back its own work.
    gateway = LLMGateway(FakeAnthropic(), db_session, pricing=TEST_PRICING)
    await gateway.complete("fast", "p", "run-roll", "generate")
    await db_session.rollback()
    assert await usage_count(db_session, "run-roll", "generate") == 1


async def test_deep_tier_uses_deep_model(db_session: AsyncSession) -> None:
    fake = FakeAnthropic()
    gateway = LLMGateway(fake, db_session, pricing=TEST_PRICING)
    await gateway.complete("deep", "p", "run-1", "final")
    assert fake.calls[0]["model"] == "model-deep"


async def test_temperature_passes_through_only_when_given(db_session: AsyncSession) -> None:
    fake = FakeAnthropic([_response("a", 10, 5), _response("b", 10, 5)])
    gateway = LLMGateway(fake, db_session, pricing=TEST_PRICING)
    await gateway.complete("fast", "p", "run-1", "screen", temperature=0.0)
    await gateway.complete("fast", "p", "run-1", "generate")
    assert fake.calls[0]["temperature"] == 0.0
    assert "temperature" not in fake.calls[1]


class FakeTemperatureRejected(Exception):
    """claude-sonnet-5 and later 400 any request that sets temperature."""

    status_code = 400

    def __str__(self) -> str:
        return "invalid_request_error: temperature: Extra inputs are not permitted"


@pytest.fixture(autouse=True)
def _fresh_temperature_memo() -> None:
    # The rejection memo is module-level (process lifetime in prod); tests
    # must not leak learned models into each other.
    llm_module._temperature_rejected.clear()


async def test_temperature_400_retries_once_without_it(db_session: AsyncSession) -> None:
    fake = FakeAnthropic([FakeTemperatureRejected(), _response("ok", 10, 5)])
    gateway = LLMGateway(fake, db_session, pricing=TEST_PRICING)
    result = await gateway.complete("deep", "p", "run-1", "final", temperature=0.0)
    assert result.text == "ok"
    assert fake.calls[0]["temperature"] == 0.0  # first attempt as requested
    assert "temperature" not in fake.calls[1]  # retried without the param


async def test_temperature_rejection_is_remembered_per_model(db_session: AsyncSession) -> None:
    # After one live rejection the gateway stops sending temperature to that
    # model — no doomed 400 round trip on every deep call.
    fake = FakeAnthropic([FakeTemperatureRejected(), _response("ok", 10, 5), _response("ok2", 10, 5)])
    gateway = LLMGateway(fake, db_session, pricing=TEST_PRICING)
    await gateway.complete("deep", "p", "run-1", "final", temperature=0.0)
    await gateway.complete("deep", "p", "run-1", "final", temperature=0.0)
    assert len(fake.calls) == 3  # 400 + clean retry, then ONE clean call
    assert "temperature" not in fake.calls[2]


async def test_unrelated_400_still_raises(db_session: AsyncSession) -> None:
    class FakeBadRequest(Exception):
        status_code = 400

        def __str__(self) -> str:
            return "invalid_request_error: max_tokens must be positive"

    fake = FakeAnthropic([FakeBadRequest()])
    gateway = LLMGateway(fake, db_session, pricing=TEST_PRICING)
    with pytest.raises(Exception, match="max_tokens"):
        await gateway.complete("deep", "p", "run-1", "final", temperature=0.0)
    assert len(fake.calls) == 1  # no blind retry


async def test_worst_case_cost_bounds_the_actual_cost(db_session: AsyncSession) -> None:
    from waypoint.llm import worst_case_cost

    prompt = "p" * 4000  # ~1000 real tokens; the estimator assumes ~1333
    worst = worst_case_cost(TEST_PRICING, "fast", prompt, "system text", max_tokens=1200)
    actual = TEST_PRICING.cost("model-fast", input_tokens=1000, output_tokens=1200)
    assert worst >= actual
    assert worst > 0


async def test_request_and_usage_ids_are_captured(db_session: AsyncSession) -> None:
    response = _response("ok", 10, 5)
    response._request_id = "req_abc"  # the anthropic SDK exposes this attribute
    gateway = LLMGateway(FakeAnthropic([response]), db_session, pricing=TEST_PRICING)
    result = await gateway.complete("fast", "p", "run-1", "generate")
    assert result.request_id == "req_abc"
    assert result.usage_id is not None
    row = await db_session.get(UsageRow, result.usage_id)
    assert row is not None and row.run_id == "run-1"
