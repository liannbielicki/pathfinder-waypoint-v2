"""Metered model gateway: one shared client, bounded retry, mandatory usage rows.

Honest cost semantics carried from the audited build: an unpriced model or a
missing usage block is an error, never a plausible number and never $0.00.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.tables import UsageRow

Tier = Literal["fast", "deep"]

_MILLION = Decimal(1_000_000)
CACHE_READ_MULTIPLIER = Decimal("0.10")
CACHE_WRITE_MULTIPLIER = Decimal("1.25")

# (input, output) USD per million tokens. Verified against Anthropic's pricing
# 2026-07-31 in the audited build's price table.
PRICES_USD_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "claude-fable-5": (Decimal("10.00"), Decimal("50.00")),
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-8": (Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-7": (Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-6": (Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": (Decimal("3.00"), Decimal("15.00")),
    "claude-sonnet-4-6": (Decimal("3.00"), Decimal("15.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
}


class UsageMissing(Exception):
    """The response carried no usage block; the call cannot be billed honestly."""


class RateLimitExhausted(Exception):
    """Bounded retries hit the rate limit every time."""


class Pricing:
    def __init__(
        self,
        models: dict[str, str],
        usd_per_mtok: dict[str, tuple[Decimal, Decimal]] | None = None,
    ) -> None:
        self.models = models
        self.usd_per_mtok = PRICES_USD_PER_MTOK if usd_per_mtok is None else usd_per_mtok
        for tier, model in models.items():
            if model not in self.usd_per_mtok:
                raise ValueError(
                    f"model {model!r} (tier {tier!r}) has no price row; "
                    "refusing to run with unmeterable spend"
                )

    def model_for(self, tier: Tier) -> str:
        return self.models[tier]

    def cost(self, model: str, input_tokens: int, output_tokens: int,
             cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> Decimal:
        input_rate, output_rate = self.usd_per_mtok[model]
        return (
            input_tokens * input_rate
            + cache_read_tokens * input_rate * CACHE_READ_MULTIPLIER
            + cache_write_tokens * input_rate * CACHE_WRITE_MULTIPLIER
            + output_tokens * output_rate
        ) / _MILLION


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Decimal = field(default_factory=lambda: Decimal(0))


def _is_rate_limited(error: Exception) -> bool:
    return getattr(error, "status_code", None) in (429, 529)


async def retry_rate_limit[T](
    call: Callable[[], Awaitable[T]], attempts: int = 3, backoff_seconds: float = 2.0
) -> T:
    for attempt in range(attempts):
        try:
            return await call()
        except Exception as error:
            if not _is_rate_limited(error):
                raise
            if attempt == attempts - 1:
                raise RateLimitExhausted(
                    f"rate limited on every one of {attempts} attempts"
                ) from error
            await asyncio.sleep(backoff_seconds * 2**attempt)
    raise AssertionError("unreachable")


class AnthropicLike(Protocol):
    @property
    def messages(self) -> Any: ...


class LLMGateway:
    def __init__(
        self,
        client: AnthropicLike,
        session: AsyncSession,
        pricing: Pricing,
        attempts: int = 3,
        backoff_seconds: float = 2.0,
    ) -> None:
        self.client = client
        self.session = session
        self.pricing = pricing
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds

    async def complete(
        self,
        tier: Tier,
        prompt: str,
        run_id: str,
        stage: str,
        system: str | None = None,
        max_tokens: int = 1200,
    ) -> LLMResult:
        model = self.pricing.model_for(tier)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system is not None:
            kwargs["system"] = system
        response = await retry_rate_limit(
            lambda: self.client.messages.create(**kwargs),
            attempts=self.attempts,
            backoff_seconds=self.backoff_seconds,
        )
        result = self._result_from_response(model, response)
        self.session.add(UsageRow(
            run_id=run_id, stage=stage, model=result.model,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_write_tokens=result.cache_write_tokens,
            cost_usd=result.cost_usd,
        ))
        await self.session.flush()
        return result

    def _result_from_response(self, model: str, response: Any) -> LLMResult:
        usage = getattr(response, "usage", None)
        if usage is None:
            raise UsageMissing(f"model {model!r} returned no usage block")
        input_tokens = int(usage.input_tokens)
        output_tokens = int(usage.output_tokens)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return LLMResult(
            text=text,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=self.pricing.cost(model, input_tokens, output_tokens,
                                       cache_read, cache_write),
        )
