"""Resumable per-Pro worker state machine.

claim → guard → context → evolve (win-stay/lose-shift loop) → 5-person final
check → score/no-action → measurement plan → finalize.

Each Pro is an independently leased durable job. Every stage checkpoints to
Postgres; the evolve loop additionally writes an authoritative round ledger so
a crash or deployment resumes mid-loop without duplicating candidates, charges,
or winners. Every paid call flows through the recorded MeteredLLM path. There
is no canned fallback anywhere: a model failure is a failed job.
"""

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from typing import Any, Protocol
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint import queue
from waypoint.calls import BudgetExhausted, MeteredLLM
from waypoint.evidence import evidence_block, failed_mechanisms, pattern_summaries
from waypoint.feasibility import gate_pro
from waypoint.llm import LLMResult, Pricing, RateLimitExhausted, extract_json, worst_case_cost
from waypoint.loop import (
    MAX_CANDIDATE_COUNT,
    LoopConfig,
    apply_round,
    is_win,
    next_mode,
    replay,
    stop_reason,
)
from waypoint.measurement import UnmeasurableWinner
from waypoint.models import (
    PENDING_AUDIENCE_QUERY,
    TERMINAL_RUN_STATUSES,
    FollowUpPlan,
    RankerDecision,
    Recommendation,
    validate_ranking,
)
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
    PROMPT_VERSION,
    RANKER_SYSTEM,
    REACTION_SYSTEM,
    WAR_GAME_SYSTEM,
    critic_prompt,
    evolve_prompt,
    ranker_prompt,
    reaction_prompt,
    war_game_prompt,
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
    PersonaEvalRow,
    RunRow,
    WinnerRow,
)
from waypoint.warmstart import FINGERPRINT_VERSION, build_fingerprint, retrieve

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
    # Independent (MeteredLLM, cache session) stacks for concurrent paid calls —
    # advisory-lock connections and sessions are never shared across tasks. None
    # means "screen sequentially"; the worker wires the factory.
    llm_stacks: (
        Callable[[], AbstractAsyncContextManager[tuple[MeteredLLM, AsyncSession]]] | None
    ) = None


@dataclass
class PipelineState:
    job: JobRow
    run: RunRow
    pro_id: str
    brief: OrgBrief | None = None


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


def _reaction_cache_key(panel: PanelSelection, concept: str, channel: str, tier: str) -> str:
    ids = sorted(i.persona_id for i in panel.items)
    raw = json.dumps([PROMPT_VERSION, panel.snapshot_version, tier, ids, concept, channel])
    return hashlib.sha256(raw.encode()).hexdigest()


async def _react(
    state: PipelineState,
    deps: PipelineDeps,
    panel: PanelSelection,
    cards: dict[str, dict[str, Any]],
    concept: str,
    channel: str,
    stage: str,
    tier: str,
    call_key: str,
    llm: MeteredLLM | None = None,
    cache_session: AsyncSession | None = None,
) -> list[float]:
    # Spec: reuse persona evaluation where persona set, touch pattern, and
    # channel are materially equivalent. Evaluation is temperature-0, so a
    # cached reaction is the same number the model would return.
    # Safe to commit here: _react is called with a clean session — the caller's
    # candidate/ledger rows are flushed/committed after _react returns, never
    # concurrently — so this commit only ever carries the cache row.
    # llm/cache_session default to the job's stack; concurrent screens pass their
    # own so no session or advisory-lock connection is shared across tasks.
    llm = llm if llm is not None else deps.llm
    session = cache_session if cache_session is not None else deps.store.session
    key = _reaction_cache_key(panel, concept, channel, tier)
    cached = (
        await session.execute(select(PersonaEvalRow).where(PersonaEvalRow.cache_key == key))
    ).scalar_one_or_none()
    if cached is not None and all(i.persona_id in cached.reactions for i in panel.items):
        return [float(cached.reactions[i.persona_id]) for i in panel.items]

    # The FULL persona card goes to the model — a bare label + role produced
    # constant role-driven ratings ([6,6,4] every round). data_provenance is
    # metadata, and empty values are dead tokens.
    panel_json = json.dumps(
        [
            {
                "persona_id": i.persona_id,
                "role": i.role,
                "card": {
                    k: v
                    for k, v in cards.get(i.persona_id, {}).items()
                    if k != "data_provenance" and v not in (None, "", [], {})
                },
            }
            for i in panel.items
        ]
    )
    # Evaluation is the frozen metric: temperature 0 so the same idea scores
    # the same number every round (Problem 1 in the design spec).
    result = await llm.complete(
        call_key=call_key,
        tier=tier,
        prompt=reaction_prompt(panel_json, concept, channel),
        run_id=state.run.id,
        pro_id=state.pro_id,
        stage=stage,
        system=REACTION_SYSTEM,
        temperature=0.0,
    )
    try:
        by_id = {item["persona_id"]: float(item["reaction"]) for item in extract_json(result.text)}
    except (ValueError, KeyError, TypeError) as error:
        # A garbled panel abstains this candidate; it must not crash the job.
        raise PipelineFailure(f"{stage}_reactions_unparseable: {error}") from error
    missing = [i.persona_id for i in panel.items if i.persona_id not in by_id]
    if missing:
        raise PipelineFailure(f"{stage}_reactions_missing: {missing}")
    session.add(
        PersonaEvalRow(
            cache_key=key,
            reactions={i.persona_id: by_id[i.persona_id] for i in panel.items},
            snapshot_version=panel.snapshot_version,
        )
    )
    try:
        await session.commit()
    except IntegrityError:  # a sibling worker cached it first; theirs wins
        await session.rollback()
    return [by_id[i.persona_id] for i in panel.items]


async def _panel_for(
    state: PipelineState, deps: PipelineDeps, brief: OrgBrief, size: Any
) -> tuple[PanelSelection, dict[str, dict[str, Any]]]:
    """Select the panel AND return each member's full card features, keyed by
    persona_id — the reaction prompt needs the substance, not just the labels
    that panel evidence stores."""
    if brief.segment is None:
        # No segment => no shared match key => never a real panel. Abstain
        # honestly instead of guessing a wrong-segment pool.
        raise InsufficientPanelFit(size=size, available=0)
    personas = await deps.get_personas(brief.segment)
    pro = ProMatchInput(pro_id=brief.pro_id, features=dict(brief.match_feature_map()))
    panel = select_panel(pro, personas, size=size)
    features = {p.persona_id: p.features for p in personas}
    return panel, {i.persona_id: features.get(i.persona_id, {}) for i in panel.items}


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
    lstate = replay(ledger, config)
    if not lstate.best_candidate_id:
        return None
    return await deps.store.session.get(CandidateRow, lstate.best_candidate_id)


# A model occasionally returns valid JSON that omits a required field (the
# `actions`-missing prod incident) or is otherwise unparseable. Re-ask under a
# FRESH call key — the recorded-call cache is keyed by call_key, so retrying the
# same key would just replay the bad response (and would also wedge job-level
# resume, since the deterministic key stays poisoned). The default (non-zero)
# temperature makes each attempt vary. Fail closed after the attempt budget.
JSON_CALL_ATTEMPTS = 3


async def _valid_json_call(
    deps: PipelineDeps,
    *,
    base_key: str,
    tier: str,
    prompt: str,
    run_id: str,
    pro_id: str,
    stage: str,
    system: str,
    parse: Callable[[str], Any],
    temperature: float | None = None,
    max_tokens: int = 1200,
) -> Any:
    last: Exception | None = None
    for attempt in range(JSON_CALL_ATTEMPTS):
        call_key = base_key if attempt == 0 else f"{base_key}:retry{attempt}"
        try:
            result = await deps.llm.complete(
                call_key=call_key,
                tier=tier,
                prompt=prompt,
                run_id=run_id,
                pro_id=pro_id,
                stage=stage,
                system=system,
                max_tokens=max_tokens,
                # Attempt 0 honors the caller's temperature (0.0 for the
                # deterministic ranker); re-asks fall back to the default
                # sampling temperature so a deterministic bad output can vary.
                temperature=temperature if attempt == 0 else None,
            )
        except BudgetExhausted:
            raise
        except RateLimitExhausted as error:
            # Distinct label so a 429 storm is attributable: MAX_LLM_IN_FLIGHT is
            # too high for the model tier (lower it or raise the Anthropic tier),
            # NOT a code bug. Not retried — the gateway already backed off.
            raise PipelineFailure(f"{stage}_rate_limited: {error}") from error
        except Exception as error:
            # Any other call/infra failure is an honest job failure — do not
            # re-ask, that only hammers a struggling provider.
            raise PipelineFailure(f"{stage}_failed: {error}") from error
        try:
            return parse(result.text)
        except Exception as error:  # noqa: BLE001 — bad OUTPUT: re-ask under a fresh key
            last = error
    raise PipelineFailure(f"{stage}_invalid_output after {JSON_CALL_ATTEMPTS} attempts: {last}")


# --- stage handlers --------------------------------------------------------

_CONSENT_ASK = re.compile(
    r"\b(opt[ -]?in|consent|permission to (text|message|contact))\b", re.IGNORECASE
)


def _is_consent_ask(idea: Recommendation) -> bool:
    """Deterministic consent-ask gate on the pro-facing surfaces of an idea.
    manager_rationale/risk are excluded: they may legitimately discuss the
    Pro's consent STATE without the touch asking for consent."""
    return bool(_CONSENT_ASK.search(" ".join([idea.title, idea.pro_facing_concept, *idea.actions])))


async def _stage_context(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    if state.brief is None:
        # A pro the context flow cannot describe abstains visibly; it never
        # silently vanishes from the run.
        await _abstain_pro(state, deps, state.pro_id, "context missing: no org brief returned")
        return {"orgs": 0, "missing": [state.pro_id]}
    return {"orgs": 1, "missing": []}


# --- evolve round: batch generation, batch critic, ranking, tie screening ----

# Bounded re-asks for a short batch: a model that keeps repeating mechanisms
# must never turn one round into an open-ended generation bill.
MAX_BATCH_REFILLS = 2

# Verdict kinds that bench a candidate before any persona spend. consent_ask is
# also enforced deterministically (see _is_consent_ask) — both prompt layers
# that ban it are probabilistic, so a batched idea gets the same hard backstop.
SUPPRESSING_BLOCK_KINDS = (
    "ungrounded",
    "unreviewed",
    "per_pro_data",
    "consent_ask",
    "infeasible_channel",
    "recently_failed",
)


def _batch_max_tokens(count: int) -> int:
    """1200 tokens per idea, never capped below what the batch needs. A ceiling
    that truncates the array mid-JSON is unparseable — extract_json cannot
    salvage a cut-off array — so the round burns every JSON_CALL_ATTEMPTS re-ask
    at full price and then fails. MAX_CANDIDATE_COUNT (+1 for a warm start) is
    what bounds the batch, so this stays bounded with it."""
    return 1200 * min(count, MAX_CANDIDATE_COUNT + 1)


def _ranker_tier(pricing: Pricing) -> str:
    """The ranker runs on its own configured tier when the worker wired one;
    otherwise it shares the fast tier."""
    return "rank" if "rank" in pricing.models else "fast"


def _parse_idea_batch(text: str) -> list[Recommendation]:
    """Lenient batch parse: a bare object counts as a one-item batch and
    malformed items are dropped (one bad idea must not waste the whole call).
    Zero valid ideas raises so _valid_json_call re-asks under a fresh key."""
    data = extract_json(text)
    items = [data] if isinstance(data, dict) else list(data)
    ideas: list[Recommendation] = []
    for item in items:
        try:
            ideas.append(Recommendation.model_validate(item))
        except ValidationError:
            continue
    if not ideas:
        raise ValueError("no valid idea in the generated batch")
    return ideas


def _dedupe_mechanisms(
    ideas: list[Recommendation], count: int, *, keep: str | None = None
) -> list[Recommendation]:
    """One idea per mechanism (first wins), truncated to the requested count —
    a batch of near-identical mechanisms is one candidate, not N. `keep`, when
    present in the batch, is guaranteed its slot: a warm-start mechanism must
    not be truncated out by the ideas generated beside it."""
    held: dict[str, Recommendation] = {}
    for idea in ideas:
        held.setdefault(idea.mechanism, idea)
    ordered = list(held.values())
    if keep is not None and keep in held:
        ordered = [held[keep], *(i for i in ordered if i.mechanism != keep)]
    return ordered[:count]


def _round_worst_case(deps: PipelineDeps, prompt: str, count: int) -> Decimal:
    """Upper bound on ONE round: generation plus every allowed refill, the batch
    critic, the ranker, and two persona screens (the tied-finalist case).

    Every JSON-parsed stage can re-ask up to JSON_CALL_ATTEMPTS times on bad
    output, and each re-ask is a fresh PAID call — counting them once would
    under-reserve the round by 3x. Screens go straight through MeteredLLM with
    no re-ask, so they stay one call each.

    The generation prompt is the size proxy — the critic/ranker prompts carry
    the same org context plus the batch, so it is the right order of magnitude."""
    pricing = deps.llm.pricing
    batch_tokens = _batch_max_tokens(count)
    generate = worst_case_cost(pricing, "fast", prompt, EVOLVE_SYSTEM, batch_tokens)
    critic = worst_case_cost(pricing, "fast", prompt, CRITIC_SYSTEM, batch_tokens)
    rank = worst_case_cost(pricing, _ranker_tier(pricing), prompt, RANKER_SYSTEM, 1200)
    screen = worst_case_cost(pricing, "fast", prompt, REACTION_SYSTEM, 1200)
    retriable = generate * (1 + MAX_BATCH_REFILLS) + critic + rank
    return retriable * JSON_CALL_ATTEMPTS + 2 * screen


async def _reserve_round_worst_case(state: PipelineState, deps: PipelineDeps, worst: Decimal) -> None:
    """Preflight the round's worst case before ANY paid call of the round: a
    batch round that ran out of money halfway would leave paid ideas nobody
    ranked or screened. Check-then-act inside one transaction — the reservation
    proves the whole round fits the remaining budget at this instant, then is
    released immediately so the MeteredLLM path can reserve and reconcile each
    call's own worst case. It is not a hold carried through the round, so a
    concurrent sibling job can still win the same headroom; per-call
    reservations remain the hard stop."""
    if not await deps.llm.reserve(state.run.id, worst):
        raise BudgetExhausted(f"round_worst_case:{state.run.id}:{state.pro_id}")
    await deps.llm.reconcile(state.run.id, worst, Decimal(0))
    await deps.llm.records.session.commit()


def _prompt_builder(
    *,
    org_context: str,
    best_json: str | None,
    history_json: str,
    channels: list[str],
    journey_window: str,
    evidence: str,
) -> Callable[..., str]:
    """(mode, count, forbidden mechanisms[, warm mechanism]) -> evolve prompt,
    with this round's context bound once — refills reuse it with a different
    count and forbidden list (and never a warm start: it is already in the
    batch)."""

    def build(mode: str, ask: int, forbidden: list[str], warm: str | None = None) -> str:
        return evolve_prompt(
            org_context,
            mode=mode,
            best_json=best_json,
            history_json=history_json,
            tried_mechanisms=forbidden,
            channels=channels,
            journey_window=journey_window,
            evidence=evidence,
            count=ask,
            warm_start_mechanism=warm,
        )

    return build


async def _generate_batch(
    state: PipelineState,
    deps: PipelineDeps,
    *,
    key: str,
    count: int,
    prompt: str,
    build_prompt: Callable[..., str],
    tried: list[str],
    warm: str | None = None,
) -> list[Recommendation]:
    """One batched generation call, then bounded refills for whatever the
    dedupe dropped. A refill that fails outright is tolerated — bounded means
    bounded, and the round proceeds with the ideas it already holds.

    A `warm` mechanism is protected end to end: it is kept through dedupe and,
    if the batch comes back without it, each refill re-requests it (the model
    routinely ignores a single embedded instruction). Otherwise the proven
    cross-pro mechanism would silently never enter the candidate set."""

    async def generate(base_key: str, ask: int, text: str) -> list[Recommendation]:
        batch: list[Recommendation] = await _valid_json_call(
            deps,
            base_key=base_key,
            tier="fast",
            prompt=text,
            run_id=state.run.id,
            pro_id=state.pro_id,
            stage="evolve",
            system=EVOLVE_SYSTEM,
            max_tokens=_batch_max_tokens(ask),
            parse=_parse_idea_batch,
        )
        return batch

    ideas = _dedupe_mechanisms(await generate(f"{key}:generate", count, prompt), count, keep=warm)
    for refill in range(MAX_BATCH_REFILLS):
        warm_missing = warm is not None and all(idea.mechanism != warm for idea in ideas)
        if len(ideas) >= count and not warm_missing:
            break
        missing = max(count - len(ideas), 1 if warm_missing else 0)
        forbidden = list(dict.fromkeys([*tried, *(idea.mechanism for idea in ideas)]))
        try:
            more = await generate(
                f"{key}:refill{refill}",
                missing,
                build_prompt("shift", missing, forbidden, warm if warm_missing else None),
            )
        except PipelineFailure:
            break
        ideas = _dedupe_mechanisms([*ideas, *more], count, keep=warm)
    return ideas


async def _verdicts_for_batch(
    state: PipelineState,
    deps: PipelineDeps,
    *,
    key: str,
    org_context: str,
    ideas: list[Recommendation],
    channels: list[str],
    failed: set[str],
) -> list[dict[str, Any]]:
    """Pre-gate for free, then ONE critic call for everything that survives.
    The critic is only paid for ideas that clear the recently-failed and
    channel-feasibility gates."""
    verdicts: list[dict[str, Any] | None] = [None] * len(ideas)
    review: list[tuple[int, Recommendation]] = []
    for index, idea in enumerate(ideas):
        if idea.mechanism in failed:
            # Spec gate: not materially different from a recent failed touch.
            verdicts[index] = {
                "block_kind": "recently_failed",
                "reason": f"mechanism {idea.mechanism!r} recently failed for this pro",
            }
        elif idea.channel != "none" and idea.channel not in channels:
            verdicts[index] = {
                "block_kind": "infeasible_channel",
                "reason": f"channel {idea.channel!r} blocked by the consent gate",
            }
        else:
            review.append((index, idea))
    if review:
        # Fail closed: a dead critic must never silently disable the only
        # grounding gate (legacy incident class) — _valid_json_call raises
        # PipelineFailure after retries, it never returns an empty verdict set.
        reviewed = await _valid_json_call(
            deps,
            base_key=f"{key}:critic",
            tier="fast",
            prompt=critic_prompt(
                org_context,
                json.dumps([{"idea_index": i, **idea.model_dump()} for i, idea in review]),
            ),
            run_id=state.run.id,
            pro_id=state.pro_id,
            stage="critics",
            system=CRITIC_SYSTEM,
            # Scales with the batch: a verdict set truncated mid-JSON is
            # unparseable, and three re-asks later the whole job fails.
            max_tokens=_batch_max_tokens(len(ideas)),
            parse=lambda text: {int(v["idea_index"]): v for v in extract_json(text)},
        )
        for index, _ in review:
            verdict = reviewed.get(index)
            if not isinstance(verdict, dict) or "block_kind" not in verdict:
                # Fail closed on a missing verdict, or one that parsed but is
                # missing the field.
                verdict = {"block_kind": "unreviewed", "reason": "no usable verdict returned"}
            verdicts[index] = verdict
    for index, idea in enumerate(ideas):
        verdict = verdicts[index]
        if (
            verdict is not None
            and verdict["block_kind"] not in SUPPRESSING_BLOCK_KINDS
            and _is_consent_ask(idea)
        ):
            # Deterministic backstop: the prompt-level ban and the critic are
            # both probabilistic, and the critic labels only the PRIMARY
            # problem — a consent-ask idea must never reach the panel.
            verdicts[index] = {
                "block_kind": "consent_ask",
                "reason": "deterministic gate: idea asks for messaging consent/opt-in",
            }
    resolved = [v for v in verdicts if v is not None]
    # Callers index verdicts by batch position; a hole would silently misalign
    # every candidate's critic verdict.
    assert len(resolved) == len(ideas)
    return resolved


async def _rank_batch(
    state: PipelineState,
    deps: PipelineDeps,
    *,
    key: str,
    org_context: str,
    candidates: list[tuple[str, Recommendation]],
    evidence: str,
) -> RankerDecision:
    """Strict-schema ranking over positional tokens. Tokens are positional, not
    DB ids, so a resumed round replays the recorded response against identical
    ids. Raises PipelineFailure when the model cannot produce a valid ranking."""
    tokens = [token for token, _ in candidates]
    decision: RankerDecision = await _valid_json_call(
        deps,
        base_key=f"{key}:rank",
        tier=_ranker_tier(deps.llm.pricing),
        prompt=ranker_prompt(
            org_context,
            json.dumps([{"candidate_id": t, **idea.model_dump()} for t, idea in candidates]),
            state.run.journey_window,
            evidence,
        ),
        run_id=state.run.id,
        pro_id=state.pro_id,
        stage="rank",
        system=RANKER_SYSTEM,
        # Ranking is a judgment we want stable across resumes, not a creative act.
        temperature=0.0,
        parse=lambda text: validate_ranking(
            RankerDecision.model_validate(extract_json(text)), tokens
        ),
    )
    return decision


@dataclass
class _ScreenOutcome:
    token: str
    index: int  # index into the round's idea batch
    score: CandidateScore | None = None
    reactions: list[float] | None = None
    failure: str | None = None


async def _screen_one(
    state: PipelineState,
    deps: PipelineDeps,
    *,
    key: str,
    panel: PanelSelection,
    cards: dict[str, dict[str, Any]],
    idea: Recommendation,
    token: str,
    index: int,
    cell: str,
    llm: MeteredLLM | None = None,
    cache_session: AsyncSession | None = None,
) -> _ScreenOutcome:
    """One finalist's frozen-panel screen. `llm`/`cache_session` stay None on the
    sequential path (the job's own stack); a concurrent screen passes its own."""
    outcome = _ScreenOutcome(token=token, index=index)
    try:
        outcome.reactions = await _react(
            state,
            deps,
            panel,
            cards,
            idea.pro_facing_concept,
            idea.channel,
            "screen",
            "fast",
            call_key=f"{key}:screen:{token}",
            llm=llm,
            cache_session=cache_session,
        )
    except PipelineFailure as error:
        outcome.failure = error.reason
    else:
        outcome.score = score_candidate(outcome.reactions, cell, deps.calibration)
    return outcome


async def _screen_finalists(
    state: PipelineState,
    deps: PipelineDeps,
    *,
    key: str,
    panel: PanelSelection,
    cards: dict[str, dict[str, Any]],
    ideas: list[Recommendation],
    finalists: list[tuple[str, int]],
    cell: str,
) -> list[_ScreenOutcome]:
    """Screen each finalist on the frozen 3-panel. A finalist whose evaluation
    fails scores nothing at all — never a fabricated number.

    Tied finalists (exactly two) screen concurrently when the worker wired
    `llm_stacks`. Both calls still queue behind the SAME fleet-wide advisory-lock
    cap, but each runs on its own connection and its own sessions — sharing
    either across tasks would corrupt the limiter and the paid-call ledger.

    TaskGroup, NOT gather: gather(return_exceptions=False) propagates the first
    exception while leaving the sibling running, so a LeaseLost would let a paid
    call continue against a job this worker no longer owns and orphan its
    connection. TaskGroup cancels the sibling and awaits it, so both stacks
    unwind through their context managers before we re-raise. The group's
    ExceptionGroup is unwrapped so BudgetExhausted/LeaseLost still propagate as
    themselves; a per-finalist PipelineFailure never reaches here at all — it
    degrades that one finalist to a None score inside _screen_one.
    """
    screen = partial(_screen_one, state, deps, key=key, panel=panel, cards=cards, cell=cell)
    if len(finalists) == 2 and deps.llm_stacks is not None:
        stacks = deps.llm_stacks

        async def screen_in_own_stack(token: str, index: int) -> _ScreenOutcome:
            async with stacks() as (llm, cache_session):
                return await screen(
                    idea=ideas[index],
                    token=token,
                    index=index,
                    llm=llm,
                    cache_session=cache_session,
                )

        tasks: list[asyncio.Task[_ScreenOutcome]] = []
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(screen_in_own_stack(t, i)) for t, i in finalists]
        except BaseExceptionGroup as failures:
            # `from failures` so a simultaneous second failure survives as
            # __cause__ instead of being dropped on the floor.
            raise failures.exceptions[0] from failures
        return [task.result() for task in tasks]
    return [
        await screen(idea=ideas[index], token=token, index=index) for token, index in finalists
    ]


async def _stage_evolve(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    """The compounding win-stay/lose-shift loop. Each round generates a batch of
    ideas in one call, critics the batch in one call, ranks the survivors, and
    screens the top candidate (top two when the ranker's top scores are tied
    within TIE_MARGIN); the frozen 3-panel screen decides win/lose mechanically.
    Every round persists one candidate row per generated idea + one ledger row
    atomically, so re-entry replays the ledger and the recorded calls make
    partially-paid rounds free to redo."""
    brief = state.brief
    if brief is None:
        return {"skipped": "no_brief"}
    gate = gate_pro(brief, list(state.run.channels), state.run.journey_window)
    if gate.blocked:
        # Spec stage 1: reject before any LLM or persona budget is spent.
        await _abstain_pro(state, deps, state.pro_id, f"infeasible: {gate.reason}")
        return {"skipped": "feasibility", "reason": gate.reason}
    channels = list(gate.allowed_channels)
    config = LoopConfig.from_mapping(state.run.loop_config or {})
    ledger = await deps.store.rounds_for(state.run.id, state.pro_id)
    lstate = replay(ledger, config)
    history = [
        {"round": r.round, "mechanism": r.mechanism, "score_pp": r.score_pp, "outcome": r.outcome}
        for r in ledger
    ]
    await _resolve_abandoned_calls(state, deps)
    cell = brief.calibration_cell() or ""
    session = deps.store.session
    patterns = await pattern_summaries(session, state.run.journey_window, channels)
    evidence = evidence_block(patterns)
    failed = set(await failed_mechanisms(session, state.pro_id))

    while (reason := stop_reason(lstate, config)) is None:
        await _guard(state, deps)
        mode = next_mode(lstate, config)
        rnd = lstate.round + 1
        key = f"{state.run.id}:{state.pro_id}:r{rnd}"

        best_json = None
        if lstate.best_candidate_id is not None:
            best = await session.get(CandidateRow, lstate.best_candidate_id)
            best_json = json.dumps(best.recommendation) if best is not None else None
        org_context = brief.model_dump_json()
        build_prompt = _prompt_builder(
            org_context=org_context,
            best_json=best_json,
            history_json=json.dumps(history),
            channels=channels,
            journey_window=state.run.journey_window,
            evidence=evidence,
        )
        count = config.candidate_count
        tried = list(lstate.tried_mechanisms)
        # Warm start: round 1 only, and only while round 1 is still uncommitted.
        # The ledger row commits atomically with the round, so once round 1 is
        # durable lstate.round is 1+ here forever after and its recorded
        # warm_start evidence is the authoritative account of that decision —
        # no resumed round can retrieve again and overwrite it. A re-run of an
        # UNcommitted round 1 replays the recorded generation call, so the ideas
        # are identical regardless of what a second retrieval decides; the
        # mechanism_in_batch flag below keeps the stored evidence honest either
        # way. Retrieval itself is pure SQL — no paid call is added.
        warm_mechanism, warm_evidence = None, None
        if lstate.round == 0:
            match, warm_evidence = await retrieve(
                session, brief, threshold=config.warm_start_threshold
            )
            if match is not None:
                warm_evidence = {
                    **warm_evidence,
                    "winner_id": match.winner_id,
                    "score": match.score,
                    "mechanism": match.mechanism,
                }
                if match.mechanism in failed:
                    # The pro already rejected this mechanism; a cross-pro win
                    # does not override this pro's own observed failure.
                    warm_evidence |= {"outcome": "cold", "skipped": "recently_failed"}
                else:
                    warm_mechanism = match.mechanism
        batch_count = count + (1 if warm_mechanism else 0)
        prompt = build_prompt(mode, count, tried, warm_mechanism)
        await _reserve_round_worst_case(state, deps, _round_worst_case(deps, prompt, batch_count))
        ideas = await _generate_batch(
            state,
            deps,
            key=key,
            count=batch_count,
            prompt=prompt,
            build_prompt=build_prompt,
            tried=tried,
            warm=warm_mechanism,
        )
        if warm_mechanism is not None and warm_evidence is not None:
            in_batch = any(idea.mechanism == warm_mechanism for idea in ideas)
            warm_evidence["mechanism_in_batch"] = in_batch
            if not in_batch:
                # The seeded mechanism did not land in the batch — attribute
                # nothing to the retrieved winner, whose mechanism never
                # actually competed this round. This also keeps the resume path
                # honest: a re-run of an uncommitted round 1 retrieves live
                # again and may surface a different winner than the frozen
                # generation used, so the stored winner_id must not claim credit
                # unless its mechanism is demonstrably in the (replayed) batch.
                warm_evidence |= {"outcome": "cold", "skipped": "mechanism_absent_from_batch"}
        verdicts = await _verdicts_for_batch(
            state,
            deps,
            key=key,
            org_context=org_context,
            ideas=ideas,
            channels=channels,
            failed=failed,
        )
        candidate_ids = [uuid4().hex for _ in ideas]
        rankable = [
            index
            for index, verdict in enumerate(verdicts)
            if verdict["block_kind"] not in SUPPRESSING_BLOCK_KINDS
        ]
        tokens = {f"c{position + 1}": index for position, index in enumerate(rankable)}

        decision: RankerDecision | None = None
        ranking_failure: str | None = None
        ranker_model = "skipped"
        if len(rankable) >= 2:
            # Recorded before the call: a failed ranking still cost up to
            # JSON_CALL_ATTEMPTS paid calls, so the audit trail must not read
            # the same as a ranker that was never invoked.
            ranker_model = deps.llm.pricing.model_for(_ranker_tier(deps.llm.pricing))
            try:
                decision = await _rank_batch(
                    state,
                    deps,
                    key=key,
                    org_context=org_context,
                    candidates=[(token, ideas[index]) for token, index in tokens.items()],
                    evidence=evidence,
                )
            except PipelineFailure as error:
                # NEVER fall back to an arbitrary unranked candidate: the round
                # is unavailable and the replayed champion survives untouched.
                ranking_failure = error.reason

        finalists: list[tuple[str, int]] = []
        if not rankable:
            selection_reason = "all_candidates_suppressed"
        elif ranking_failure is not None:
            selection_reason = "ranking_failed_champion_preserved"
        elif decision is None:
            selection_reason = "single_rankable_candidate"
            finalists = [("c1", rankable[0])]
        else:
            first, second = decision.by_rank()[:2]
            finalists = [(first.candidate_id, tokens[first.candidate_id])]
            if first.score - second.score <= config.tie_margin:
                # Indistinguishable on the ranker's evidence: the persona screen
                # is the tiebreaker, so both finalists get screened.
                finalists.append((second.candidate_id, tokens[second.candidate_id]))
                selection_reason = "tie_within_margin_top_two_screened"
            else:
                selection_reason = "clear_winner"

        screens: list[_ScreenOutcome] = []
        panel: PanelSelection | None = None
        if finalists:
            try:
                panel, cards = await _panel_for(state, deps, brief, 3)
            except InsufficientPanelFit as error:
                await _abstain_pro(state, deps, state.pro_id, f"low panel fit: {error}")
                return {"rounds": lstate.round, "stop": "panel_unavailable"}
            screens = await _screen_finalists(
                state,
                deps,
                key=key,
                panel=panel,
                cards=cards,
                ideas=ideas,
                finalists=finalists,
                cell=cell,
            )

        scored = [(s, s.score.reduction_pp) for s in screens if s.score is not None]
        score_pp: float | None = None
        if not rankable:
            outcome, challenger = "suppressed", 0  # a loss, no persona spend
        elif not scored:
            # Ranking or every finalist's evaluation was unavailable this round —
            # an honest loss with no score at all, never a fabricated one. The
            # ledger row still references a candidate for its mechanism.
            outcome, challenger = "unavailable", (finalists[0][1] if finalists else rankable[0])
        else:
            # The screen breaks the ranker's tie. An abstained screen (scored but
            # with no usable reduction) must never out-rank a real one, hence -inf.
            best_screen, score_pp = max(
                scored, key=lambda pair: pair[1] if pair[1] is not None else float("-inf")
            )
            challenger = best_screen.index
            outcome = "win" if is_win(lstate, score_pp, config, MIN_REDUCTION_FLOOR_PP) else "lose"
            if len(finalists) == 2 and best_screen.token == finalists[1][0]:
                selection_reason = "tie_broken_by_screen_runner_up"

        # One CandidateRow per generated idea, all committed atomically with the
        # round's single ledger row below.
        screened = {s.index: s for s in screens}
        for index, idea in enumerate(ideas):
            status = "discarded"
            if verdicts[index]["block_kind"] in SUPPRESSING_BLOCK_KINDS:
                status = "suppressed"
            elif outcome == "win" and index == challenger:
                status = "champion"
            candidate = CandidateRow(
                id=candidate_ids[index],
                run_id=state.run.id,
                pro_id=state.pro_id,
                recommendation=idea.model_dump(),
                status=status,
                round=rnd,
                critics={
                    "block_kind": verdicts[index]["block_kind"],
                    "reason": verdicts[index].get("reason", ""),
                },
            )
            screen = screened.get(index)
            if screen is not None and screen.score is not None and screen.reactions is not None:
                assert panel is not None
                candidate.score = {"screen": screen.score.model_dump()}
                candidate.persona_evidence = {
                    "screen": {"panel": panel.model_dump(), "reactions": screen.reactions}
                }
            session.add(candidate)

        if outcome == "win" and lstate.best_candidate_id is not None:
            dethroned = await session.get(CandidateRow, lstate.best_candidate_id)
            if dethroned is not None:
                dethroned.status = "discarded"
        mechanism = ideas[challenger].mechanism
        lstate = apply_round(
            lstate,
            mechanism=mechanism,
            candidate_id=candidate_ids[challenger],
            score_pp=score_pp,
            outcome=outcome,
            config=config,
            # The batch's other mechanisms were generated, critiqued and ranked
            # this round; forbid them next round instead of re-buying them.
            also_tried=[i.mechanism for index, i in enumerate(ideas) if index != challenger],
        )
        session.add(
            EvolveRoundRow(
                run_id=state.run.id,
                pro_id=state.pro_id,
                round=rnd,
                mechanism=mechanism,
                candidate_id=candidate_ids[challenger],
                outcome=outcome,
                score_pp=score_pp,
                ranking=_ranking_evidence(
                    ideas=ideas,
                    candidate_ids=candidate_ids,
                    tokens=tokens,
                    decision=decision,
                    tie_margin=config.tie_margin,
                    finalists=finalists,
                    selection_reason=selection_reason,
                    ranker_model=ranker_model,
                    screens=screens,
                    ranking_failure=ranking_failure,
                    warm_start=warm_evidence,
                ),
            )
        )
        await session.commit()  # candidates + ledger row land atomically
        history.append(
            {"round": rnd, "mechanism": mechanism, "score_pp": score_pp, "outcome": outcome}
        )

    return {"rounds": lstate.round, "stop": reason, "best_score": lstate.best_score}


def _ranking_evidence(
    *,
    ideas: list[Recommendation],
    candidate_ids: list[str],
    tokens: dict[str, int],
    decision: RankerDecision | None,
    tie_margin: float,
    finalists: list[tuple[str, int]],
    selection_reason: str,
    ranker_model: str,
    screens: list[_ScreenOutcome],
    ranking_failure: str | None,
    warm_start: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The round decision's audit trail: who was ranked, in what order, why the
    challenger was chosen, and what failed on the way."""
    ranks = {r.candidate_id: r for r in decision.ranking} if decision is not None else {}
    evidence: dict[str, Any] = {
        "order": [
            {
                "token": token,
                "candidate_id": candidate_ids[index],
                "mechanism": ideas[index].mechanism,
                "rank": ranks[token].rank if token in ranks else None,
                "score": ranks[token].score if token in ranks else None,
            }
            for token, index in tokens.items()
        ],
        "tie": decision.tie if decision is not None else False,
        "tie_reason": decision.tie_reason if decision is not None else "",
        "tie_margin": tie_margin,
        "finalists": [token for token, _ in finalists],
        "selection_reason": selection_reason,
        "ranker_model": ranker_model,
        "candidate_ids": {token: candidate_ids[index] for token, index in tokens.items()},
    }
    if decision is not None:
        evidence["order"].sort(key=lambda item: item["rank"])
    failures = {s.token: s.failure for s in screens if s.failure is not None}
    if failures:
        evidence["screen_failures"] = failures
    if ranking_failure is not None:
        evidence["ranking_failure"] = ranking_failure
    if warm_start is not None:
        evidence["warm_start"] = warm_start
    return evidence


async def _final_reactions(
    state: PipelineState,
    deps: PipelineDeps,
    panel: PanelSelection,
    cards: dict[str, dict[str, Any]],
    champion: CandidateRow,
) -> tuple[list[float], str, str | None]:
    """Held-out reactions: deep tier first, downgrading to the fast tier on any
    deep failure (provider error or garbled output) instead of losing the Pro.
    Returns (reactions, tier_used, deep_failure). Budget/ownership exceptions
    re-raise untouched; a both-tiers failure raises PipelineFailure carrying
    both causes."""

    async def react(tier: str, key_suffix: str) -> list[float]:
        return await _react(
            state,
            deps,
            panel,
            cards,
            champion.recommendation["pro_facing_concept"],
            champion.recommendation.get("channel", "none"),
            "final",
            tier,
            call_key=f"{state.run.id}:{state.pro_id}:{key_suffix}",
        )

    try:
        return await react("deep", "final"), "deep", None
    except (BudgetExhausted, LeaseLost):
        raise  # budget/ownership semantics, never a model failure
    except Exception as error:  # noqa: BLE001 — any deep failure downgrades
        deep_failure = (
            error.reason
            if isinstance(error, PipelineFailure)
            else f"{type(error).__name__}: {error}"
        )[:500]
        # The deep failure may have outlived the lease or a kill-switch flip:
        # re-guard before paying for the fallback (every paid call is guarded).
        await _guard(state, deps)
        try:
            return await react("fast", "final_fast"), "fast", deep_failure
        except PipelineFailure as fast_error:
            raise PipelineFailure(
                f"deep: {deep_failure}; fast: {fast_error.reason}"
            ) from fast_error


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
    await _resolve_abandoned_calls(state, deps)  # a crashed final call may have paid
    try:
        panel, cards = await _panel_for(state, deps, brief, 5)
    except InsufficientPanelFit as error:
        await _abstain_pro(state, deps, state.pro_id, f"low panel fit: {error}")
        return {}
    await _guard(state, deps)
    cell = brief.calibration_cell() or ""
    try:
        reactions, tier_used, deep_failure = await _final_reactions(
            state, deps, panel, cards, champion
        )
    except PipelineFailure as error:
        # Keep the REAL cause on the stored score: a bare "no_reactions" reads
        # as "the panel said no" downstream, when the evaluation just failed.
        score = score_candidate([], cell, deps.calibration).model_copy(
            update={"abstain_reason": error.reason}
        )
    else:
        score = score_candidate(reactions, cell, deps.calibration)
        champion.persona_evidence = {
            **champion.persona_evidence,
            "final": {
                "panel": panel.model_dump(),
                "reactions": reactions,
                "tier": tier_used,
                **({"deep_failure": deep_failure} if deep_failure else {}),
            },
        }
    champion.score = {**champion.score, "final": score.model_dump()}
    await deps.store.session.commit()
    return {}


def _degraded_panel_notes(persona_evidence: dict[str, Any] | None) -> dict[str, str]:
    """Reviewer-facing notes per stage whose panel ran short-handed. Named
    honestly: a missing counterweight and a final check that re-used the
    screen's exact personas both void invariants the full panel guarantees."""
    notes: dict[str, str] = {}
    members: dict[str, set[str]] = {}
    for stage, stage_evidence in (persona_evidence or {}).items():
        panel = stage_evidence.get("panel", {})
        if not panel.get("degraded"):
            continue
        items = panel.get("items", [])
        qualified = [item for item in items if item.get("role") != "backfill"]
        note = f"only {len(qualified)} of {panel.get('requested_size')} personas qualified"
        if len(items) > len(qualified):
            note += f", {len(items) - len(qualified)} below-threshold backfill seat(s)"
        if not any(item.get("role") == "counterweight" for item in items):
            note += ", no counterweight on the panel"
        notes[stage] = note
        members[stage] = {item.get("persona_id") for item in items}
    if {"screen", "final"} <= members.keys() and members["screen"] == members["final"]:
        notes["final"] += "; final check reused the screen panel (not held out)"
    return notes


async def _stage_score(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    if await deps.store.winner_for(state.run.id, state.pro_id) is not None:
        return {}
    config = LoopConfig.from_mapping(state.run.loop_config or {})
    champion = await _champion_for(state, deps, config)
    final = champion.score.get("final") if champion is not None else None
    if champion is None or final is None:
        # Two different endings, recorded distinctly: no round ever won the
        # screen (an honest "not worth touching") vs a champion that never got
        # its held-out final check (an incomplete run, not a conclusion).
        deps.store.session.add(
            WinnerRow(
                run_id=state.run.id,
                pro_id=state.pro_id,
                kind="no_action",
                rationale=(
                    "no_round_cleared_screen" if champion is None else "champion_final_missing"
                ),
            )
        )
        await deps.store.session.commit()
        return {}
    outcome = select_winner({champion.id: CandidateScore.model_validate(final)})
    if isinstance(outcome, Winner):
        # A stage that ran on a short-handed panel is flagged on the winner —
        # the output still ships, with the disclaimer, instead of abstaining.
        degraded_panels = _degraded_panel_notes(champion.persona_evidence)
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
                    **({"panel_disclaimer": degraded_panels} if degraded_panels else {}),
                },
                # Sanitized bands only, and never eligible here: eligibility is
                # earned in outcomes.ingest from an observed 7d return.
                fingerprint=build_fingerprint(state.brief) if state.brief else {},
                fingerprint_version=FINGERPRINT_VERSION if state.brief else None,
            )
        )
    else:
        assert isinstance(outcome, NoAction)
        # "all_candidates_abstained" alone reads as a panel judgment when the
        # evaluation may simply have failed — surface the recorded cause.
        abstain_reason = final.get("abstain_reason")
        deps.store.session.add(
            WinnerRow(
                run_id=state.run.id,
                pro_id=state.pro_id,
                kind="no_action",
                rationale=(
                    f"{outcome.reason}: {abstain_reason}" if abstain_reason else outcome.reason
                ),
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


async def _attach_follow_up(
    state: PipelineState, deps: PipelineDeps, winner: WinnerRow, candidate: CandidateRow
) -> None:
    """War game must offer only channels this Pro still consents to. The
    evolve stage re-gates per round, but state.run.channels is the raw run
    list, so re-gate here rather than trust it verbatim (a stale/expanded
    consent state must not leak an un-consented channel into the follow-up)."""
    skip_reason: str | None = None
    gated_channels: list[str] = []
    if state.brief is None:
        skip_reason = "channels not gateable on resume: no org brief"
    else:
        gate = gate_pro(state.brief, list(state.run.channels), state.run.journey_window)
        if gate.blocked:
            skip_reason = f"channels not gateable: {gate.reason}"
        else:
            gated_channels = list(gate.allowed_channels)
    if skip_reason is not None:
        winner.evidence = {**winner.evidence, "follow_up_unavailable": skip_reason}
        return
    try:
        plan_json = await _valid_json_call(
            deps,
            base_key=f"{state.run.id}:{state.pro_id}:wargame",
            tier="fast",
            prompt=war_game_prompt(
                state.brief.model_dump_json() if state.brief else "{}",
                json.dumps(candidate.recommendation),
                gated_channels,
            ),
            run_id=state.run.id,
            pro_id=state.pro_id,
            stage="wargame",
            system=WAR_GAME_SYSTEM,
            parse=lambda text: FollowUpPlan.model_validate(extract_json(text)),
        )
    except PipelineFailure as error:
        # Additive, never blocking: a winner without a war game still ships.
        winner.evidence = {**winner.evidence, "follow_up_unavailable": error.reason}
    else:
        follow_up = plan_json.model_dump()
        # Never trust the model on the stop rule.
        follow_up["on_negative"] = {"action": "stop", "channel": "none"}
        winner.evidence = {**winner.evidence, "follow_up": follow_up}


async def _stage_measure(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    winner = await deps.store.winner_for(state.run.id, state.pro_id)
    if winner is None or winner.kind != "winner":
        return {}
    if await deps.store.measurement_for(winner.id) is not None:
        return {}  # resume
    await _resolve_abandoned_calls(state, deps)  # a crashed measure call may have paid
    candidate = await deps.store.session.get(CandidateRow, winner.candidate_id)
    assert candidate is not None
    await _guard(state, deps)
    if "follow_up" not in winner.evidence and "follow_up_unavailable" not in winner.evidence:
        await _attach_follow_up(state, deps, winner, candidate)
        await deps.store.session.commit()
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
    if run is None or run.status in TERMINAL_RUN_STATUSES:
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


# Non-terminal, non-degraded runs whose jobs are ALL terminal: the crash window
# between the last job's terminal commit and its finalize_run call.
_STALLED_RUNS_SQL = text("""
SELECT r.id FROM runs r
WHERE r.status NOT IN ('complete', 'no_action', 'abstained', 'stopped', 'failed', 'degraded')
  AND EXISTS (SELECT 1 FROM jobs j WHERE j.run_id = r.id)
  AND NOT EXISTS (
    SELECT 1 FROM jobs j
    WHERE j.run_id = r.id AND j.status NOT IN ('done', 'failed', 'stopped')
  )
""")


async def finalize_stalled_runs(session: AsyncSession) -> int:
    """Self-heal runs stranded by a crash after their last job went terminal
    but before finalize_run committed. Called from the worker idle beat."""
    run_ids = [row.id for row in (await session.execute(_STALLED_RUNS_SQL)).all()]
    for run_id in run_ids:
        await finalize_run(session, run_id)
    return len(run_ids)


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
    if run.status in TERMINAL_RUN_STATUSES:
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
            # Attempts exhausted for THIS Pro only: its job fails (requeue_job
            # already marked it) and finalize_run aggregates to failed/degraded
            # once every sibling is terminal — one id's dead context flow must
            # never take the whole run down.
            await queue.checkpoint_job(
                store.session, job_id, "failure",
                {"reason": f"context_unavailable: {error}"},
            )
            await finalize_run(store.session, run_id)
        return
    state.brief = next(
        (brief for brief in batch.organizations if brief.pro_id == state.pro_id),
        None,
    )
    # Stamp the flow's self-reported query version exactly once, replacing only
    # the creation-time placeholder. Stamp-once keeps lineage stable when the
    # flow redeploys mid-run: later jobs (or lease-reclaim re-entries) never
    # rewrite a version pros were already scored under.
    reported = batch.audience_query_version
    if reported and run.audience_query == PENDING_AUDIENCE_QUERY:
        run.audience_query = reported
        await store.session.commit()

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
        except Exception as error:  # noqa: BLE001 — the honest-failure backstop
            # Unhandled crash: record the cause and burn the attempt NOW. The
            # alternative is an anonymous 10-minute lease-expiry loop ending in
            # a reaped job with no recorded reason (the persona-429 / deep-400
            # incident). Recorded calls make the retry free to resume.
            await store.session.rollback()
            await queue.checkpoint_job(
                store.session, job_id, "failure", {"reason": f"unhandled at {stage}: {error!r}"}
            )
            if await store.requeue_job(job_id):
                return  # attempts remain: a fresh claim resumes from checkpoints
            await finalize_run(store.session, run_id)
            return
        await store.complete_stage(job_id, stage, payload)
