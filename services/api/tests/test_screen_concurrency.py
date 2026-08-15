"""Tied finalists screen concurrently through private paid-call stacks, and the
worker's ranker-tier pricing falls back to the fast model when unconfigured."""

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from functools import partial

from sqlalchemy import select

from waypoint import queue as queue_module
from waypoint.calls import (
    MAX_IN_FLIGHT_LLM_CALLS,
    BudgetExhausted,
    FleetSlots,
    MeteredLLM,
    RecordedCalls,
)
from waypoint.llm import LLMResult
from waypoint.pipeline import run_job
from waypoint.settings import Settings
from waypoint.tables import LlmCallRow, RunRow
from waypoint.worker import make_pricing

from .conftest import (
    DEFAULT_MECHANISMS,
    FAKE_PRICING,
    FakeDeps,
    FakeLLM,
    rank_json,
    reactions_json,
)
from .test_pipeline import GOOD, GREAT, LOSE, rounds, set_loop_config

TIED_RANKING = rank_json(("c1", 0.90), ("c2", 0.88), ("c3", 0.20), tie=True, tie_reason="same")


# The batch's ideas carry these concepts (see conftest.idea_json); finalist c1 is
# idea 0 and c2 is idea 1, so a prompt substring picks out one concurrent screen.
C1_CONCEPT = "Concept 0"


class ScreenProbe(FakeLLM):
    """Watches how many screen calls are in flight at once, and can single one
    of them out by concept.

    With `rendezvous` on, the first screen call BLOCKS until the second one
    arrives — a sequential implementation would never deliver the second, so
    the round completing at all is proof the two calls overlapped in time.

    `screen_effects` maps a concept substring to what that finalist's call does:
    an Exception to raise, or a response string to return instead of the scripted
    one. Only when one of those effects is an Exception does the OTHER finalist
    park in the provider, so a raising sibling must be cancelled there — which is
    what `cancelled_screens` counts.
    """

    def __init__(
        self, rendezvous: bool, screen_effects: dict[str, str | Exception] | None = None
    ) -> None:
        super().__init__()
        self.rendezvous = rendezvous
        self.screen_effects = screen_effects or {}
        self.both_arrived = asyncio.Event()
        self.peak_screens_in_flight = 0
        self.completed_screens = 0
        self.cancelled_screens = 0
        self._in_flight = 0

    async def complete(  # type: ignore[override]
        self, tier: str, prompt: str, run_id: str, stage: str, **kwargs: object
    ) -> LLMResult:
        if stage != "screen":
            return await super().complete(tier, prompt, run_id, stage, **kwargs)  # type: ignore[arg-type]
        self._in_flight += 1
        self.peak_screens_in_flight = max(self.peak_screens_in_flight, self._in_flight)
        try:
            if self.rendezvous:
                if self._in_flight >= 2:
                    self.both_arrived.set()
                # A generous ceiling: on the sequential path this is the failure
                # mode, so it must not hang the suite forever.
                async with asyncio.timeout(20):
                    await self.both_arrived.wait()
            singled_out = [e for marker, e in self.screen_effects.items() if marker in prompt]
            if singled_out:
                effect = singled_out[0]
                if isinstance(effect, Exception):
                    raise effect
                result = LLMResult(
                    text=effect,
                    model="fake-fast",
                    input_tokens=10,
                    output_tokens=5,
                    cost_usd=Decimal("0.001"),
                )
            else:
                if any(isinstance(e, Exception) for e in self.screen_effects.values()):
                    # Only when a sibling actually raises: park in the provider so
                    # a correct implementation cancels this await instead of
                    # orphaning it. Gated, or every effects test pays the wait.
                    await asyncio.sleep(30)
                result = await super().complete(tier, prompt, run_id, stage, **kwargs)  # type: ignore[arg-type]
            self.completed_screens += 1
            return result
        except asyncio.CancelledError:
            self.cancelled_screens += 1
            raise
        finally:
            self._in_flight -= 1


def stack_factory(engine, factory, gateway):
    """The worker's llm-stack contract, built over the test engine: each stack
    owns its advisory-lock connection, its usage session (records + reserve +
    reconcile) and its persona-cache session, and closes all three on exit.

    `max_slots` is left unset on purpose so FleetSlots derives it from the same
    MAX_IN_FLIGHT_LLM_CALLS default the main limiter falls back to — one
    fleet-wide cap, never a second number that can drift out of step."""

    @asynccontextmanager
    async def stack():
        connection = await engine.connect()
        try:
            async with factory() as usage_session, factory() as cache_session:
                slots = FleetSlots(connection)
                assert slots.max_slots == MAX_IN_FLIGHT_LLM_CALLS
                yield (
                    MeteredLLM(
                        gateway=gateway,
                        records=RecordedCalls(usage_session),
                        slots=slots,
                        pricing=FAKE_PRICING,
                        reserve=partial(queue_module.reserve_cost, usage_session),
                        reconcile=partial(queue_module.reconcile_cost, usage_session),
                    ),
                    cache_session,
                )
        finally:
            await connection.close()

    return stack


def use_gateway(deps: FakeDeps, gateway: FakeLLM) -> None:
    deps.gateway = gateway
    deps.llm.gateway = gateway
    gateway.responses["rank"] = TIED_RANKING


async def screen_call_keys(deps: FakeDeps, run_id: str) -> dict[str, str]:
    """call_key -> status, read as columns so the pipeline session's identity
    map cannot mask rows another session committed."""
    rows = await deps.db.execute(
        select(LlmCallRow.call_key, LlmCallRow.status).where(
            LlmCallRow.run_id == run_id, LlmCallRow.stage == "screen"
        )
    )
    return dict(rows.all())  # type: ignore[arg-type]


async def test_tied_finalists_are_screened_concurrently(
    db_engine, db_session_factory, deps: FakeDeps, seeded_job
) -> None:
    gateway = ScreenProbe(rendezvous=True)
    use_gateway(deps, gateway)
    # Same reactions for both finalists: under concurrency the completion order
    # is not fixed, so the assertion must not depend on it.
    gateway.responses["screen"] = reactions_json(GOOD)
    deps.llm_stacks = stack_factory(db_engine, db_session_factory, gateway)
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)

    await run_job(seeded_job.id, deps)

    assert gateway.peak_screens_in_flight == 2  # both were in the provider at once
    assert gateway.calls_for("screen") == 2
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].ranking["finalists"] == ["c1", "c2"]
    assert ledger[0].outcome == "win"


async def test_each_concurrent_screen_is_a_recorded_metered_call(
    db_engine, db_session_factory, deps: FakeDeps, seeded_job
) -> None:
    gateway = ScreenProbe(rendezvous=True)
    use_gateway(deps, gateway)
    gateway.responses["screen"] = reactions_json(GOOD)
    deps.llm_stacks = stack_factory(db_engine, db_session_factory, gateway)
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)

    await run_job(seeded_job.id, deps)

    prefix = f"{seeded_job.run_id}:{seeded_job.pro_id}:r1:screen"
    recorded = await screen_call_keys(deps, seeded_job.run_id)
    assert set(recorded) == {f"{prefix}:c1", f"{prefix}:c2"}
    assert set(recorded.values()) == {"reconciled"}  # each stack reconciled its own spend


async def test_a_budget_exhausted_screen_propagates_and_cancels_its_sibling(
    db_engine, db_session_factory, deps: FakeDeps, seeded_job
) -> None:
    """The critical contract: one finalist raising must stop the OTHER finalist's
    paid call, not leave it running against a job we may no longer own."""
    gateway = ScreenProbe(
        rendezvous=True, screen_effects={C1_CONCEPT: BudgetExhausted("injected")}
    )
    use_gateway(deps, gateway)
    gateway.responses["screen"] = reactions_json(GOOD)
    deps.llm_stacks = stack_factory(db_engine, db_session_factory, gateway)
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)

    await run_job(seeded_job.id, deps)

    assert gateway.peak_screens_in_flight == 2  # both were in the provider
    assert gateway.cancelled_screens == 1  # the sibling was cancelled, not orphaned
    assert gateway.completed_screens == 0  # and it never finished a paid call
    # BudgetExhausted reached run_job's handler as itself, not an ExceptionGroup.
    run = await deps.db.get(RunRow, seeded_job.run_id)
    await deps.db.refresh(run)
    assert (run.status, run.stop_reason) == ("stopped", "budget_exhausted")
    assert not await rounds(deps.db, seeded_job.run_id)  # the round never landed
    # The cancelled stack unwound: its pending row is still pending (never
    # resurrected), and nothing leaked a connection — the next query works.
    assert set((await screen_call_keys(deps, seeded_job.run_id)).values()) <= {"pending"}


async def test_one_finalist_failing_concurrently_leaves_the_other_scored(
    db_engine, db_session_factory, deps: FakeDeps, seeded_job
) -> None:
    """A per-finalist PipelineFailure must degrade only that finalist, exactly as
    on the sequential path — it never escapes the concurrent branch."""
    gateway = ScreenProbe(rendezvous=True, screen_effects={C1_CONCEPT: "not json at all"})
    use_gateway(deps, gateway)
    gateway.responses["screen"] = reactions_json(GREAT)  # c2's usable panel
    deps.llm_stacks = stack_factory(db_engine, db_session_factory, gateway)
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)

    await run_job(seeded_job.id, deps)

    assert gateway.peak_screens_in_flight == 2
    assert gateway.cancelled_screens == 0  # a degraded finalist cancels nothing
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert "c1" in ledger[0].ranking["screen_failures"]  # only c1 was unusable
    assert ledger[0].outcome == "win"  # c2's real score still decided the round
    assert ledger[0].score_pp is not None
    assert ledger[0].mechanism == DEFAULT_MECHANISMS[1]


async def test_without_llm_stacks_the_tied_round_screens_sequentially(
    deps: FakeDeps, seeded_job
) -> None:
    gateway = ScreenProbe(rendezvous=False)  # a barrier here would simply hang
    use_gateway(deps, gateway)
    gateway.responses["screen"] = [reactions_json(LOSE), reactions_json(GREAT)]
    assert deps.llm_stacks is None
    await set_loop_config(deps, seeded_job.run_id, MAX_ROUNDS=1)

    await run_job(seeded_job.id, deps)

    assert gateway.peak_screens_in_flight == 1  # one at a time, unchanged
    assert gateway.calls_for("screen") == 2
    ledger = await rounds(deps.db, seeded_job.run_id)
    assert ledger[0].ranking["selection_reason"] == "tie_broken_by_screen_runner_up"
    assert ledger[0].outcome == "win"
    recorded = await screen_call_keys(deps, seeded_job.run_id)
    assert len(recorded) == 2


# --- worker pricing ----------------------------------------------------------


def settings_with(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://localhost/x",
        LLM_API_KEY="k",
        N8N_CONTEXT_URL="https://n8n.example.com/webhook",
        N8N_TOKEN="k",
        PERSONA_URL="https://personas.example.com",
        PERSONA_TOKEN="k",
        HANDOFF_URL="https://handoff.example.com",
        HANDOFF_TOKEN="k",
        BYPASS_TOKEN="k",
        RUN_COST_USD="10",
        DAY_COST_USD="100",
        WORKER_COUNT=1,
        MODEL_FAST="claude-haiku-4-5",
        MODEL_DEEP="claude-opus-5",
        APP_PASSWORD="pw",
        SESSION_KEY="x" * 32,
        **overrides,
    )


def test_unset_ranker_model_falls_back_to_the_fast_model() -> None:
    pricing = make_pricing(settings_with(MODEL_RANKER=""))
    assert pricing.model_for("rank") == "claude-haiku-4-5"
    assert pricing.model_for("rank") == pricing.model_for("fast")


def test_a_configured_ranker_model_is_the_one_used() -> None:
    pricing = make_pricing(settings_with(MODEL_RANKER="claude-sonnet-5"))
    assert pricing.model_for("rank") == "claude-sonnet-5"
    assert pricing.model_for("fast") == "claude-haiku-4-5"  # the other tiers are untouched
