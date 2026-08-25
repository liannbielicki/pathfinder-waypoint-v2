import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from waypoint.api import create_app
from waypoint.settings import Settings

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://localhost:5432/waypoint_test"
)

_TABLES = (
    "measurements",
    "handoffs",
    "winners",
    "jobs",
    "evolve_rounds",
    "llm_calls",
    "candidates",
    "touch_outcomes",
    "persona_evals",
    "runs",
    "llm_usage",
    "fleet_control",
)


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """Drop and re-migrate the test schema once per session, via the real migrations."""

    async def _reset_schema() -> None:
        engine = create_async_engine(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(_reset_schema())
    cfg = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")
    return TEST_DATABASE_URL


@pytest.fixture
async def db_engine(migrated_database: str) -> AsyncIterator:
    engine = create_async_engine(migrated_database)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} CASCADE"))
    yield engine
    await engine.dispose()


@pytest.fixture
def db_session_factory(db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def db_session(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with db_session_factory() as session:
        yield session


# --- pipeline fakes -------------------------------------------------------

import json
from decimal import Decimal

from waypoint import queue as queue_module
from waypoint.calls import MeteredLLM, RecordedCalls
from waypoint.llm import LLMResult, Pricing, RateLimitExhausted
from waypoint.measurement import METRIC_CATALOG, create_measurement_plan
from waypoint.models import MeasurementIndicator, MeasurementPlan
from waypoint.n8n import CONTRACT_VERSION, ContextUnavailable, OrgContextBatch
from waypoint.personas import Persona
from waypoint.pipeline import PipelineDeps, PostgresStore, QueueOps
from waypoint.queue import enqueue
from waypoint.scoring import load_calibration
from waypoint.tables import FleetControlRow, RunRow

FIXTURES = Path(__file__).parent / "fixtures"

FAKE_PRICING = Pricing(
    models={"fast": "model-fast", "deep": "model-deep", "rank": "model-fast"},
    usd_per_mtok={
        "model-fast": (Decimal(10), Decimal(20)),
        "model-deep": (Decimal(30), Decimal(60)),
    },
)


class InjectedCrash(Exception):
    pass


PERSONA_FIXTURE = json.loads((FIXTURES / "personas.json").read_text())
PERSONAS = [
    Persona(snapshot_version=PERSONA_FIXTURE["snapshot_version"], **p)
    for p in PERSONA_FIXTURE["personas"]
]


async def _fake_get_personas(segment: str) -> list[Persona]:
    # Fixture personas are all segment "1A"; the fake pool ignores the arg.
    return PERSONAS


def idea_json(mechanism: str, i: int = 0) -> str:
    """One evolve-round challenger, as the model would return it."""
    return json.dumps(
        {
            "title": f"Idea {i} via {mechanism}",
            "mechanism": mechanism,
            "actions": [f"do_{mechanism}"],
            "pro_facing_concept": f"Concept {i} the pro would experience.",
            "manager_rationale": f"Rationale {i} for the manager.",
            "channel": "sms",
            "risk": "May not land.",
        }
    )


def batch_json(mechanisms: list[str]) -> str:
    """One evolve-round batch, as the model would return it: a JSON array with
    one idea per mechanism."""
    return json.dumps([json.loads(idea_json(m, i)) for i, m in enumerate(mechanisms)])


def critics_json(block_kinds: list[str]) -> str:
    """One verdict per idea_index, in batch order."""
    return json.dumps(
        [
            {"idea_index": i, "block_kind": kind, "reason": f"verdict {kind}"}
            for i, kind in enumerate(block_kinds)
        ]
    )


def rank_json(*pairs: tuple[str, float], tie: bool = False, tie_reason: str = "") -> str:
    """A ranker decision over positional tokens, best first."""
    return json.dumps(
        {
            "ranking": [
                {"candidate_id": token, "rank": i + 1, "score": score}
                for i, (token, score) in enumerate(pairs)
            ],
            "tie": tie,
            "tie_reason": tie_reason,
        }
    )


DEFAULT_MECHANISMS = ["invoice_delivery", "review_requests", "feature_adoption"]
BATCH_OK = batch_json(DEFAULT_MECHANISMS)
RANK_OK = rank_json(("c1", 0.9), ("c2", 0.5), ("c3", 0.2))
CRITIC_OK = json.dumps([{"idea_index": 0, "block_kind": "none", "reason": "grounded"}])
CRITIC_BLOCK = json.dumps(
    [{"idea_index": 0, "block_kind": "ungrounded", "reason": "invented AR balance"}]
)
MEASURE_JSON = json.dumps({"indicators": [{"key": "invoices_sent"}]})
WARGAME_JSON = json.dumps({
    "on_return": {"action": "Send a congratulations nudge toward the feature used",
                  "channel": "email"},
    "on_click_no_use": {"action": "One simpler ask focused on a single first step",
                        "channel": "sms"},
    "on_no_interaction": {"action": "One alternate mechanism touch", "channel": "sms"},
    "on_negative": {"action": "stop", "channel": "none"},
})


def reactions_json(value: float) -> str:
    return json.dumps([{"persona_id": p.persona_id, "reaction": value} for p in PERSONAS])


class FakeLLM:
    """Scriptable per-stage gateway fake.

    A response value may be a str (returned every call), a list (consumed one
    per call; the last entry repeats), or an Exception inside a list (raised
    on that call). Stages: evolve (batched generation), critics, rank, screen,
    final, measure.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []  # stage, tier, temperature, prompt
        self._fail: set[str] = set()
        self.responses: dict[str, object] = {
            "evolve": [BATCH_OK],
            "critics": critics_json(["none"] * len(DEFAULT_MECHANISMS)),
            "rank": RANK_OK,
            "screen": [reactions_json(5.3)],
            "final": reactions_json(5.3),
            "measure": MEASURE_JSON,
            "wargame": WARGAME_JSON,
        }

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def calls_for(self, stage: str) -> int:
        return sum(1 for c in self.calls if c["stage"] == stage)

    def prompts_for(self, stage: str) -> list[str]:
        return [c["prompt"] for c in self.calls if c["stage"] == stage]

    def fail_stage(self, stage: str) -> None:
        self._fail.add(stage)

    def _next(self, stage: str) -> str:
        value = self.responses[stage]
        if isinstance(value, list):
            item = value.pop(0) if len(value) > 1 else value[0]
        else:
            item = value
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, str)
        return item

    async def complete(
        self,
        tier: str,
        prompt: str,
        run_id: str,
        stage: str,
        system: str | None = None,
        max_tokens: int = 1200,
        temperature: float | None = None,
    ) -> LLMResult:
        self.calls.append(
            {"stage": stage, "tier": tier, "temperature": temperature, "prompt": prompt}
        )
        if stage in self._fail:
            raise RateLimitExhausted("injected model failure")
        return LLMResult(
            text=self._next(stage),
            model=f"fake-{tier}",
            input_tokens=10,
            output_tokens=5,
            cost_usd=Decimal("0.001"),
        )


class FakeContext:
    def __init__(self) -> None:
        self.unavailable = False
        self.audience_query_version: str | None = None
        self.batch = OrgContextBatch.model_validate_json(
            (FIXTURES / "n8n_context.json").read_text()
        )

    async def fetch(self, pro_ids: list[str]) -> OrgContextBatch:
        if self.unavailable:
            raise ContextUnavailable("injected outage")
        orgs = [o for o in self.batch.organizations if o.pro_id in pro_ids]
        return OrgContextBatch(
            contract_version=CONTRACT_VERSION,
            organizations=orgs,
            audience_query_version=self.audience_query_version,
        )


class CrashableStore(PostgresStore):
    def __init__(self, session) -> None:
        super().__init__(session)
        self.crash_after_stage: str | None = None

    async def complete_stage(self, job_id: str, stage: str, payload=None) -> None:
        await super().complete_stage(job_id, stage, payload)
        if stage == self.crash_after_stage:
            raise InjectedCrash(f"crashed after {stage}")


async def fake_create_plan(winner, llm, catalog) -> MeasurementPlan:
    return MeasurementPlan(
        indicators=[
            MeasurementIndicator(
                key="invoices_sent",
                label="Invoices sent",
                direction="increase",
                source="billing",
                window_days=30,
                rationale="The proposal sends invoices.",
            )
        ]
    )


class NoFleetSlots:
    """No-op stand-in for the fleet limiter; tests don't exercise the cap."""

    async def acquire(self) -> int:
        return 0

    async def release(self, slot: int) -> None:
        pass


class FakeDeps(PipelineDeps):
    def __init__(self, session) -> None:
        store = CrashableStore(session)
        gateway = FakeLLM()

        async def reserve(run_id: str, amount: Decimal) -> bool:
            return await queue_module.reserve_cost(session, run_id, amount)

        async def reconcile(run_id: str, reserved: Decimal, actual: Decimal) -> None:
            await queue_module.reconcile_cost(session, run_id, reserved, actual)

        super().__init__(
            store=store,
            llm=MeteredLLM(
                gateway=gateway,
                records=RecordedCalls(session),
                slots=NoFleetSlots(),
                pricing=FAKE_PRICING,
                reserve=reserve,
                reconcile=reconcile,
            ),
            context=FakeContext(),
            queue=QueueOps(session),
            get_personas=_fake_get_personas,
            calibration=load_calibration(
                Path(__file__).parents[1] / "data" / "reaction_churn_calibration_cards.json"
            ),
            create_plan=create_measurement_plan,
            metric_catalog=METRIC_CATALOG,
        )
        self.db = session
        self.gateway = gateway

    def fail_after(self, stage: str) -> None:
        self.store.crash_after_stage = stage

    def clear_failure(self) -> None:
        self.store.crash_after_stage = None


@pytest.fixture
async def deps(db_session: AsyncSession) -> FakeDeps:
    return FakeDeps(db_session)


@pytest.fixture
async def seeded_job(db_session: AsyncSession):
    run = RunRow(
        id="run-pipe",
        pro_ids=["pro_1"],
        audience_query="audience_v7",
        audience_run="2026-08-06T18:00:00Z",
        channels=["sms"],
        cost_limit=Decimal("100.00"),
    )
    db_session.add(run)
    db_session.add(FleetControlRow(id=1, day_cost_limit=Decimal("1000.00")))
    await db_session.flush()
    job_id = await enqueue(db_session, run.id, stage="pro", pro_id="pro_1")
    await db_session.commit()

    class Seeded:
        id = job_id
        run_id = run.id
        pro_id = "pro_1"

    return Seeded()


# --- app + client fixtures (shared by test_api and test_funnel) --------------

TEST_SETTINGS = Settings(
    _env_file=None,
    DATABASE_URL="postgresql+asyncpg://localhost:5432/waypoint_test",
    LLM_API_KEY="test",
    N8N_CONTEXT_URL="https://n8n.example/webhook/context",
    N8N_TOKEN="test",
    PERSONA_URL="https://personas.example/personas",
    PERSONA_TOKEN="test",
    HANDOFF_URL="https://lcm.example/handoff",
    HANDOFF_TOKEN="lcm-token",
    BYPASS_TOKEN="bypass-secret",
    RUN_COST_USD="25.00",
    DAY_COST_USD="500.00",
    WORKER_COUNT=1,
    MODEL_FAST="claude-haiku-4-5",
    MODEL_DEEP="claude-sonnet-5",
    APP_PASSWORD="operator-password",
    SESSION_KEY="0123456789abcdef0123456789abcdef",
)

# Same app, but with the scoped machine token configured. Kept separate so the
# "no token configured" path stays testable on the default settings.
TOKEN_SETTINGS = TEST_SETTINGS.model_copy(update={"OUTCOMES_TOKEN": SecretStr("tok-good")})


@pytest.fixture
async def client(db_session_factory) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings=TEST_SETTINGS, session_factory=db_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://operator.test") as c:
        yield c


@pytest.fixture
async def auth_client(client: httpx.AsyncClient) -> httpx.AsyncClient:
    response = await client.post("/api/auth/login", json={"password": "operator-password"})
    assert response.status_code == 200
    return client


@pytest.fixture
async def token_client(db_session_factory) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings=TOKEN_SETTINGS, session_factory=db_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://operator.test") as c:
        yield c
