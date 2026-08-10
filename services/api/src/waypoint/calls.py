"""Recorded paid-call lifecycle and the fleet-wide in-flight limiter.

Every paid model call flows through MeteredLLM.complete: reserve the worst case,
persist a pending row under a deterministic key, take a fleet slot, call the
provider, persist the result, reconcile the reservation to actual usage. A
committed key short-circuits with the stored response and zero new spend —
duplicate-retry protection and resume-without-re-paying in one mechanism.

Session rule (load-bearing): reservations, call rows, reconciles, and
abandoned-spend conversion all commit on the calls/usage session — paid facts
must survive pipeline rollbacks.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from waypoint.llm import LLMResult, Pricing, worst_case_cost
from waypoint.tables import LlmCallRow

# ponytail: code constant, not a fleet_control column — the spec says nothing
# may edit it at run time. Promote to a column if ops ever needs no-deploy tuning.
MAX_IN_FLIGHT_LLM_CALLS = 4
LLM_SLOT_NAMESPACE = "waypoint_llm_slot"


class BudgetExhausted(Exception):
    pass


class LLMLike(Protocol):
    async def complete(
        self,
        tier: str,
        prompt: str,
        run_id: str,
        stage: str,
        system: str | None = None,
        max_tokens: int = 1200,
        temperature: float | None = None,
    ) -> LLMResult: ...


class FleetSlots:
    """Fleet-wide slot limiter on Postgres session-level advisory locks.

    Locks belong to the CONNECTION: hold a dedicated AsyncConnection for the
    worker's lifetime so a crash releases the slot when the connection dies.
    Never acquire these on a pooled session.
    """

    def __init__(self, connection: AsyncConnection, poll_seconds: float = 0.25) -> None:
        self.connection = connection
        self.poll_seconds = poll_seconds

    async def acquire(self) -> int:
        while True:
            for slot in range(MAX_IN_FLIGHT_LLM_CALLS):
                got = (
                    await self.connection.execute(
                        text("SELECT pg_try_advisory_lock(hashtext(:ns), :slot)"),
                        {"ns": LLM_SLOT_NAMESPACE, "slot": slot},
                    )
                ).scalar_one()
                await self.connection.commit()
                if got:
                    return slot
            await asyncio.sleep(self.poll_seconds)

    async def release(self, slot: int) -> None:
        await self.connection.execute(
            text("SELECT pg_advisory_unlock(hashtext(:ns), :slot)"),
            {"ns": LLM_SLOT_NAMESPACE, "slot": slot},
        )
        await self.connection.commit()


class RecordedCalls:
    """Durable llm_calls rows: pending → committed → reconciled | abandoned."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lookup(self, call_key: str) -> LlmCallRow | None:
        return (
            await self.session.execute(
                select(LlmCallRow)
                .where(LlmCallRow.call_key == call_key)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def begin(
        self,
        call_key: str,
        run_id: str,
        pro_id: str | None,
        stage: str,
        model: str,
        reserved_usd: Decimal,
    ) -> LlmCallRow:
        row = LlmCallRow(
            call_key=call_key,
            run_id=run_id,
            pro_id=pro_id,
            stage=stage,
            model=model,
            reserved_usd=reserved_usd,
        )
        self.session.add(row)
        await self.session.commit()  # reservation + pending row become durable together
        return row

    async def commit_result(
        self,
        row: LlmCallRow,
        response_text: str,
        usage_id: str | None,
        actual_usd: Decimal,
        request_id: str | None,
    ) -> bool:
        """Pending → committed. False when a new lease owner abandoned the row
        mid-flight; never resurrect it — its spend is already converted."""
        await self.session.refresh(row)
        if row.status != "pending":
            return False
        row.status = "committed"
        row.response_text = response_text
        row.usage_id = usage_id
        row.actual_usd = actual_usd
        row.provider_request_id = request_id
        await self.session.commit()
        return True

    async def mark_reconciled(self, row: LlmCallRow) -> None:
        row.status = "reconciled"
        await self.session.commit()

    async def abandon_stale(self, run_id: str, pro_id: str | None) -> list[LlmCallRow]:
        rows = list(
            (
                await self.session.execute(
                    select(LlmCallRow).where(
                        LlmCallRow.run_id == run_id,
                        LlmCallRow.pro_id == pro_id,
                        LlmCallRow.status == "pending",
                    )
                )
            ).scalars()
        )
        for row in rows:
            row.status = "abandoned"
        await self.session.commit()
        return rows


@dataclass
class MeteredLLM:
    """The one paid-call path. No pipeline code calls a gateway directly."""

    gateway: LLMLike
    records: RecordedCalls
    slots: FleetSlots
    pricing: Pricing
    reserve: Callable[[str, Decimal], Awaitable[bool]]  # (run_id, amount)
    reconcile: Callable[[str, Decimal, Decimal], Awaitable[None]]  # (run_id, reserved, actual)

    async def complete(
        self,
        *,
        call_key: str,
        tier: str,
        prompt: str,
        run_id: str,
        pro_id: str | None,
        stage: str,
        system: str | None = None,
        max_tokens: int = 1200,
        temperature: float | None = None,
    ) -> LLMResult:
        row = await self.records.lookup(call_key)
        if row is not None and row.status in ("committed", "reconciled"):
            if row.status == "committed":  # crash between commit and reconcile
                await self.reconcile(run_id, row.reserved_usd, row.actual_usd or Decimal(0))
                await self.records.mark_reconciled(row)
            return LLMResult(
                text=row.response_text or "",
                model=row.model,
                input_tokens=0,
                output_tokens=0,
                cost_usd=row.actual_usd or Decimal(0),
                request_id=row.provider_request_id,
                usage_id=row.usage_id,
            )
        if row is None:
            worst = worst_case_cost(self.pricing, tier, prompt, system, max_tokens)
            if not await self.reserve(run_id, worst):
                raise BudgetExhausted(call_key)
            row = await self.records.begin(
                call_key,
                run_id,
                pro_id,
                stage,
                self.pricing.model_for(tier),
                worst,
            )
        elif row.status == "abandoned":
            # A recovered crash converted the old attempt's reservation to
            # spend; this is a NEW attempt under a new reservation. ponytail:
            # a stale owner racing commit_result here would write the same
            # deterministic prompt's response, over-count spend slightly, and
            # leave THIS attempt's reservation held until run end (our
            # commit_result returns False, skipping the reconcile) — both
            # effects honest-direction and self-limiting, tiny window, not
            # worth a fencing token.
            worst = worst_case_cost(self.pricing, tier, prompt, system, max_tokens)
            if not await self.reserve(run_id, worst):
                raise BudgetExhausted(call_key)
            row.status = "pending"
            row.reserved_usd = worst
            await self.records.session.commit()
        else:  # pending from a crashed attempt: its reservation is already durable
            worst = row.reserved_usd
        slot = await self.slots.acquire()
        try:
            result = await self.gateway.complete(
                tier,
                prompt,
                run_id,
                stage,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        finally:
            # A provider failure leaves the row pending; the durable worst-case
            # reservation is resolved by abandon_stale on resume.
            await self.slots.release(slot)
        if await self.records.commit_result(
            row,
            result.text,
            result.usage_id,
            result.cost_usd,
            result.request_id,
        ):
            await self.reconcile(run_id, worst, result.cost_usd)
            await self.records.mark_reconciled(row)
        return result
