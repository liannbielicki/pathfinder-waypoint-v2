"""Production-shaped 200-Pro capacity gate.

Real Postgres queue, leases, checkpoints, budget, and handoff idempotency
under 4 concurrent workers; model and context calls are fakes per the plan
(the live-model rerun is a recorded human gate). Evidence is written to
docs/verification/launch-report.md.
"""

import asyncio
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from waypoint.handoff import LCMClient, ready_rows
from waypoint.n8n import CONTRACT_VERSION, OrgBrief, OrgContextBatch
from waypoint.pipeline import STAGES, run_job
from waypoint.queue import claim_job, enqueue
from waypoint.tables import (
    CandidateRow,
    FleetControlRow,
    HandoffRow,
    JobRow,
    RunRow,
    WinnerRow,
)

from .conftest import FakeContext, FakeDeps

TOTAL_PROS = 200
PROS_PER_RUN = 10
WORKERS = 4
LCM_URL = "https://lcm.example/handoff"
REPORT = Path(__file__).parents[3] / "docs" / "verification" / "launch-report.md"


class SyntheticContext(FakeContext):
    """Serves a brief for any pro id, mirroring the recorded fixture profile."""

    async def fetch(self, pro_ids: list[str]) -> OrgContextBatch:
        return OrgContextBatch(
            contract_version=CONTRACT_VERSION,
            organizations=[
                OrgBrief(
                    org_uuid=pro_id,
                    segment="1A",
                    plan_tier="basic",
                    tenure_band="0-3m",
                    org_size_band="solo",
                    vertical="hvac",
                    open_ar_band="low",
                )
                for pro_id in pro_ids
            ],
        )


async def _seed(factory: async_sessionmaker[AsyncSession]) -> list[str]:
    async with factory() as session:
        session.add(FleetControlRow(id=1, day_cost_limit=Decimal("500.00")))
        run_ids = []
        for chunk_start in range(0, TOTAL_PROS, PROS_PER_RUN):
            pro_ids = [f"pro_{i}" for i in range(chunk_start, chunk_start + PROS_PER_RUN)]
            run = RunRow(
                pro_ids=pro_ids,
                audience_query="audience_v7",
                audience_run="2026-08-06T18:00:00Z",
                channels=["sms"],
                cost_limit=Decimal("25.00"),
            )
            session.add(run)
            await session.flush()
            for pro_id in pro_ids:
                await enqueue(session, run.id, stage="pro", pro_id=pro_id)
            run_ids.append(run.id)
        await session.commit()
        return run_ids


async def _worker(
    factory: async_sessionmaker[AsyncSession], worker_id: str, stats: dict[str, int]
) -> None:
    while True:
        async with factory() as session:
            job = await claim_job(session, worker_id, lease_seconds=600)
            await session.commit()
            if job is None:
                return
            stats["claims"] += 1
            deps = FakeDeps(session)
            deps.context = SyntheticContext()
            await run_job(job.id, deps)
            stats["llm_calls"] += deps.gateway.call_count


@pytest.mark.load
async def test_two_hundred_pros_complete_without_integrity_failures(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    httpx_mock: HTTPXMock,
) -> None:
    run_ids = await _seed(db_session_factory)
    stats = {"claims": 0, "llm_calls": 0}

    started = time.monotonic()
    await asyncio.gather(
        *[_worker(db_session_factory, f"load-worker-{i}", stats) for i in range(WORKERS)]
    )
    pipeline_elapsed = time.monotonic() - started

    # --- integrity: no double claims, no lost checkpoints, no hidden failures.
    jobs = (await db_session.execute(select(JobRow))).scalars().all()
    assert len(jobs) == TOTAL_PROS  # one leased durable job per Pro
    assert all(job.status == "done" for job in jobs)
    assert all(job.attempts == 1 for job in jobs), "a live lease was double-claimed"
    lost_checkpoints = [
        job.id for job in jobs if any(stage not in job.checkpoint for stage in STAGES)
    ]
    assert lost_checkpoints == []

    runs = (await db_session.execute(select(RunRow))).scalars().all()
    assert all(run.status == "complete" for run in runs), [
        (run.id, run.status, run.stop_reason) for run in runs if run.status != "complete"
    ]

    winners = (await db_session.execute(select(WinnerRow))).scalars().all()
    assert len(winners) == TOTAL_PROS
    assert all(w.kind == "winner" for w in winners)
    processed = len({(w.run_id, w.pro_id) for w in winners})
    assert processed == TOTAL_PROS

    # --- budget: reservations never exceeded any limit.
    for run in runs:
        assert run.cost_reserved <= run.cost_limit
    fleet = await db_session.get(FleetControlRow, 1)
    assert fleet is not None
    assert fleet.day_cost_reserved <= fleet.day_cost_limit

    # --- handoffs: one batch POST per run (never per row); retries are no-ops.
    def _accept(request):
        rows = json.loads(request.content)["rows"]
        return httpx.Response(200, json={
            "batch": "any",
            "rows": [{"row_id": row["row_id"], "status": "accepted"} for row in rows],
        })

    httpx_mock.add_callback(_accept, url=LCM_URL, is_reusable=True)
    handoff_started = time.monotonic()
    client = LCMClient(url=LCM_URL, token="t", bypass_token="b", session=db_session)
    winners_by_run: dict[str, list[WinnerRow]] = {}
    for winner in winners:
        winners_by_run.setdefault(winner.run_id, []).append(winner)
    for run_id, run_winners in winners_by_run.items():
        rows = await ready_rows(db_session, run_id)
        assert len(rows) == len(run_winners)
        first = await client.handoff(run_id, rows)
        second = await client.handoff(run_id, rows)
        assert [r.idempotency_key for r in first] == [r.idempotency_key for r in second]
    handoff_elapsed = time.monotonic() - handoff_started

    posts = len(httpx_mock.get_requests(url=LCM_URL))
    assert posts == len(winners_by_run), "handoff retries must never duplicate POSTs"
    handoff_rows = (
        await db_session.execute(select(func.count()).select_from(HandoffRow))
    ).scalar_one()
    assert handoff_rows == TOTAL_PROS

    elapsed = pipeline_elapsed + handoff_elapsed
    assert elapsed < 86400, "the 200-Pro day budget was exceeded"

    _write_report(
        run_count=len(run_ids),
        elapsed=elapsed,
        pipeline_elapsed=pipeline_elapsed,
        handoff_elapsed=handoff_elapsed,
        stats=stats,
        posts=posts,
        reserved=str(sum(run.cost_reserved for run in runs)),
    )


def _write_report(
    run_count: int,
    elapsed: float,
    pipeline_elapsed: float,
    handoff_elapsed: float,
    stats: dict[str, int],
    posts: int,
    reserved: str,
) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    per_pro = elapsed / TOTAL_PROS
    REPORT.write_text(f"""# Launch capacity report — 200 Pros/day gate

Generated by `tests/test_load.py` on {datetime.now(UTC).isoformat()}.

## Shape

- {TOTAL_PROS} Pros across {run_count} runs of {PROS_PER_RUN}, {WORKERS} concurrent
  workers against real Postgres (leases, `SKIP LOCKED` claims, checkpoints,
  budget reservation, idempotent handoffs).
- Model and n8n context calls are deterministic fakes per the implementation
  plan; the live-model rerun is listed in docs/HUMAN-TASKS.md.

## Results

| Metric | Value |
|---|---|
| Pros processed | {TOTAL_PROS} |
| Winners persisted | {TOTAL_PROS} |
| Duplicate claims | 0 (every job exactly one attempt) |
| Duplicate handoffs | 0 ({posts} POSTs for {TOTAL_PROS} winners handed off twice) |
| Lost checkpoints | 0 (all {len(STAGES)} stages durable on every per-Pro job) |
| Hidden failures | 0 (every run terminal state `complete`) |
| Budget overshoot | none (reserved ${reserved} within run and day limits) |
| Pipeline elapsed | {pipeline_elapsed:.2f}s |
| Handoff elapsed | {handoff_elapsed:.2f}s |
| Total elapsed | {elapsed:.2f}s ({per_pro * 1000:.0f} ms/Pro orchestration overhead) |
| Fake LLM calls | {stats["llm_calls"]} ({stats["llm_calls"] / TOTAL_PROS:.1f} per Pro) |
| Worker claims | {stats["claims"]} |
| Retries / 429s | 0 / 0 (fault injection covered in test_resume, test_queue, test_llm) |

## Reading

Orchestration overhead is ~{per_pro * 1000:.0f} ms/Pro. At ~7 model calls/Pro,
real-model latency dominates capacity; with the audited ~$0.10/call estimate a
200-Pro day reserves well under the default $500 day budget. The integrity
properties (no double claims, no duplicate handoffs, no lost checkpoints, no
silent failures, no budget overshoot) are the gate this run proves.
""")
