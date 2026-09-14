# Idea Ranking + Cross-Pro Warm Starts — Implementation Plan

Source spec: `docs/superpowers/specs/2026-08-14-journey-window-touch-optimization-design.md`
(GSTACK REVIEW REPORT section: CEO + ENG cleared ideas 1 and 2).

## Global Constraints (binding on every task)

- Waypoint is **recommendation-only**. Never write to, edit, schedule, or control
  Iterable. Do not change LCM sending behavior (`handoff.py` request/response
  contract stays byte-identical).
- Do NOT create a new service, RAG database, vector database, or parallel loop.
- Reuse existing infrastructure: `MeteredLLM`/`RecordedCalls` durable paid-call
  path, `EvolveRoundRow` replay ledger, `queue.reserve_cost`/`reconcile_cost`
  cost accounting, `PersonaEvalRow` reaction cache, fleet kill switch, `n8n.py`
  allowlisted context, `outcomes.py` ingestion, `handoff.py`.
- Every paid model call goes through `deps.llm.complete` (MeteredLLM) with a
  deterministic `call_key` — no direct gateway calls.
- Deterministic replay must be preserved: re-running a job after a crash must
  replay the `evolve_rounds` ledger and reuse recorded LLM calls without
  duplicating rounds, candidates, or spend.
- Do not change the primary return-to-app objective or `measurement.py`.
- Work happens on branch `V2-Improvements`. Implementers commit per task; the
  controller squashes each system into its single final commit at the end
  (final history: exactly two commits, one per system — never mixed).
- Backend: Python 3.14, `services/api`. Run tests with
  `cd services/api && uv run pytest -q`. Lint/type: `uv run ruff check src tests`
  and `uv run mypy src` must stay clean.
- Frontend: `apps/web`, Next.js + vitest (`cd apps/web && npx vitest run`).
- Style: prefer small functions (<50 lines); comments only for constraints the
  code can't show; match existing idiom (the codebase is heavily commented with
  load-bearing rationale — keep that register).
- Test DB: local Postgres `waypoint_test` (conftest drops/re-migrates via real
  Alembic migrations; new columns need real migrations or every test fails).

---

## Task 1 — Ranking groundwork: config, settings, ranker contract, prompts, ledger column, UI fields

**Context:** Commit 1 replaces the evolve loop's one-idea-per-round generation
with a configurable batched generation + strict LLM ranking. This task lands
everything EXCEPT the pipeline rewrite: typed config, the strict ranker output
contract, the batched/ranker prompts, the `evolve_rounds.ranking` evidence
column + migration, and the run-configuration UI fields. `pipeline.py` must NOT
be touched in this task (Task 2 owns it) — `evolve_prompt` keeps `count`
defaulting to 1 so the existing pipeline continues to work unchanged.

**IMPORTANT — partial groundwork already exists uncommitted in the working
tree** for: `services/api/src/waypoint/loop.py`, `settings.py`, `models.py`,
`prompts.py`, `tables.py`, and the new
`services/api/alembic/versions/0005_evolve_round_ranking.py`. Start by reading
`git diff` and the new migration file. Review that work critically, keep or
adjust it as your own, complete what's missing, and add all tests. Do not
assume it is correct — you own its quality.

**Requirements:**

1. `loop.py` — `LoopConfig` gains two editable keys (same path as existing loop
   controls: fleet `loop_defaults` merged with per-run `loop_config`):
   - `CANDIDATE_COUNT` (int, default **3**): ideas generated per evolve round.
     Validation: must be a positive integer; safety ceiling
     `MAX_CANDIDATE_COUNT = 10` (a module constant) — values above it are a
     `ValueError`, as are 0, negatives, and non-numeric input.
   - `TIE_MARGIN` (float, default **0.05**): ranker-score gap (0–1 scale) at or
     under which the top two candidates are both persona-screened. Validation:
     `0 <= TIE_MARGIN <= 1`.
   - Both keys appear in `to_dict()` and are accepted by `from_mapping`.
2. `settings.py` — add `MODEL_RANKER: str = ""` (empty string means "use
   MODEL_FAST"; the worker wires this in Task 3). Update
   `tests/test_settings.py`'s expected-name set.
3. `models.py` — strict ranker output contract:
   - `RankedCandidate`: `candidate_id` (non-empty str), `rank` (int >= 1),
     `score` (float in [0, 1]; NaN must fail validation).
   - `RankerDecision`: `ranking` (non-empty list), `tie: bool` (REQUIRED — an
     explicit tie decision, no default), `tie_reason: str = ""`, plus a
     `by_rank()` helper returning the list sorted by rank.
   - `validate_ranking(decision, expected_ids)`: raises `ValueError` unless
     every expected id appears exactly once (no unknown, missing, or duplicate
     ids) and ranks are exactly the unique contiguous set 1..N.
4. `prompts.py`:
   - `evolve_prompt` gains `count: int = 1`. For any count it must instruct:
     exactly `count` ideas, each idea's mechanism distinct within the batch,
     returned as a JSON array of exactly `count` objects. Mode semantics: in
     `stay` mode the FIRST idea refines the champion mechanism and (when
     count > 1) the rest must use different grounded mechanisms; in `shift`
     mode all ideas must avoid the forbidden (tried) mechanisms. All existing
     content (two-layer split, grounding hard rule, seeds-not-copy, channel
     directive, journey window, evidence block, fenced untrusted context) must
     survive for every count.
   - New `RANKER_SYSTEM` and `ranker_prompt(org_context, candidates_json,
     journey_window, evidence)`: rank candidates by expected return-to-app
     value (opens/clicks are diagnostics, not the goal), weighing historical
     outcome evidence, journey-window relevance, feasibility, downside risk,
     and uncertainty. Output contract in the prompt must state: one JSON object
     `{"ranking": [{"candidate_id", "rank", "score"}...], "tie": bool,
     "tie_reason": str}`; every candidate_id exactly once spelled exactly as
     given; unique contiguous ranks 1..N (1 best); scores on a 0–1 scale; `tie`
     is an EXPLICIT decision. Candidates and org context are both fenced as
     untrusted.
5. `tables.py` + migration `0005` — `EvolveRoundRow` gains
   `ranking: Mapped[dict[str, Any]] = mapped_column(default=dict)` (JSONB,
   server default `'{}'::jsonb`, non-null). Migration must upgrade AND
   downgrade cleanly.
6. `apps/web` — the run-start loop controls enumerate fields in
   `RunStart.tsx` `LOOP_FIELDS` (and `RunStatus.tsx` labels): add
   `CANDIDATE_COUNT` ("Ideas per round", min 1) and `TIE_MARGIN`
   ("Ranker tie margin (0-1)", min 0). Follow the existing confirm-typing UX
   unchanged. Update the fleet-settings fixtures in `RunStart.test.tsx` /
   `RunStatus.test.tsx` to carry the new defaults and add a minimal assertion
   that the new fields render.

**Tests (backend in `services/api/tests`):**

- `test_loop.py`: CANDIDATE_COUNT default 3; explicit values other than 3
  accepted (1, 2, 5, 10); ceiling (11 → ValueError); invalid values (0, -1,
  "abc") → ValueError; TIE_MARGIN default/bounds (-0.1 and 1.5 → ValueError);
  `to_dict` round-trips both keys; existing tests updated for the new keys.
- New ranker-contract tests (e.g. `test_ranking.py`): `validate_ranking`
  rejects unknown ids, missing candidates, duplicate ids, duplicate ranks,
  gapped ranks; accepts a valid permutation; `RankerDecision` rejects a missing
  `tie` field; `RankedCandidate` rejects score < 0, > 1, and NaN.
- `test_prompts.py`: batch evolve prompt (count=3) demands exactly 3 ideas as
  a JSON array with distinct mechanisms while keeping fences + grounding +
  two-layer assertions; count=1 keeps working; ranker prompt contract test
  (fences, every-id-exactly-once rule, 0-1 score scale, explicit tie).
  Fix any existing assertions broken by the new wording.
- `test_settings.py`: updated expected-name set; MODEL_RANKER defaults to "".

**Verify:** `cd services/api && uv run pytest -q` all green;
`uv run ruff check src tests`; `uv run mypy src`;
`cd apps/web && npx vitest run` green. The pipeline still runs one idea per
round after this task (no behavior change).

---

## Task 2 — Pipeline: batched generation, batch critic, strict ranker, tie screening, round cost preflight

**Context:** The heart of commit 1. `_stage_evolve` in
`services/api/src/waypoint/pipeline.py` currently generates ONE idea per round
(generate → critic → persona screen → win/lose ledger row). Rework each round
to: reserve the round's worst-case cost, generate exactly CANDIDATE_COUNT ideas
in ONE durable batched call, dedupe mechanisms + bounded refill, run the
existing grounding critic over the batch in ONE call, rank the survivors with a
strict-schema LLM ranker, persona-screen the top candidate (top two when tied
within TIE_MARGIN), and write ONE authoritative `EvolveRoundRow` for the round
decision plus one `CandidateRow` per generated idea. Deterministic replay and
all existing failure semantics must survive.

**Design (follow this; deviate only with a stated reason):**

- `_valid_json_call` gains `temperature: float | None = None` and
  `max_tokens: int = 1200` params. Temperature applies to attempt 0 only;
  retries fall back to the default (non-zero) temperature so a deterministic
  bad output can vary on re-ask.
- `_react` gains `llm: MeteredLLM | None = None` and
  `cache_session: AsyncSession | None = None` (defaulting to `deps.llm` /
  `deps.store.session`) so Task 3 can run tied screens concurrently on
  independent stacks. Behavior unchanged when omitted.
- Constants: `MAX_BATCH_REFILLS = 2`; suppressing verdict kinds stay
  `("ungrounded", "unreviewed", "per_pro_data", "infeasible_channel",
  "recently_failed")`; `_batch_max_tokens(count) = min(1200 * count, 6000)`.
- Ranker tier: helper `_ranker_tier(pricing)` returns `"rank"` when that tier
  exists in `pricing.models`, else `"fast"` (Task 3 wires the real model).
  Ranker calls use `temperature=0.0` (deterministic ranking wherever
  supported; the gateway already drops the param for models that reject it),
  stage `"rank"`, call key `f"{key}:rank"`.
- **Round cost preflight** (spec: reserve worst-case round cost before paid
  work, then reconcile actual): before ANY paid call of the round, compute the
  round's worst case = generation worst case × (1 + MAX_BATCH_REFILLS) +
  critic + ranker + 2 × screen (use `worst_case_cost` with the generation
  prompt as the size proxy and each stage's system prompt/max_tokens). Reserve
  it via `deps.llm.reserve`; refusal raises `BudgetExhausted` (run_job already
  labels it honestly). On success immediately release the hold
  (`deps.llm.reconcile(run_id, worst, Decimal(0))` + commit the records
  session) — the per-call MeteredLLM path then re-reserves each call's own
  worst case and reconciles it to actual spend. Comment WHY (preflight
  ensures a round either fully fits the budget or stops before partial spend).
- **Batched generation:** one `_valid_json_call` under key
  `f"{key}:generate"`, stage `"evolve"`, prompt `evolve_prompt(...,
  count=config.candidate_count)`, `max_tokens=_batch_max_tokens(count)`.
  Parse leniently: `extract_json`; a bare object counts as a 1-element array;
  per-item `Recommendation.model_validate`, dropping malformed items; zero
  valid items → ValueError (so `_valid_json_call` re-asks under a fresh key).
  Then dedupe by mechanism (keep first) and truncate to `candidate_count`.
  **Bounded refill:** while short and refills < MAX_BATCH_REFILLS, issue
  `f"{key}:refill{n}"` calls with a shift-mode prompt whose forbidden list is
  tried ∪ already-held mechanisms, asking for exactly the missing count;
  merge + dedupe. A refill that fails `_valid_json_call` entirely is tolerated
  (proceed with ≥1 idea already held) — bounded means bounded.
- **Batch critic:** pre-gates first, per idea, without spend: mechanism in
  `failed_mechanisms` → `recently_failed`; channel not in gated channels →
  `infeasible_channel`. All remaining ideas go in ONE critic call under
  `f"{key}:critic"` (existing `critic_prompt`; `idea_index` = index into the
  batch). Missing/malformed verdicts fail closed as `unreviewed` (existing
  incident rule).
- **Ranking:** candidates that survived the critic get positional tokens
  `"c1".."cN"` (position-stable so a resumed round replays the recorded ranker
  response against identical ids — do NOT use DB ids in the ranker contract).
  Exactly one rankable candidate → skip the ranker call entirely
  (`selection_reason="single_rankable_candidate"`). Two or more → ranker call;
  parse = `validate_ranking(RankerDecision.model_validate(extract_json(text)),
  expected_tokens)`. **Ranking failure** (PipelineFailure after the bounded
  re-asks): the round is recorded with outcome `"unavailable"`, no candidate
  is selected (champion preserved via replay; ledger row's candidate is the
  first rankable candidate purely as the row's mechanism/candidate reference,
  status stays `discarded`), failure reason recorded in ranking evidence —
  NEVER select an arbitrary unranked candidate. `BudgetExhausted`/`LeaseLost`
  propagate as today.
- **Finalists:** rank-1 always; rank-2 additionally iff
  `rank1.score - rank2.score <= config.tie_margin`. Persona-screen each
  finalist via `_react` with call key `f"{key}:screen:{token}"` (sequentially
  in this task; Task 3 adds concurrency). A finalist whose screen fails
  (PipelineFailure) scores None. All finalists failing → round outcome
  `"unavailable"`. Otherwise the challenger is the best-scoring finalist
  (screen breaks the tie), `outcome = win/lose` via existing `is_win`.
- **Persistence (atomic per round):** one `CandidateRow` per generated idea,
  ids assigned client-side (`uuid4().hex`) so ranking evidence can reference
  them; statuses: suppressed (critic/pre-gate blocked), champion (winning
  challenger), discarded (everything else). Screened candidates carry
  `score={"screen": ...}` and `persona_evidence={"screen": {panel,
  reactions}}` exactly like today. The round's `EvolveRoundRow` carries
  `mechanism`/`candidate_id`/`outcome`/`score_pp` of the challenger plus the
  new `ranking` dict: rank order (token, mechanism, rank, score), `tie` +
  `tie_reason` from the ranker, `tie_margin` from config, `finalists`,
  `selection_reason` ("clear_winner", "tie_within_margin_top_two_screened",
  "tie_broken_by_screen_runner_up", "single_rankable_candidate",
  "all_candidates_suppressed", "ranking_failed_champion_preserved"),
  ranker model or "skipped", token→CandidateRow-id map, and any
  `screen_failures`. Candidates + ledger row commit in ONE transaction after
  screening (never before — `_react` commits the persona cache mid-round).
  Win still dethrones the prior champion row's status.
- All-suppressed batch: round outcome `"suppressed"`, score None, mechanism =
  first idea's mechanism (matches today's semantics of a suppressed round).
- Keep `_stage_evolve` readable by decomposing into helpers (e.g.
  `_generate_batch`, `_verdicts_for_batch`, `_rank_batch`,
  `_screen_finalists`, `_reserve_round_worst_case`) each under ~50 lines.
- Update the stale comment claiming "exactly one CandidateRow per round".

**Test fakes (`tests/conftest.py`):** FakeLLM defaults must exercise the real
default path: `evolve` returns a JSON array of 3 ideas with distinct
mechanisms, `critics` returns verdicts for idea_index 0..2, and a new `rank`
stage returns a valid 3-candidate ranking (c1 rank 1 score 0.9, c2 rank 2
score 0.5, c3 rank 3 score 0.2, tie false). Add helpers (e.g.
`batch_json(mechanisms)`, `rank_json(...)`). Add `"rank": "model-fast"` to
FAKE_PRICING models. Legacy loop-behavior tests that script one idea per round
should set `CANDIDATE_COUNT: 1` in the run's `loop_config` (ranker is skipped
at count 1, so their call counts stay meaningful) — update them accordingly,
keeping their original intent.

**Tests (new, e.g. `test_ranking.py` + updates in `test_pipeline.py`):**

- happy path at default count 3: one evolve call, one critic call, one rank
  call, one screen call (clear winner) in round 1; winner completes.
- candidate counts other than 3: count 1 (no ranker call), count 2 and 5
  (generation asks for that many; all flow through).
- duplicate mechanisms in the batch are deduped and trigger a bounded refill;
  malformed items are dropped and refilled; refill stops at MAX_BATCH_REFILLS
  and the round proceeds with what it has; a batch that never yields a valid
  idea fails the job honestly.
- batch critic: one critic call carries all non-pre-gated ideas; a blocked
  idea is suppressed without persona spend; missing verdicts fail closed.
- strict ranker validation: unknown token / duplicate rank / missing candidate
  responses are re-asked and, when exhausted, the round records
  `ranking_failed_champion_preserved` with outcome unavailable and the prior
  champion survives; a later good round still wins.
- clear winner: only rank-1 is screened. Tied finalists (scores within
  TIE_MARGIN): both screened, better screen score wins the round and
  `tie_broken_by_screen_runner_up` is recorded when rank-2 wins.
- ranker determinism: the rank-stage call is issued with temperature 0.0.
- cost preflight: a run whose remaining budget is below the round worst case
  stops as `budget_exhausted` with zero gateway calls that round; reservation
  is released after a successful preflight (cost_reserved returns to the
  per-call pattern — assert no permanent hold accumulates across rounds).
- kill switch: engaged mid-loop still stops the run with `fleet_killed` and no
  further paid calls.
- replay after partial failure: crash after round N commits → rerun resumes
  from the ledger, generation/critic/rank calls for committed rounds replay
  from recorded calls with zero new spend, and no duplicate rounds/candidates
  appear.
- ranking evidence: EvolveRoundRow.ranking carries order, scores, tie margin,
  finalists, selection reason, and the token→candidate-id map.

**Verify:** full backend suite green (`uv run pytest -q`), ruff + mypy clean.

---

## Task 3 — Concurrent tied-finalist screening + worker wiring for the ranker model

**Context:** Task 2 screens tied finalists sequentially. The spec requires tied
finalists to be evaluated concurrently through the EXISTING in-flight limiter
(the Postgres advisory-lock `FleetSlots` inside MeteredLLM). Concurrent paid
calls must not share an AsyncSession or an advisory-lock connection, so
concurrency needs per-call stacks.

**Requirements:**

1. `pipeline.py` — `PipelineDeps` gains
   `llm_stacks: Callable[[], AbstractAsyncContextManager[tuple[MeteredLLM,
   AsyncSession]]] | None = None`. `_screen_finalists`: when there are exactly
   2 finalists AND `llm_stacks` is provided, screen them via `asyncio.gather`,
   each inside its own stack (the stack's MeteredLLM for the paid call, the
   stack's session for the persona cache); otherwise sequential (unchanged).
   `BudgetExhausted`/`LeaseLost` from either branch propagate;
   per-finalist `PipelineFailure` still degrades to a None score.
2. `worker.py`:
   - Pricing gains the ranker tier:
     `models={"fast": ..., "deep": ..., "rank": settings.MODEL_RANKER or
     settings.MODEL_FAST}`.
   - Build an llm-stack factory and pass it into `PipelineDeps`: each stack
     opens its own engine connection for a fresh `FleetSlots` (advisory locks
     are connection-scoped; sharing one connection across concurrent tasks is
     corruption), its own usage session for `LLMGateway`/`RecordedCalls`/
     reserve/reconcile partials, and its own cache session; everything closes
     on exit. Same `max_slots` as the main limiter so the fleet-wide cap is
     one limit, not two.
3. `conftest.py` — FAKE_PRICING already carries the rank tier from Task 2;
   nothing else global.

**Tests:**

- Concurrency: with a stack factory built over `db_session_factory` and an
  event-coordinated fake gateway (first screen call blocks until the second
  arrives), a tied round completes and both screen calls overlap in time —
  proving concurrent execution. Without `llm_stacks`, the same round screens
  sequentially and still completes (fallback covered).
- Each concurrent call still goes through MeteredLLM (recorded llm_calls rows
  exist for both `screen:c1`-style keys).
- Worker pricing: unit test that `MODEL_RANKER=""` falls back to MODEL_FAST
  and a set value is used (construct the Pricing mapping as `main()` does —
  extract a tiny helper if needed to make it testable).

**Verify:** full backend suite green, ruff + mypy clean.

---

## Task 4 — Warm-start groundwork: winner fingerprints + outcome-driven eligibility promotion

**Context:** Commit 2 (cross-Pro warm starts) begins. A winner earns the right
to seed future runs for SIMILAR pros only after a REAL observed 7-day return
outcome — persona scores never qualify. Cross-org reuse is allowed only
through a sanitized, versioned, allowlisted context fingerprint (structured
bands/states from the compressed n8n context; never raw IDs, names, amounts,
free text, rationale, or org-specific facts).

**Requirements:**

1. New module `services/api/src/waypoint/warmstart.py` (retrieval lands in
   Task 5; this task adds the fingerprint contract):
   - `FINGERPRINT_VERSION = "fp_v1"`.
   - `FINGERPRINT_FIELDS`: an explicit allowlist tuple, strictly a subset of
     `n8n.ALLOWED_FIELDS` band/state fields, e.g.: `segment`, `vertical`,
     `plan_tier`, `org_size_band`, `tenure_band`, `lifecycle_stage`,
     `churn_risk_state`, `health_grade`, `platform_usage_band`,
     `feature_adoption_band`, `jobs_created_28d_band`, `open_ar_band`,
     `mrr_band`, `email_engagement_state`. NEVER `org_uuid` or any identifier.
   - `build_fingerprint(brief: OrgBrief) -> dict[str, str]`: the allowlisted
     fields present (non-None) on the brief, values verbatim (they are already
     bands). Nothing else can enter the dict.
2. `tables.py` + migration `0006` — `WinnerRow` gains:
   - `fingerprint: Mapped[dict[str, Any]] = mapped_column(default=dict)`,
   - `fingerprint_version: Mapped[str | None] = mapped_column(default=None)`,
   - `warm_start_eligible: Mapped[bool] = mapped_column(Boolean, default=False)`,
   - `warm_start_evidence: Mapped[dict[str, Any]] = mapped_column(default=dict)`,
   - `validation_status: Mapped[str | None] = mapped_column(default=None)`
     (None = pending; "validated" = observed 7d return; "validated_negative"
     = observed 7d no-return).
   Migration also adds the retrieval/replay indexes:
   - partial index `ix_winners_warm_start` on winners
     `(fingerprint_version, created_at DESC)` WHERE `warm_start_eligible`;
   - `ix_llm_calls_run_pro_status` on llm_calls `(run_id, pro_id, status)`
     (backs `abandon_stale`/replay lookups).
3. `pipeline.py` `_stage_score` — when creating a `kind="winner"` row, stamp
   `fingerprint=build_fingerprint(state.brief)` and
   `fingerprint_version=FINGERPRINT_VERSION` (brief present on that path).
4. `outcomes.py` — promote eligibility idempotently through the EXISTING
   ingestion path: after a batch is applied, for each attributable item whose
   `returned_7d` is not None, update the matching winner (kind "winner" only):
   - `returned_7d is True` → `warm_start_eligible=True`,
     `validation_status="validated"`, and `warm_start_evidence` recording at
     least `{"returned_7d": True, "source": item.source, "mechanism":
     <candidate mechanism>, "channel": <resolved channel>}` (mechanism comes
     from the already-prefetched candidate — retrieval must not need a join
     back into org-scoped rows).
   - `returned_7d is False` → `warm_start_eligible=False`,
     `validation_status="validated_negative"`.
   - Duplicate and late re-ingestion converge to the same state (idempotent);
     unattributed outcomes never promote anything; nothing else (persona
     scores, screens, measurements) may set eligibility.
5. Wherever the winners API view serializes winners (`api.py` run_detail),
   include `warm_start_eligible`, `validation_status`, and
   `fingerprint_version` so the existing winner-review surface shows
   eligibility state (no new dashboard).

**Tests (new `test_warmstart.py` + updates to `test_outcomes.py` /
`test_pipeline.py` / `test_api.py` as needed):**

- 7-day eligibility promotion: ingesting an attributable outcome with
  `returned_7d=True` flips the winner to eligible/validated with evidence;
  `returned_7d=False` → validated_negative, not eligible.
- duplicate ingestion (same recommendation_id+source twice) and late arrival
  (7d flag arriving on a resubmission after an earlier flag-less record) both
  converge idempotently.
- persona-only winners: a freshly scored winner (no outcome ingested) is never
  eligible; nothing in the pipeline sets eligibility.
- sanitization: `build_fingerprint` output contains ONLY allowlisted band
  fields — no `org_uuid`, no identifiers, no free text — and
  `FINGERPRINT_FIELDS ⊆ n8n.ALLOWED_FIELDS`; the stamped winner row carries
  fingerprint + version.
- unattributed outcome (unknown recommendation_id) promotes nothing.
- migration/indexes: `pg_indexes` shows `ix_winners_warm_start` and
  `ix_llm_calls_run_pro_status`.

**Verify:** full backend suite green, ruff + mypy clean.

---

## Task 5 — Warm-start retrieval, candidate competition, telemetry, threshold config

**Context:** Completes commit 2. A new run for a similar pro should not start
cold: bounded structured similarity retrieval over eligible prior winners'
fingerprints finds the best validated mechanism, which enters round 1's batch
as ONE ADDITIONAL candidate that competes through the critic, ranker, and
persona screen like every other idea — never an automatic winner. Retrieval is
Postgres + Python only (bounded scan; NO RAG/vector DB) and is deliberately
kept behind a narrow interface so a future retrieval system could replace it
if telemetry ever proves the need.

**Requirements:**

1. `loop.py` — new editable key `WARM_START_THRESHOLD` (float, default
   **0.75**, validated `0 <= t <= 1`), same config path as the other keys.
   `apps/web` RunStart/RunStatus gain the field ("Warm-start similarity
   (0-1)", min 0) with fixture updates.
2. `warmstart.py`:
   - `DEFAULT_SIMILARITY_WEIGHTS: dict[str, float]` covering
     FINGERPRINT_FIELDS (uniform 1.0 is fine; churn_risk_state / segment /
     lifecycle_stage may weigh higher — pick something defensible and comment
     it). Weights and fields are function parameters with these defaults —
     that is the configurability contract.
   - `similarity(query_fp, candidate_fp, weights) -> float`: weighted share of
     the QUERY fingerprint's weighted fields whose values match exactly on the
     candidate (a field missing on the candidate is a mismatch). Empty query
     fingerprint → 0.0.
   - `@dataclass WarmStartMatch`: `mechanism`, `score`, `winner_id`,
     `fingerprint_version`.
   - `async retrieve(session, brief, *, threshold, weights=...,
     scan_limit=200) -> tuple[WarmStartMatch | None, dict]`: builds the query
     fingerprint; SELECTs winners WHERE `warm_start_eligible` AND
     `fingerprint_version == FINGERPRINT_VERSION` ordered `created_at DESC`
     LIMIT scan_limit (index-backed by `ix_winners_warm_start`); scores each
     in Python; returns the best match at/above threshold plus a telemetry
     dict `{"scanned": n, "latency_ms": float, "best_score": float | None,
     "outcome": "warm" | "cold" | "degraded"}`. Below threshold or no rows →
     `(None, {... "outcome": "cold"})`. ANY exception → `(None, {...
     "outcome": "degraded", "error": str})` and a `log.warning` — a visible
     degraded cold start, never a crash and never a silent failure. Every
     retrieval emits one structured `log.info` with scanned/latency/best_score/
     outcome (warm-start and cold-start rates are derivable from these lines;
     no new dashboard).
   - Rows with missing/unknown fingerprint versions are excluded by the WHERE
     clause (test both).
3. `prompts.py` — `evolve_prompt` gains `warm_start_mechanism: str | None =
   None`; when set, the prompt requires ONE ADDITIONAL idea (count+1 total)
   whose mechanism is exactly the given one, framed as historically validated
   for similar pros, competing on equal terms (still grounded in THIS pro's
   context; all other rules unchanged).
4. `pipeline.py` `_stage_evolve` — on the FIRST round only (empty ledger):
   call `warmstart.retrieve` with the run's threshold; if a match arrives and
   its mechanism is not in the pro's `failed_mechanisms`, pass it to the
   generation prompt (batch size becomes candidate_count + 1; dedupe/refill
   semantics unchanged; the extra candidate flows through critic → ranker →
   screen like all others and can never bypass them). Record the retrieval
   telemetry (+ matched winner_id/score when warm) in round 1's
   `EvolveRoundRow.ranking["warm_start"]`. Degraded retrieval must leave the
   round running cold with the degraded telemetry recorded. The retrieval call
   must not add a paid LLM call.
5. Cross-org isolation: the only data that crosses organizations is the
   sanitized fingerprint, the mechanism label, and aggregate evidence strength
   — the warm-start candidate's content is freshly generated from THIS pro's
   context. No raw evidence, rationale, or identifiers from the source org may
   appear anywhere in the new run's rows or prompts (the source winner_id in
   telemetry is an internal audit key, not org data).

**Tests (extend `test_warmstart.py` + pipeline integration tests):**

- warm vs cold: an eligible similar winner produces a warm round 1 (prompt
  contains the mechanism, batch is count+1, telemetry outcome "warm"); no
  eligible winners → normal cold start (outcome "cold", batch is count).
- threshold boundary: similarity exactly 0.75 matches; just below does not.
- weighted similarity: hand-computed cases including missing-on-candidate
  fields, empty query fingerprint, and custom weights.
- missing/unknown fingerprint versions are never retrieved.
- cross-org isolation: a warm start sourced from another org leaks nothing but
  mechanism + fingerprint (assert the generation prompt and stored rows carry
  no source-org band values or ids beyond the mechanism instruction).
- raw-data sanitization is enforced at retrieval too (winner rows with
  non-allowlisted fingerprint keys — hand-inserted — do not contribute those
  keys to similarity; or assert similarity only reads allowlisted keys).
- retrieval failure: a broken retrieval (inject an exception) yields a
  degraded cold start — the round still completes, telemetry outcome
  "degraded" is persisted, and a warning is logged.
- warm-start candidate competition: ranker places the warm candidate last →
  it is not selected (never an automatic winner); ranker places it first → it
  is screened like any finalist.
- failed-mechanism guard: a warm-start mechanism in the pro's recent failures
  is skipped (cold start).
- replay/retry: re-running round 1 after a crash replays the same warm-start
  decision without a second retrieval changing the recorded outcome (the
  generation call is recorded; retrieval telemetry stays on the committed
  round row).
- index-backed retrieval: the retrieval SQL matches `ix_winners_warm_start`
  (EXPLAIN contains the index name, or at minimum the index exists and the
  query filters on exactly its columns).

**Verify:** full backend suite green, ruff + mypy clean, web tests green.

---

## Final assembly (controller, after all tasks + final review)

1. Final whole-branch code review (most capable model) over
   `git merge-base f22398a HEAD`.. — fix Critical/Important findings.
2. Squash task commits into exactly two commits on `V2-Improvements`:
   - Tasks 1–3 → `feat: add configurable batched idea ranking`
   - Tasks 4–5 → `feat: add validated cross-pro warm starts`
   (soft-reset squash per system; the two systems are never mixed.)
3. Report SHAs, changed files, tests run, remaining external dependencies
   (canonical Amplitude event contract; stable LCM attribution).
