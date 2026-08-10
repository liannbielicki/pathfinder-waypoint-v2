"""Resumable per-Pro worker state machine.

claim → guard → context → evolve (win-stay/lose-shift loop) → 5-person final
check → score/no-action → measurement plan → finalize.

Each Pro is an independently leased durable job. Every stage checkpoints to
Postgres; the evolve loop additionally writes an authoritative round ledger so
a crash or deployment resumes mid-loop without duplicating candidates, charges,
or winners. Every paid call flows through the recorded MeteredLLM path. There
is no canned fallback anywhere: a model failure is a failed job.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint import queue
from waypoint.calls import BudgetExhausted, MeteredLLM
from waypoint.llm import LLMResult
from waypoint.loop import (
    LoopConfig,
    apply_round,
    is_win,
    next_mode,
    replay,
    stop_reason,
)
from waypoint.measurement import UnmeasurableWinner
from waypoint.models import Recommendation
from waypoint.n8n import ContextUnavailable, OrgBrief
from waypoint.personas import (
    InsufficientPanelFit,
    PanelSelection,
    Persona,
    ProMatchInput,
    select_panel,
)
from waypoint.prompts import (
    CRITIC_SYSTEM,
    EVOLVE_SYSTEM,
    REACTION_SYSTEM,
    critic_prompt,
    evolve_prompt,
    reaction_prompt,
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
from waypoint.tables import (
    CandidateRow,
    EvolveRoundRow,
    JobRow,
    MeasurementRow,
    RunRow,
    WinnerRow,
)

STAGES = ("context", "evolve", "final", "score", "measure", "ready")

TERMINAL_STATES = {
    "complete",
    "no_action",
    "abstained",
    "stopped",
    "failed",
    "budget_exhausted",
    "context_unavailable",
    "panel_unavailable",
}

# UI-visible run statuses are the FRONTEND.md set; the specific taxonomy above
# is carried in stop_reason.
_TERMINAL_RUN_STATUSES = {"complete", "no_action", "abstained", "stopped", "failed"}


class LeaseLost(Exception):
    """Another worker owns this job now; stop quietly, it will finish the work."""


class PipelineFailure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ContextLike(Protocol):
    async def fetch(self, pro_ids: list[str]) -> Any: ...


class QueueOps:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def fleet_is_killed(self) -> bool:
        return await queue.fleet_is_killed(self.session)


class PostgresStore:
    """Durable pipeline writes. Stage completion commits atomically."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def load(self, job_id: str) -> tuple[JobRow, RunRow]:
        job = (
            await self.session.execute(
                select(JobRow).where(JobRow.id == job_id).execution_options(populate_existing=True)
            )
        ).scalar_one()
        run = (
            await self.session.execute(
                select(RunRow)
                .where(RunRow.id == job.run_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        return job, run

    async def stage_complete(self, job_id: str, stage: str) -> bool:
        job = await self.session.get(JobRow, job_id)
        return job is not None and stage in job.checkpoint

    async def complete_stage(
        self, job_id: str, stage: str, payload: dict[str, Any] | None = None
    ) -> None:
        await queue.checkpoint_job(self.session, job_id, stage, payload or {"done": True})
        await self.session.commit()

    async def set_run_status(
        self, run_id: str, status: str, stop_reason: str | None = None
    ) -> None:
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

    async def rounds_for(self, run_id: str, pro_id: str) -> list[EvolveRoundRow]:
        return list(
            (
                await self.session.execute(
                    select(EvolveRoundRow)
                    .where(EvolveRoundRow.run_id == run_id, EvolveRoundRow.pro_id == pro_id)
                    .order_by(EvolveRoundRow.round)
                )
            ).scalars()
        )

    async def winner_for(self, run_id: str, pro_id: str) -> WinnerRow | None:
        return (
            await self.session.execute(
                select(WinnerRow).where(WinnerRow.run_id == run_id, WinnerRow.pro_id == pro_id)
            )
        ).scalar_one_or_none()

    async def measurement_for(self, winner_id: str) -> MeasurementRow | None:
        return (
            await self.session.execute(
                select(MeasurementRow).where(MeasurementRow.winner_id == winner_id)
            )
        ).scalar_one_or_none()


@dataclass
class PipelineDeps:
    store: PostgresStore
    llm: MeteredLLM
    context: ContextLike
    queue: QueueOps
    get_personas: Callable[[str], Awaitable[list[Persona]]]
    calibration: Calibration
    create_plan: Any  # async (winner, llm, catalog) -> MeasurementPlan
    metric_catalog: dict[str, Any] = field(default_factory=dict)
    worker_id: str | None = None  # set by the worker; None disables heartbeats
    lease_seconds: int = 600


@dataclass
class PipelineState:
    job: JobRow
    run: RunRow
    pro_id: str
    brief: OrgBrief | None = None


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


async def _guard(state: PipelineState, deps: PipelineDeps) -> None:
    """Heartbeat + kill/stop check, run before EVERY round and paid stage —
    MeteredLLM does not heartbeat, so a long loop would otherwise let the lease
    lapse and a second worker double-pay. A raised BudgetExhausted routes
    through run_job's honest-label handler (fleet kill vs operator stop vs
    budget)."""
    await _heartbeat(state, deps)
    if await deps.queue.fleet_is_killed():
        raise BudgetExhausted("fleet_killed")
    current = (
        await deps.store.session.execute(select(RunRow.status).where(RunRow.id == state.run.id))
    ).scalar_one()
    if current == "stopped":
        raise BudgetExhausted("operator_stop")


async def _abstain_pro(
    state: PipelineState, deps: PipelineDeps, pro_id: str, rationale: str
) -> None:
    if await deps.store.winner_for(state.run.id, pro_id) is None:
        deps.store.session.add(
            WinnerRow(
                run_id=state.run.id,
                pro_id=pro_id,
                kind="abstained",
                rationale=rationale,
            )
        )
        await deps.store.session.commit()


async def _react(
    state: PipelineState,
    deps: PipelineDeps,
    panel: PanelSelection,
    concept: str,
    stage: str,
    tier: str,
    call_key: str,
) -> list[float]:
    panel_json = json.dumps(
        [
            {"persona_id": i.persona_id, "label": i.label, "family": i.family, "role": i.role}
            for i in panel.items
        ]
    )
    # Evaluation is the frozen metric: temperature 0 so the same idea scores
    # the same number every round (Problem 1 in the design spec).
    result = await deps.llm.complete(
        call_key=call_key,
        tier=tier,
        prompt=reaction_prompt(panel_json, concept),
        run_id=state.run.id,
        pro_id=state.pro_id,
        stage=stage,
        system=REACTION_SYSTEM,
        temperature=0.0,
    )
    try:
        by_id = {item["persona_id"]: float(item["reaction"]) for item in _parse_json(result.text)}
    except (ValueError, KeyError, TypeError) as error:
        # A garbled panel abstains this candidate; it must not crash the job.
        raise PipelineFailure(f"{stage}_reactions_unparseable: {error}") from error
    missing = [i.persona_id for i in panel.items if i.persona_id not in by_id]
    if missing:
        raise PipelineFailure(f"{stage}_reactions_missing: {missing}")
    return [by_id[i.persona_id] for i in panel.items]


async def _panel_for(
    state: PipelineState, deps: PipelineDeps, brief: OrgBrief, size: Any
) -> PanelSelection:
    if brief.segment is None:
        # No segment => no shared match key => never a real panel. Abstain
        # honestly instead of guessing a wrong-segment pool.
        raise InsufficientPanelFit(size=size, available=0)
    personas = await deps.get_personas(brief.segment)
    pro = ProMatchInput(pro_id=brief.pro_id, features=dict(brief.match_feature_map()))
    return select_panel(pro, personas, size=size)


async def _resolve_abandoned_calls(state: PipelineState, deps: PipelineDeps) -> None:
    """A crashed attempt's pending calls may have been paid without a visible
    response: convert their worst-case reservations to honest recorded spend."""
    session = deps.llm.records.session
    abandoned = await deps.llm.records.abandon_stale(state.run.id, state.pro_id)
    for row in abandoned:
        await queue.convert_reservation_to_spend(session, state.run.id, row.reserved_usd)
    if abandoned:
        await session.commit()


async def _champion_for(
    state: PipelineState, deps: PipelineDeps, config: LoopConfig
) -> CandidateRow | None:
    """Champion-authoritative lookup: the round ledger decides."""
    ledger = await deps.store.rounds_for(state.run.id, state.pro_id)
    lstate = replay(ledger, config, MIN_REDUCTION_FLOOR_PP)
    if not lstate.best_candidate_id:
        return None
    return await deps.store.session.get(CandidateRow, lstate.best_candidate_id)


# --- stage handlers --------------------------------------------------------


async def _stage_context(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    if state.brief is None:
        # A pro the context flow cannot describe abstains visibly; it never
        # silently vanishes from the run.
        await _abstain_pro(state, deps, state.pro_id, "context missing: no org brief returned")
        return {"orgs": 0, "missing": [state.pro_id]}
    return {"orgs": 1, "missing": []}


async def _stage_evolve(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    """The compounding win-stay/lose-shift loop. One challenger per round; the
    frozen 3-panel screen decides win/lose mechanically; every round persists a
    candidate row + a ledger row atomically, so re-entry replays the ledger and
    the recorded calls make partially-paid rounds free to redo."""
    brief = state.brief
    if brief is None:
        return {"skipped": "no_brief"}
    config = LoopConfig.from_mapping(state.run.loop_config or {})
    ledger = await deps.store.rounds_for(state.run.id, state.pro_id)
    lstate = replay(ledger, config, MIN_REDUCTION_FLOOR_PP)
    history = [
        {"round": r.round, "mechanism": r.mechanism, "score_pp": r.score_pp, "outcome": r.outcome}
        for r in ledger
    ]
    await _resolve_abandoned_calls(state, deps)
    cell = brief.calibration_cell() or ""
    session = deps.store.session

    while (reason := stop_reason(lstate, config)) is None:
        await _guard(state, deps)
        mode = next_mode(lstate, config)
        rnd = lstate.round + 1
        key = f"{state.run.id}:{state.pro_id}:r{rnd}"

        best_json = None
        if lstate.best_candidate_id is not None:
            best = await session.get(CandidateRow, lstate.best_candidate_id)
            best_json = json.dumps(best.recommendation) if best is not None else None
        prompt = evolve_prompt(
            brief.model_dump_json(),
            mode=mode,
            best_json=best_json,
            history_json=json.dumps(history),
            tried_mechanisms=list(lstate.tried_mechanisms),
        )
        try:
            proposal = await deps.llm.complete(
                call_key=f"{key}:generate",
                tier="fast",
                prompt=prompt,
                run_id=state.run.id,
                pro_id=state.pro_id,
                stage="evolve",
                system=EVOLVE_SYSTEM,
            )
            idea = Recommendation.model_validate(_parse_json(proposal.text))
        except BudgetExhausted:
            raise
        except Exception as error:
            raise PipelineFailure(f"evolve_failed: {error}") from error

        try:
            critic = await deps.llm.complete(
                call_key=f"{key}:critic",
                tier="fast",
                prompt=critic_prompt(
                    brief.model_dump_json(), json.dumps([{"idea_index": 0, **idea.model_dump()}])
                ),
                run_id=state.run.id,
                pro_id=state.pro_id,
                stage="critics",
                system=CRITIC_SYSTEM,
            )
            verdicts = {int(v["idea_index"]): v for v in _parse_json(critic.text)}
        except BudgetExhausted:
            raise
        except Exception as error:
            # Fail closed: a dead critic must never silently disable the only
            # grounding gate (legacy incident class).
            raise PipelineFailure(f"critics_failed: {error}") from error
        verdict = verdicts.get(0, {"block_kind": "unreviewed", "reason": "no verdict returned"})

        score: CandidateScore | None = None
        panel = None
        reactions: list[float] | None = None
        if verdict["block_kind"] in ("ungrounded", "unreviewed", "per_pro_data"):
            outcome, score_pp = "suppressed", None  # a loss, no persona spend
        else:
            try:
                panel = await _panel_for(state, deps, brief, 3)
            except InsufficientPanelFit as error:
                await _abstain_pro(state, deps, state.pro_id, f"low panel fit: {error}")
                return {"rounds": lstate.round, "stop": "panel_unavailable"}
            try:
                reactions = await _react(
                    state,
                    deps,
                    panel,
                    idea.pro_facing_concept,
                    "screen",
                    "fast",
                    call_key=f"{key}:screen",
                )
            except PipelineFailure:
                # The evaluation was unavailable this round — an honest loss,
                # never a fabricated score.
                score = score_candidate([], cell, deps.calibration)
                outcome, score_pp = "unavailable", None
            else:
                score = score_candidate(reactions, cell, deps.calibration)
                score_pp = score.reduction_pp
                outcome = (
                    "win" if is_win(lstate, score_pp, config, MIN_REDUCTION_FLOOR_PP) else "lose"
                )

        status = {"win": "champion", "suppressed": "suppressed"}.get(outcome, "discarded")
        candidate = CandidateRow(
            run_id=state.run.id,
            pro_id=state.pro_id,
            recommendation=idea.model_dump(),
            status=status,
            round=rnd,
            critics={"block_kind": verdict["block_kind"], "reason": verdict.get("reason", "")},
        )
        if score is not None:
            candidate.score = {"screen": score.model_dump()}
        if reactions is not None and panel is not None:
            candidate.persona_evidence = {
                "screen": {
                    "panel": panel.model_dump(),
                    "reactions": reactions,
                }
            }
        if outcome == "win" and lstate.best_candidate_id is not None:
            dethroned = await session.get(CandidateRow, lstate.best_candidate_id)
            if dethroned is not None:
                dethroned.status = "discarded"
        session.add(candidate)
        await session.flush()
        lstate = apply_round(
            lstate,
            mechanism=idea.mechanism,
            candidate_id=candidate.id,
            score_pp=score_pp,
            outcome=outcome,
            config=config,
            floor_pp=MIN_REDUCTION_FLOOR_PP,
        )
        session.add(
            EvolveRoundRow(
                run_id=state.run.id,
                pro_id=state.pro_id,
                round=rnd,
                mode=mode,
                mechanism=idea.mechanism,
                candidate_id=candidate.id,
                outcome=outcome,
                score_pp=score_pp,
                best_score_after=lstate.best_score,
            )
        )
        await session.commit()  # candidate + ledger row land atomically
        history.append(
            {"round": rnd, "mechanism": idea.mechanism, "score_pp": score_pp, "outcome": outcome}
        )

    return {"rounds": lstate.round, "stop": reason, "best_score": lstate.best_score}


async def _stage_final(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    """Held-out confirmation: the champion faces the 5-persona panel once.
    ponytail: the loop optimizes the cheap 3-panel proxy; this single held-out
    check catches the catastrophic proxy/judge disagreement (→ no_action). No
    mid-loop revalidation until the proxy is proven to drift."""
    brief = state.brief
    if brief is None or await deps.store.winner_for(state.run.id, state.pro_id) is not None:
        return {}
    config = LoopConfig.from_mapping(state.run.loop_config or {})
    champion = await _champion_for(state, deps, config)
    if champion is None:
        return {}  # resolves to no_action at score
    if "final" in champion.score:
        return {}  # resume
    try:
        panel = await _panel_for(state, deps, brief, 5)
    except InsufficientPanelFit as error:
        await _abstain_pro(state, deps, state.pro_id, f"low panel fit: {error}")
        return {}
    await _guard(state, deps)
    cell = brief.calibration_cell() or ""
    try:
        reactions = await _react(
            state,
            deps,
            panel,
            champion.recommendation["pro_facing_concept"],
            "final",
            "deep",
            call_key=f"{state.run.id}:{state.pro_id}:final",
        )
    except PipelineFailure:
        score = score_candidate([], cell, deps.calibration)
    else:
        score = score_candidate(reactions, cell, deps.calibration)
        champion.persona_evidence = {
            **champion.persona_evidence,
            "final": {
                "panel": panel.model_dump(),
                "reactions": reactions,
            },
        }
    champion.score = {**champion.score, "final": score.model_dump()}
    await deps.store.session.commit()
    return {}


async def _stage_score(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    if await deps.store.winner_for(state.run.id, state.pro_id) is not None:
        return {}
    config = LoopConfig.from_mapping(state.run.loop_config or {})
    champion = await _champion_for(state, deps, config)
    final = champion.score.get("final") if champion is not None else None
    if champion is None or final is None:
        deps.store.session.add(
            WinnerRow(
                run_id=state.run.id,
                pro_id=state.pro_id,
                kind="no_action",
                rationale="no_candidate_cleared_floor",
            )
        )
        await deps.store.session.commit()
        return {}
    outcome = select_winner({champion.id: CandidateScore.model_validate(final)})
    if isinstance(outcome, Winner):
        deps.store.session.add(
            WinnerRow(
                run_id=state.run.id,
                pro_id=state.pro_id,
                kind="winner",
                candidate_id=champion.id,
                rationale=champion.recommendation["manager_rationale"],
                evidence={
                    "final": final,
                    "screen": champion.score.get("screen", {}),
                    "org_id": state.brief.org_uuid if state.brief else "",
                },
            )
        )
    else:
        assert isinstance(outcome, NoAction)
        deps.store.session.add(
            WinnerRow(
                run_id=state.run.id,
                pro_id=state.pro_id,
                kind="no_action",
                rationale=outcome.reason,
            )
        )
    await deps.store.session.commit()
    return {}


class _KeyedMeasureLLM:
    """Adapts the metered facade to measurement.py's legacy signature, pinning
    the deterministic call key — measurement.py stays untouched."""

    def __init__(self, llm: MeteredLLM, pro_id: str) -> None:
        self.llm = llm
        self.pro_id = pro_id

    async def complete(
        self,
        tier: str,
        prompt: str,
        run_id: str,
        stage: str,
        system: str | None = None,
        max_tokens: int = 1200,
    ) -> LLMResult:
        return await self.llm.complete(
            call_key=f"{run_id}:{self.pro_id}:measure",
            tier=tier,
            prompt=prompt,
            run_id=run_id,
            pro_id=self.pro_id,
            stage=stage,
            system=system,
            max_tokens=max_tokens,
        )


async def _stage_measure(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    winner = await deps.store.winner_for(state.run.id, state.pro_id)
    if winner is None or winner.kind != "winner":
        return {}
    if await deps.store.measurement_for(winner.id) is not None:
        return {}  # resume
    candidate = await deps.store.session.get(CandidateRow, winner.candidate_id)
    assert candidate is not None
    await _guard(state, deps)
    context = WinnerContext(
        run_id=state.run.id,
        pro_id=winner.pro_id,
        winner_id=winner.id,
        mechanism=candidate.recommendation["mechanism"],
        title=candidate.recommendation["title"],
    )
    try:
        plan = await deps.create_plan(
            context,
            _KeyedMeasureLLM(deps.llm, state.pro_id),
            deps.metric_catalog,
        )
    except UnmeasurableWinner as error:
        # Never invent a measurement source: an unmeasurable winner abstains.
        # Transient failures (429 storms, outages) propagate instead — a
        # validated winner must survive retryable infrastructure trouble.
        winner.kind = "abstained"
        winner.rationale = f"unmeasurable: {error}"
        await deps.store.session.commit()
        return {}
    deps.store.session.add(
        MeasurementRow(
            run_id=state.run.id,
            winner_id=winner.id,
            indicators=[item.model_dump() for item in plan.indicators],
        )
    )
    await deps.store.session.commit()
    return {}


async def _stage_ready(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    # Ordering rule: commit this job's terminal status FIRST, then finalize
    # from committed rows only — two concurrent finishers either both compute
    # the identical aggregate or the later one does.
    await deps.store.finish_job(state.job.id, "done")
    status = await finalize_run(deps.store.session, state.run.id)
    return {"run_status": status or "pending_siblings"}


async def finalize_run(session: AsyncSession, run_id: str) -> str | None:
    """Champion-authoritative, idempotent run finalization. Computes the
    aggregate status from committed job + winner rows once every per-Pro job is
    terminal; a no-op otherwise or when the run is already terminal."""
    run = (
        await session.execute(
            select(RunRow).where(RunRow.id == run_id).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None or run.status in _TERMINAL_RUN_STATUSES:
        return None
    jobs = list(
        (
            await session.execute(
                select(JobRow)
                .where(JobRow.run_id == run_id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    if not jobs or any(j.status not in ("done", "failed", "stopped") for j in jobs):
        return None
    winners = list(
        (await session.execute(select(WinnerRow).where(WinnerRow.run_id == run_id))).scalars()
    )
    kinds = {w.kind for w in winners}
    failed = [j for j in jobs if j.status == "failed"]
    context_missing = [
        w for w in winners if w.kind == "abstained" and w.rationale.startswith("context missing")
    ]
    reason = None
    if failed and len(failed) == len(jobs):
        status = "failed"
        reason = f"all_pro_jobs_failed: {len(failed)} of {len(jobs)}"
    elif failed:
        status = "degraded"
        reason = f"pro_jobs_failed: {len(failed)} of {len(jobs)}"
    elif context_missing:
        # Infrastructure gaps are degradation, not a legitimate abstention.
        status = "degraded"
        reason = f"context_missing: {len(context_missing)} pros had no org brief"
    elif "winner" in kinds:
        status = "complete"
    elif "no_action" in kinds:
        status = "no_action"
    elif "abstained" in kinds:
        status = "abstained"
    else:
        status = "no_action"
    first_failure = next(
        (
            j.checkpoint.get("failure", {}).get("reason")
            for j in failed
            if j.checkpoint.get("failure", {}).get("reason")
        ),
        None,
    )
    if reason is not None and first_failure:
        reason = f"{reason}; first: {first_failure}"
    run.status = status
    run.stop_reason = reason
    await session.commit()
    return status


@dataclass
class WinnerContext:
    run_id: str
    pro_id: str
    winner_id: str
    mechanism: str
    title: str


STAGE_HANDLERS = {
    "context": _stage_context,
    "evolve": _stage_evolve,
    "final": _stage_final,
    "score": _stage_score,
    "measure": _stage_measure,
    "ready": _stage_ready,
}


async def run_job(job_id: str, deps: PipelineDeps) -> None:
    store = deps.store
    job, run = await store.load(job_id)
    if job.stage == "recommend":
        # ponytail: pre-evolve deploys left run-scoped jobs the new per-Pro
        # pipeline cannot execute; fail them honestly instead of guessing.
        await store.finish_job(job_id, "failed")
        await store.set_run_status(run.id, "failed", "superseded_deploy")
        return
    if run.status in _TERMINAL_RUN_STATUSES:
        if job.status not in ("done", "failed", "stopped"):
            await store.finish_job(job_id, "stopped")
        return
    assert job.pro_id is not None, "per-pro jobs always carry a pro_id"
    run_id = run.id  # plain strings survive session rollbacks; ORM instances expire
    state = PipelineState(job=job, run=run, pro_id=job.pro_id)

    resumed = any(stage in job.checkpoint for stage in STAGES)
    await store.set_run_status(run_id, "resumed" if resumed else "running")

    # Raw context is ephemeral: re-fetched on every (re)entry, never stored.
    try:
        batch = await deps.context.fetch([state.pro_id])
    except ContextUnavailable as error:
        if await store.requeue_job(job_id):
            await store.set_run_status(run_id, "waiting", f"context_unavailable: {error}")
        else:
            await store.set_run_status(run_id, "failed", f"context_unavailable: {error}")
        return
    state.brief = next(
        (brief for brief in batch.organizations if brief.pro_id == state.pro_id),
        None,
    )

    for stage in STAGES:
        if await store.stage_complete(job_id, stage):
            continue
        if await deps.queue.fleet_is_killed():
            await store.set_run_status(run_id, "stopped", "fleet_killed")
            await store.finish_job(job_id, "stopped")
            return
        current = (
            await store.session.execute(select(RunRow.status).where(RunRow.id == run_id))
        ).scalar_one()
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
                status = (
                    await store.session.execute(select(RunRow.status).where(RunRow.id == run_id))
                ).scalar_one()
                if status != "stopped":  # operator kill keeps its own reason
                    await store.set_run_status(run_id, "stopped", "budget_exhausted")
            await store.finish_job(job_id, "stopped")
            return
        except PipelineFailure as error:
            await store.session.rollback()
            # This Pro's job fails; sibling Pros keep working. finalize_run
            # aggregates to failed/degraded once every job is terminal.
            await queue.checkpoint_job(store.session, job_id, "failure", {"reason": error.reason})
            await store.finish_job(job_id, "failed")
            await finalize_run(store.session, run_id)
            return
        await store.complete_stage(job_id, stage, payload)
