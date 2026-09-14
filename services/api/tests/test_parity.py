"""Side-by-side contract parity: IDs, payloads, gates, scoring, persistence,
and handoff receipts — not generated-copy equality."""

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pytest_httpx import HTTPXMock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.handoff import LCMClient, ready_rows
from waypoint.measurement import METRIC_CATALOG, select_indicators
from waypoint.models import MeasurementPlan
from waypoint.pipeline import run_job
from waypoint.queue import enqueue
from waypoint.tables import (
    CandidateRow,
    FleetControlRow,
    MeasurementRow,
    RunRow,
    WinnerRow,
)

from .conftest import FakeDeps

LCM_URL = "https://lcm.example/handoff"


def load_parity_cases() -> list[dict[str, Any]]:
    return json.loads((Path(__file__).parent / "fixtures" / "parity_cases.json").read_text())


@dataclass
class ParityResult:
    outcome: str
    org_id: str | None
    context_version: str
    panel_sizes: list[int]
    measurement_plan: MeasurementPlan | None
    handoff: dict[str, Any] | None


class ProductionStack:
    """Full pipeline + real measurement + real handoff client, fake model/context."""

    def __init__(self, session: AsyncSession, httpx_mock: HTTPXMock) -> None:
        self.session = session
        self.httpx_mock = httpx_mock

    async def run(self, pro_id: str) -> ParityResult:
        deps = FakeDeps(self.session)
        deps.create_plan = select_indicators
        deps.metric_catalog = METRIC_CATALOG
        run = RunRow(
            pro_ids=[pro_id],
            audience_query="audience_v7",
            audience_run="2026-08-06T18:00:00Z",
            channels=["sms"],
            cost_limit=Decimal("100.00"),
        )
        self.session.add(run)
        if await self.session.get(FleetControlRow, 1) is None:
            self.session.add(FleetControlRow(id=1, day_cost_limit=Decimal("1000.00")))
        await self.session.flush()
        job_id = await enqueue(self.session, run.id, stage="pro", pro_id=pro_id)
        await self.session.commit()
        await run_job(job_id, deps)

        winner = (
            await self.session.execute(select(WinnerRow).where(WinnerRow.run_id == run.id))
        ).scalar_one()
        batch = await deps.context.fetch([pro_id])

        if winner.kind != "winner":
            return ParityResult(
                outcome=winner.kind,
                org_id=batch.organizations[0].org_uuid,
                context_version=batch.contract_version,
                panel_sizes=[],
                measurement_plan=None,
                handoff=None,
            )

        candidate = await self.session.get(CandidateRow, winner.candidate_id)
        assert candidate is not None
        panel_sizes = [
            len(candidate.persona_evidence["screen"]["panel"]["items"]),
            len(candidate.persona_evidence["final"]["panel"]["items"]),
        ]
        measurement = (
            await self.session.execute(
                select(MeasurementRow).where(MeasurementRow.winner_id == winner.id)
            )
        ).scalar_one()
        plan = MeasurementPlan.model_validate({"indicators": measurement.indicators})

        self.httpx_mock.add_response(url=LCM_URL, json={
            "batch": run.id, "rows": [{"row_id": winner.id, "status": "accepted"}],
        })
        client = LCMClient(url=LCM_URL, token="t", bypass_token="b", session=self.session)
        # The production row shape, not a hand-rolled copy: parity must fail
        # when ready_rows changes.
        await client.handoff(run.id, await ready_rows(self.session, run.id))
        request = self.httpx_mock.get_requests(url=LCM_URL)[-1]
        payload = json.loads(request.content)["rows"][0]
        return ParityResult(
            outcome="winner",
            org_id=winner.evidence["org_id"],
            context_version=batch.contract_version,
            panel_sizes=panel_sizes,
            measurement_plan=plan,
            handoff=payload,
        )


@pytest.fixture
def production_stack(db_session: AsyncSession, httpx_mock: HTTPXMock) -> ProductionStack:
    return ProductionStack(db_session, httpx_mock)


@pytest.mark.parametrize("case", load_parity_cases(), ids=lambda c: c["case"])
async def test_build_contract_parity(
    case: dict[str, Any], production_stack: ProductionStack
) -> None:
    result = await production_stack.run(case["pro_id"])
    assert result.outcome == case["expected_outcome"]
    assert result.org_id == case["expected_org_id"]
    assert result.context_version == case["expected_context_version"]
    assert result.panel_sizes == case["expected_panel_sizes"]
    if case["expected_metric"] is not None:
        assert result.measurement_plan is not None
        assert result.measurement_plan.indicators[0].key == case["expected_metric"]
    if case["expected_handoff_fields"]:
        assert result.handoff is not None
        assert set(result.handoff) >= set(case["expected_handoff_fields"])
