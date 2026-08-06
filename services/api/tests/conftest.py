import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://localhost:5432/waypoint_test"
)

_TABLES = (
    "measurements", "handoffs", "winners", "jobs", "candidates",
    "runs", "llm_usage", "fleet_control",
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

from waypoint.llm import LLMResult, RateLimitExhausted
from waypoint.models import MeasurementIndicator, MeasurementPlan
from waypoint.n8n import ContextUnavailable, OrgContextBatch
from waypoint.personas import Persona
from waypoint.pipeline import PipelineDeps, PostgresStore, QueueOps
from waypoint.queue import enqueue
from waypoint.scoring import load_calibration
from waypoint.tables import FleetControlRow, RunRow

FIXTURES = Path(__file__).parent / "fixtures"


class InjectedCrash(Exception):
    pass


PERSONA_FIXTURE = json.loads((FIXTURES / "personas.json").read_text())
PERSONAS = [
    Persona(snapshot_version=PERSONA_FIXTURE["snapshot_version"], **p)
    for p in PERSONA_FIXTURE["personas"]
]

IDEAS_JSON = json.dumps([
    {
        "title": f"Idea {i}",
        "mechanism": mech,
        "actions": [f"do_{mech}"],
        "pro_facing_concept": f"Concept {i} the pro would experience.",
        "manager_rationale": f"Rationale {i} for the manager.",
        "channel": "sms",
        "risk": "May not land.",
    }
    for i, mech in enumerate(["invoice_delivery", "feature_adoption", "review_requests"])
])

CRITICS_JSON = json.dumps([
    {"idea_index": i, "block_kind": "none", "reason": "grounded"} for i in range(3)
])


def reactions_json(value: float) -> str:
    return json.dumps([
        {"persona_id": p.persona_id, "reaction": value} for p in PERSONAS
    ])


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail: set[str] = set()
        self.responses: dict[str, str] = {
            "generate": IDEAS_JSON,
            "critics": CRITICS_JSON,
            "screen": reactions_json(5.3),
            "final": reactions_json(5.3),
            "search": IDEAS_JSON,
        }

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def calls_for(self, stage: str) -> int:
        return sum(1 for s, _ in self.calls if s == stage)

    def fail_stage(self, stage: str) -> None:
        self._fail.add(stage)

    async def complete(self, tier: str, prompt: str, run_id: str, stage: str,
                       system: str | None = None, max_tokens: int = 1200) -> LLMResult:
        self.calls.append((stage, tier))
        if stage in self._fail:
            raise RateLimitExhausted("injected model failure")
        return LLMResult(
            text=self.responses[stage], model=f"fake-{tier}",
            input_tokens=10, output_tokens=5, cost_usd=Decimal("0.001"),
        )


class FakeContext:
    def __init__(self) -> None:
        self.unavailable = False
        self.batch = OrgContextBatch.model_validate_json(
            (FIXTURES / "n8n_context.json").read_text()
        )

    async def fetch(self, pro_ids: list[str]) -> OrgContextBatch:
        if self.unavailable:
            raise ContextUnavailable("injected outage")
        orgs = [o for o in self.batch.organizations if o.pro_id in pro_ids]
        return OrgContextBatch(contract_version="org_context_v1", organizations=orgs)


class CrashableStore(PostgresStore):
    def __init__(self, session) -> None:
        super().__init__(session)
        self.crash_after_stage: str | None = None

    async def complete_stage(self, job_id: str, stage: str, payload=None) -> None:
        await super().complete_stage(job_id, stage, payload)
        if stage == self.crash_after_stage:
            raise InjectedCrash(f"crashed after {stage}")


async def fake_create_plan(winner, llm, catalog) -> MeasurementPlan:
    return MeasurementPlan(indicators=[MeasurementIndicator(
        key="invoices_sent", label="Invoices sent", direction="increase",
        source="billing", window_days=30, rationale="The proposal sends invoices.",
    )])


class FakeDeps(PipelineDeps):
    def __init__(self, session) -> None:
        store = CrashableStore(session)
        super().__init__(
            store=store,
            llm=FakeLLM(),
            context=FakeContext(),
            queue=QueueOps(session),
            personas=PERSONAS,
            calibration=load_calibration(
                Path(__file__).parents[1] / "data" / "reaction_churn_calibration_cards.json"
            ),
            create_plan=fake_create_plan,
        )
        self.db = session

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
        id="run-pipe", pro_ids=["pro_1"], audience_query="audience_v7",
        audience_run="2026-08-06T18:00:00Z", channels=["sms"],
        cost_limit=Decimal("100.00"),
    )
    db_session.add(run)
    db_session.add(FleetControlRow(id=1, day_cost_limit=Decimal("1000.00")))
    await db_session.flush()
    job_id = await enqueue(db_session, run.id, stage="recommend")
    await db_session.commit()

    class Seeded:
        id = job_id
        run_id = run.id

    return Seeded()
