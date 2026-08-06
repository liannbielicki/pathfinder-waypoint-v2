from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.llm import LLMGateway, Pricing, RateLimitExhausted, UsageMissing
from waypoint.tables import UsageRow

TEST_PRICING = Pricing(
    models={"fast": "model-fast", "deep": "model-deep"},
    usd_per_mtok={"model-fast": (Decimal(10), Decimal(20)),
                  "model-deep": (Decimal(30), Decimal(60))},
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


def _response(text: str, input_tokens: int, output_tokens: int,
              usage: bool = True) -> object:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        model="model-fast",
        usage=SimpleNamespace(
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ) if usage else None,
    )


class Fake429(Exception):
    status_code = 429


async def usage_count(session: AsyncSession, run_id: str, stage: str) -> int:
    return (await session.execute(
        select(func.count()).select_from(UsageRow)
        .where(UsageRow.run_id == run_id, UsageRow.stage == stage)
    )).scalar_one()


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
        Pricing(models={"fast": "mystery-model", "deep": "model-deep"},
                usd_per_mtok={"model-deep": (Decimal(1), Decimal(2))})


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
