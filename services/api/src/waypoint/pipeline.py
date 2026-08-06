"""Resumable worker state machine.

claim → guard → context → generate → critics → 3-person screen → search →
5-person final check → score/no-action → measurement plan → persist.

Every durable stage checkpoints to Postgres. A crash or deployment resumes
from the checkpoint without duplicating candidates, charges, or winners.
There is no canned fallback anywhere: a model failure is a failed run.
"""

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint import queue
from waypoint.llm import LLMResult
from waypoint.measurement import UnmeasurableWinner
from waypoint.models import Recommendation
from waypoint.n8n import ContextUnavailable, OrgBrief, OrgContextBatch
from waypoint.personas import (
    InsufficientPanelFit,
    PanelSelection,
    Persona,
    ProMatchInput,
    select_panel,
)
from waypoint.prompts import (
    CRITIC_SYSTEM,
    GENERATOR_SYSTEM,
    REACTION_SYSTEM,
    critic_prompt,
    generator_prompt,
    reaction_prompt,
    search_directive_prompt,
)
from waypoint.scoring import (
    MIN_REDUCTION_FLOOR_PP,
    Calibration,
    CandidateScore,
    NoAction,
    Winner,
    score_candidate,
    select_winner,
)
from waypoint.tables import CandidateRow, JobRow, MeasurementRow, RunRow, WinnerRow

STAGES = ("context", "generate", "critics", "screen", "search", "final",
          "score", "measure", "ready")

TERMINAL_STATES = {
    "complete", "no_action", "abstained", "stopped", "failed",
    "budget_exhausted", "context_unavailable", "panel_unavailable",
}

# UI-visible run statuses are the FRONTEND.md set; the specific taxonomy above
# is carried in stop_reason.
_TERMINAL_RUN_STATUSES = {"complete", "no_action", "abstained", "stopped", "failed"}

N_IDEAS = 3
# ponytail: flat per-call reservation estimate; refine from measured usage if
# real spend diverges.
ESTIMATED_CALL_COST_USD = Decimal("0.10")


class BudgetExhausted(Exception):
    pass


class LeaseLost(Exception):
    """Another worker owns this job now; stop quietly, it will finish the work."""


class PipelineFailure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LLMLike(Protocol):
    async def complete(self, tier: str, prompt: str, run_id: str, stage: str,
                       system: str | None = None, max_tokens: int = 1200) -> LLMResult: ...


class ContextLike(Protocol):
    async def fetch(self, pro_ids: list[str]) -> OrgContextBatch: ...


class QueueOps:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def fleet_is_killed(self) -> bool:
        return await queue.fleet_is_killed(self.session)

    async def reserve(self, run_id: str, amount: Decimal) -> bool:
        return await queue.reserve_cost(self.session, run_id, amount)


class PostgresStore:
    """Durable pipeline writes. Stage completion commits atomically."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def load(self, job_id: str) -> tuple[JobRow, RunRow]:
        job = (await self.session.execute(
            select(JobRow).where(JobRow.id == job_id).execution_options(populate_existing=True)
        )).scalar_one()
        run = (await self.session.execute(
            select(RunRow).where(RunRow.id == job.run_id).execution_options(populate_existing=True)
        )).scalar_one()
        return job, run

    async def stage_complete(self, job_id: str, stage: str) -> bool:
        job = await self.session.get(JobRow, job_id)
        return job is not None and stage in job.checkpoint

    async def complete_stage(self, job_id: str, stage: str,
                             payload: dict[str, Any] | None = None) -> None:
        await queue.checkpoint_job(self.session, job_id, stage, payload or {"done": True})
        await self.session.commit()

    async def set_run_status(self, run_id: str, status: str,
                             stop_reason: str | None = None) -> None:
        run = await self.session.get(RunRow, run_id)
        assert run is not None
        run.status = status
        run.stop_reason = stop_reason
        await self.session.commit()

    async def finish_job(self, job_id: str, status: str) -> None:
        job = await self.session.get(JobRow, job_id)
        assert job is not None
        job.status = status
        await self.session.commit()

    async def requeue_job(self, job_id: str) -> bool:
        """Requeue for retry. Returns False when attempts are exhausted."""
        job = await self.session.get(JobRow, job_id)
        assert job is not None
        if job.attempts >= job.max_attempts:
            job.status = "failed"
            await self.session.commit()
            return False
        job.status = "queued"
        job.lease_until = None
        await self.session.commit()
        return True

    async def candidates_for(self, run_id: str, pro_id: str) -> list[CandidateRow]:
        return list((await self.session.execute(
            select(CandidateRow).where(
                CandidateRow.run_id == run_id, CandidateRow.pro_id == pro_id
            ).order_by(CandidateRow.created_at, CandidateRow.id)
        )).scalars())

    async def winner_for(self, run_id: str, pro_id: str) -> WinnerRow | None:
        return (await self.session.execute(
            select(WinnerRow).where(WinnerRow.run_id == run_id, WinnerRow.pro_id == pro_id)
        )).scalar_one_or_none()

    async def winners(self, run_id: str) -> list[WinnerRow]:
        return list((await self.session.execute(
            select(WinnerRow).where(WinnerRow.run_id == run_id)
        )).scalars())

    async def measurement_for(self, winner_id: str) -> MeasurementRow | None:
        return (await self.session.execute(
            select(MeasurementRow).where(MeasurementRow.winner_id == winner_id)
        )).scalar_one_or_none()


@dataclass
class PipelineDeps:
    store: PostgresStore
    llm: LLMLike
    context: ContextLike
    queue: QueueOps
    personas: list[Persona]
    calibration: Calibration
    create_plan: Any  # async (winner, llm, catalog) -> MeasurementPlan
    metric_catalog: dict[str, Any] = field(default_factory=dict)
    worker_id: str | None = None  # set by the worker; None disables heartbeats
    lease_seconds: int = 600


@dataclass
class PipelineState:
    job: JobRow
    run: RunRow
    briefs: dict[str, OrgBrief] = field(default_factory=dict)


def _parse_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(cleaned)


async def _heartbeat(state: PipelineState, deps: PipelineDeps) -> None:
    """Extend the lease before paid work; abort if another worker owns the job."""
    if deps.worker_id is None:
        return
    alive = await queue.heartbeat_job(
        deps.store.session, state.job.id, deps.worker_id, deps.lease_seconds
    )
    if not alive:
        raise LeaseLost(state.job.id)


async def _reserve(state: PipelineState, deps: PipelineDeps, calls: int) -> None:
    await _heartbeat(state, deps)
    amount = ESTIMATED_CALL_COST_USD * calls
    if calls and not await deps.queue.reserve(state.run.id, amount):
        raise BudgetExhausted


async def _abstain_pro(state: PipelineState, deps: PipelineDeps, pro_id: str,
                       rationale: str) -> None:
    if await deps.store.winner_for(state.run.id, pro_id) is None:
        deps.store.session.add(WinnerRow(
            run_id=state.run.id, pro_id=pro_id, kind="abstained", rationale=rationale,
        ))
        await deps.store.session.commit()


async def _react(state: PipelineState, deps: PipelineDeps, panel: PanelSelection,
                 concept: str, stage: str, tier: str) -> list[float]:
    await _heartbeat(state, deps)
    panel_json = json.dumps([
        {"persona_id": i.persona_id, "label": i.label, "family": i.family,
         "role": i.role} for i in panel.items
    ])
    result = await deps.llm.complete(
        tier, reaction_prompt(panel_json, concept), state.run.id, stage,
        system=REACTION_SYSTEM,
    )
    try:
        by_id = {
            item["persona_id"]: float(item["reaction"]) for item in _parse_json(result.text)
        }
    except (ValueError, KeyError, TypeError) as error:
        # A garbled panel abstains this candidate; it must not crash the job.
        raise PipelineFailure(f"{stage}_reactions_unparseable: {error}") from error
    missing = [i.persona_id for i in panel.items if i.persona_id not in by_id]
    if missing:
        raise PipelineFailure(f"{stage}_reactions_missing: {missing}")
    return [by_id[i.persona_id] for i in panel.items]


def _panel_for(state: PipelineState, deps: PipelineDeps, brief: OrgBrief,
               size: Any) -> PanelSelection:
    pro = ProMatchInput(pro_id=brief.pro_id, features=dict(brief.match_feature_map()))
    return select_panel(pro, deps.personas, size=size)


async def _generate_for_pro(state: PipelineState, deps: PipelineDeps, brief: OrgBrief,
                            prompt: str, status: str) -> list[CandidateRow]:
    await _reserve(state, deps, 1)
    try:
        result = await deps.llm.complete(
            "fast", prompt, state.run.id, "generate", system=GENERATOR_SYSTEM,
            max_tokens=N_IDEAS * 1200,
        )
        ideas = [Recommendation.model_validate(item) for item in _parse_json(result.text)]
    except BudgetExhausted:
        raise
    except Exception as error:
        raise PipelineFailure(f"generate_failed: {error}") from error
    if not ideas:
        raise PipelineFailure("generate_failed: model returned zero ideas")
    rows = [
        CandidateRow(
            run_id=state.run.id, pro_id=brief.pro_id,
            recommendation=idea.model_dump(), status=status,
        )
        for idea in ideas
    ]
    deps.store.session.add_all(rows)
    await deps.store.session.commit()
    return rows


async def _critic_pass(state: PipelineState, deps: PipelineDeps, brief: OrgBrief,
                       rows: list[CandidateRow]) -> None:
    pending = [row for row in rows if not row.critics]
    if not pending:
        return
    await _reserve(state, deps, 1)
    ideas_json = json.dumps([
        {"idea_index": i, **row.recommendation} for i, row in enumerate(pending)
    ])
    try:
        result = await deps.llm.complete(
            "fast", critic_prompt(brief.model_dump_json(), ideas_json),
            state.run.id, "critics", system=CRITIC_SYSTEM,
        )
        verdicts = {int(v["idea_index"]): v for v in _parse_json(result.text)}
    except BudgetExhausted:
        raise
    except Exception as error:
        # Fail closed: a dead critic must never silently disable the only
        # grounding gate (legacy incident class).
        raise PipelineFailure(f"critics_failed: {error}") from error
    for i, row in enumerate(pending):
        verdict = verdicts.get(i, {"block_kind": "unreviewed", "reason": "no verdict returned"})
        row.critics = {"block_kind": verdict["block_kind"], "reason": verdict.get("reason", "")}
        if verdict["block_kind"] in ("ungrounded", "unreviewed", "per_pro_data"):
            row.status = "suppressed"
    await deps.store.session.commit()


async def _screen_pro(state: PipelineState, deps: PipelineDeps, brief: OrgBrief,
                      rows: list[CandidateRow]) -> None:
    live = [row for row in rows if row.status in ("generated", "search")]
    pending = [row for row in live if "screen" not in row.score]
    if not pending:
        return
    try:
        panel = _panel_for(state, deps, brief, 3)
    except InsufficientPanelFit as error:
        await _abstain_pro(state, deps, brief.pro_id, f"low panel fit: {error}")
        return
    await _reserve(state, deps, len(pending))
    cell = brief.calibration_cell()
    for row in pending:
        concept = row.recommendation["pro_facing_concept"]
        try:
            reactions = await _react(state, deps, panel, concept, "screen", "fast")
        except PipelineFailure:
            score = score_candidate([], cell or "", deps.calibration)
        else:
            score = score_candidate(reactions, cell or "", deps.calibration)
            row.persona_evidence = {**row.persona_evidence, "screen": {
                "panel": panel.model_dump(), "reactions": reactions,
            }}
        row.score = {**row.score, "screen": score.model_dump()}
        await deps.store.session.commit()


def _screen_leader(rows: list[CandidateRow]) -> CandidateRow | None:
    scored = [
        row for row in rows
        if row.status in ("generated", "search")
        and row.score.get("screen", {}).get("reduction_pp") is not None
    ]
    if not scored:
        return None
    return max(scored, key=lambda row: row.score["screen"]["reduction_pp"])


def _clears_screen_floor(row: CandidateRow | None) -> bool:
    if row is None:
        return False
    return float(row.score["screen"]["reduction_pp"]) >= MIN_REDUCTION_FLOOR_PP


# --- stage handlers --------------------------------------------------------


async def _stage_context(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    missing = [p for p in state.run.pro_ids if p not in state.briefs]
    for pro_id in missing:
        # A pro the context flow cannot describe abstains visibly; it never
        # silently vanishes from the run.
        await _abstain_pro(state, deps, pro_id, "context missing: no org brief returned")
    return {"orgs": len(state.briefs), "missing": missing}


async def _stage_generate(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    generated = 0
    for pro_id, brief in state.briefs.items():
        if await deps.store.candidates_for(state.run.id, pro_id):
            continue  # resume: paid work already persisted
        await _generate_for_pro(
            state, deps, brief,
            generator_prompt(brief.model_dump_json(), N_IDEAS), status="generated",
        )
        generated += 1
    return {"pros_generated": generated}


async def _stage_critics(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    for pro_id, brief in state.briefs.items():
        rows = await deps.store.candidates_for(state.run.id, pro_id)
        await _critic_pass(state, deps, brief, rows)
    return {}


async def _stage_screen(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    for pro_id, brief in state.briefs.items():
        rows = await deps.store.candidates_for(state.run.id, pro_id)
        await _screen_pro(state, deps, brief, rows)
    return {}


async def _stage_search(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    """One bounded directive round for pros whose leader missed the floor."""
    retried = 0
    for pro_id, brief in state.briefs.items():
        if await deps.store.winner_for(state.run.id, pro_id) is not None:
            continue
        rows = await deps.store.candidates_for(state.run.id, pro_id)
        if _clears_screen_floor(_screen_leader(rows)):
            continue
        if any(row.status == "search" for row in rows):
            continue  # resume: search round already ran
        if not rows:
            continue
        tried = sorted({row.recommendation["mechanism"] for row in rows})
        new_rows = await _generate_for_pro(
            state, deps, brief,
            search_directive_prompt(brief.model_dump_json(), N_IDEAS, tried),
            status="search",
        )
        await _critic_pass(state, deps, brief, new_rows)
        await _screen_pro(state, deps, brief, await deps.store.candidates_for(
            state.run.id, pro_id))
        retried += 1
    return {"pros_retried": retried}


async def _stage_final(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    for pro_id, brief in state.briefs.items():
        if await deps.store.winner_for(state.run.id, pro_id) is not None:
            continue
        leader = _screen_leader(await deps.store.candidates_for(state.run.id, pro_id))
        if not _clears_screen_floor(leader):
            continue  # resolves to no_action at score
        assert leader is not None
        if "final" in leader.score:
            continue  # resume
        try:
            panel = _panel_for(state, deps, brief, 5)
        except InsufficientPanelFit as error:
            await _abstain_pro(state, deps, pro_id, f"low panel fit: {error}")
            continue
        await _reserve(state, deps, 1)
        cell = brief.calibration_cell()
        try:
            reactions = await _react(
                state, deps, panel, leader.recommendation["pro_facing_concept"],
                "final", "deep",
            )
        except PipelineFailure:
            score = score_candidate([], cell or "", deps.calibration)
        else:
            score = score_candidate(reactions, cell or "", deps.calibration)
            leader.persona_evidence = {**leader.persona_evidence, "final": {
                "panel": panel.model_dump(), "reactions": reactions,
            }}
        leader.score = {**leader.score, "final": score.model_dump()}
        await deps.store.session.commit()
    return {}


async def _stage_score(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    for pro_id in state.briefs:
        if await deps.store.winner_for(state.run.id, pro_id) is not None:
            continue
        leader = _screen_leader(await deps.store.candidates_for(state.run.id, pro_id))
        final = leader.score.get("final") if leader is not None else None
        if leader is None or final is None:
            deps.store.session.add(WinnerRow(
                run_id=state.run.id, pro_id=pro_id, kind="no_action",
                rationale="no_candidate_cleared_floor",
            ))
            await deps.store.session.commit()
            continue
        outcome = select_winner({leader.id: CandidateScore.model_validate(final)})
        if isinstance(outcome, Winner):
            deps.store.session.add(WinnerRow(
                run_id=state.run.id, pro_id=pro_id, kind="winner",
                candidate_id=leader.id,
                rationale=leader.recommendation["manager_rationale"],
                evidence={"final": final, "screen": leader.score.get("screen", {}),
                          "org_id": state.briefs[pro_id].org_id},
            ))
        else:
            assert isinstance(outcome, NoAction)
            deps.store.session.add(WinnerRow(
                run_id=state.run.id, pro_id=pro_id, kind="no_action",
                rationale=outcome.reason,
            ))
        await deps.store.session.commit()
    return {}


async def _stage_measure(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    for winner in await deps.store.winners(state.run.id):
        if winner.kind != "winner":
            continue
        if await deps.store.measurement_for(winner.id) is not None:
            continue  # resume
        candidate = await deps.store.session.get(CandidateRow, winner.candidate_id)
        assert candidate is not None
        await _reserve(state, deps, 1)
        context = WinnerContext(
            run_id=state.run.id, pro_id=winner.pro_id, winner_id=winner.id,
            mechanism=candidate.recommendation["mechanism"],
            title=candidate.recommendation["title"],
        )
        try:
            plan = await deps.create_plan(context, deps.llm, deps.metric_catalog)
        except UnmeasurableWinner as error:
            # Never invent a measurement source: an unmeasurable winner abstains.
            # Transient failures (429 storms, outages) propagate instead — a
            # validated winner must survive retryable infrastructure trouble.
            winner.kind = "abstained"
            winner.rationale = f"unmeasurable: {error}"
            await deps.store.session.commit()
            continue
        deps.store.session.add(MeasurementRow(
            run_id=state.run.id, winner_id=winner.id,
            indicators=[item.model_dump() for item in plan.indicators],
        ))
        await deps.store.session.commit()
    return {}


async def _stage_ready(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    winners = await deps.store.winners(state.run.id)
    kinds = {w.kind for w in winners}
    job = (await deps.store.session.execute(
        select(JobRow).where(JobRow.id == state.job.id).execution_options(populate_existing=True)
    )).scalar_one()  # checkpoint is written via raw SQL; bypass the identity map
    context_missing = bool((job.checkpoint.get("context") or {}).get("missing"))
    stop_reason = None
    if context_missing:
        # Infrastructure gaps are degradation, not a legitimate abstention.
        status = "degraded"
        missing = job.checkpoint["context"]["missing"]
        stop_reason = f"context_missing: {len(missing)} pros had no org brief"
    elif "winner" in kinds:
        status = "complete"
    elif "no_action" in kinds:
        status = "no_action"
    elif "abstained" in kinds:
        status = "abstained"
    else:
        status = "no_action"
    await deps.store.set_run_status(state.run.id, status, stop_reason)
    await deps.store.finish_job(state.job.id, "done")
    return {"status": status}


@dataclass
class WinnerContext:
    run_id: str
    pro_id: str
    winner_id: str
    mechanism: str
    title: str


STAGE_HANDLERS = {
    "context": _stage_context,
    "generate": _stage_generate,
    "critics": _stage_critics,
    "screen": _stage_screen,
    "search": _stage_search,
    "final": _stage_final,
    "score": _stage_score,
    "measure": _stage_measure,
    "ready": _stage_ready,
}


async def run_job(job_id: str, deps: PipelineDeps) -> None:
    store = deps.store
    job, run = await store.load(job_id)
    if run.status in _TERMINAL_RUN_STATUSES:
        return
    run_id = run.id  # plain strings survive session rollbacks; ORM instances expire
    state = PipelineState(job=job, run=run)

    resumed = any(stage in job.checkpoint for stage in STAGES)
    await store.set_run_status(run_id, "resumed" if resumed else "running")

    # Raw context is ephemeral: re-fetched on every (re)entry, never stored.
    try:
        batch = await deps.context.fetch(list(run.pro_ids))
    except ContextUnavailable as error:
        if await store.requeue_job(job_id):
            await store.set_run_status(run_id, "waiting", f"context_unavailable: {error}")
        else:
            await store.set_run_status(run_id, "failed", f"context_unavailable: {error}")
        return
    state.briefs = {brief.pro_id: brief for brief in batch.organizations
                    if brief.pro_id in run.pro_ids}

    for stage in STAGES:
        if await store.stage_complete(job_id, stage):
            continue
        if await deps.queue.fleet_is_killed():
            await store.set_run_status(run_id, "stopped", "fleet_killed")
            await store.finish_job(job_id, "stopped")
            return
        current = (await store.session.execute(
            select(RunRow.status).where(RunRow.id == run_id)
        )).scalar_one()
        if current == "stopped":  # operator killed this run mid-flight
            await store.finish_job(job_id, "stopped")
            return
        try:
            payload = await STAGE_HANDLERS[stage](state, deps)
        except LeaseLost:
            await store.session.rollback()
            return  # the new owner resumes from the durable checkpoint
        except BudgetExhausted:
            await store.session.rollback()
            # A refused reservation has three honest causes; label the real one.
            if await deps.queue.fleet_is_killed():
                await store.set_run_status(run_id, "stopped", "fleet_killed")
            else:
                status = (await store.session.execute(
                    select(RunRow.status).where(RunRow.id == run_id)
                )).scalar_one()
                if status != "stopped":  # operator kill keeps its own reason
                    await store.set_run_status(run_id, "stopped", "budget_exhausted")
            await store.finish_job(job_id, "stopped")
            return
        except PipelineFailure as error:
            await store.session.rollback()
            await store.set_run_status(run_id, "failed", error.reason)
            await store.finish_job(job_id, "failed")
            return
        await store.complete_stage(job_id, stage, payload)
