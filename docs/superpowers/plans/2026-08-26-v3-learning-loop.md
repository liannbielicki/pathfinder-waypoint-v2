# Waypoint V3 Learning Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the V3 learning loop: authoritative send-based measurement, 1d/7d/30d checkpoints, canonical item identity over an expandable theme corpus, exposure-level attribution (including neutral/control), deterministic measurement selection, and bounded checkpoint scheduling with an independent kill switch.

**Architecture:** Waypoint stays recommendation-only; LCM Personalization owns SMS creation/QA/send. Measurement starts at authoritative send confirmation (never LCM intake ack). Learning reads Day-1/Day-7 checkpoints; Day-30 is diagnostic only. Identity is exposure-level (`ExposureRow`) with canonical `item_id`/`item_version` resolved from a versioned, organically grown items corpus via replaceable structured+fuzzy retrieval (no vector infra). Callers can never override attribution identity. A bounded checkpoint sweep on the worker maintenance beat resolves unmeasured horizons to measured negatives, gated by a learning-loop kill switch independent of the fleet kill switch.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Postgres, pytest (existing stack; difflib from stdlib for fuzzy matching).

## Global Constraints

- Waypoint remains recommendation-only; zero sends.
- Do not build or modify anything under `n8n/`.
- LCM intake acknowledgement is never proof of delivery; checkpoint clocks start at `send_status == "confirmed"`.
- Checkpoints: 24-hour (`returned_1d`), 7-day (`returned_7d`), 30-day (`returned_30d`). Day 1 + Day 7 drive learning; Day 30 diagnostic only.
- A-only evidence is directional; A+B evidence is causal; only causal positive promotes warm-start eligibility.
- Neutral/control exposures never require a WinnerRow.
- No hard-coded theme set; the items corpus is expandable with organic, versioned metadata.
- Retrieval is structured + fuzzy over the full corpus, behind a replaceable interface; no vector infrastructure.
- Callers must never override attribution identity (item_id, item_version, pro_id, org_id, arm where an exposure exists).
- Version markers: `LEARNING_VERSION`, `RESOLVER_VERSION`, `CHECKPOINT_VERSION`.
- Checkpoint scheduling is bounded per sweep; failures are logged and retried on the next beat.
- Handoff loading is set-based (IN() prefetch, no per-winner SELECT loop).
- Remove: 30-day suppression predicate; LLM-driven measurement selection.
- Do not touch unrelated dirty files in the main worktree.

---

### Task 1: Fix the `_apply_item` positional-argument bug (broken baseline)

**Files:** Modify `services/api/src/waypoint/outcomes.py:285`

`_apply_batch` passes `(session, item, winner, run, candidate, existing_by_key, exposure)` into a signature `(session, item, winner, exposure, run, candidate, existing_by_key)`. Call with keywords. 17 existing tests already fail on this; they are the failing tests. Run full suite → green baseline. Commit.

### Task 2: Attribution authority hardening

**Files:** Modify `services/api/src/waypoint/models.py`, `outcomes.py`; Test `tests/test_outcomes.py`

- Pydantic validator on `TouchOutcomeIn`: `returned_1d/7d/30d is True` requires `first_return_at`.
- Winner-path `arm`: caller value accepted only when no exposure row exists (exposure is arm authority).
- Test: caller-supplied `item_id`/`item_version`/`pro_id`/`org_id` never override winner/exposure identity.

### Task 3: Deterministic measurement selection (remove LLM-driven selection)

**Files:** Modify `measurement.py`, `pipeline.py` (`_stage_measure`, delete `_KeyedMeasureLLM`), `worker.py`; Test `tests/test_measurement.py`

`select_indicators(mechanism: str, catalog) -> MeasurementPlan`: always `app_return`; plus one mechanism-mapped metric via keyword mapping (deterministic, no LLM call, no UnmeasurableWinner abstention for parse failures — a winner is always measurable now). Delete `measurement_prompt`, `MeasurementSelection`, `LLMLike`, `create_measurement_plan`'s LLM path. Update pipeline/worker wiring and tests.

### Task 4: Day-1/Day-7 learning, Day-30 diagnostic; remove 30-day suppression

**Files:** Modify `evidence.py`; Test `tests/test_evidence.py`

- `failed_mechanisms` predicate: `unsubscribed OR returned_7d IS FALSE` (was `returned_30d IS FALSE`).
- Evidence horizons: learning = `1d`, `7d`; `30d` reported as diagnostic-only in `evidence_block`; drop 14d/90d from prompt evidence (columns stay).
- `pattern_summaries` keeps item-level grouping.

### Task 5: Items corpus + structured/fuzzy resolution (migration 0009)

**Files:** Create `services/api/src/waypoint/items.py`, `alembic/versions/0009_v3_learning_loop.py`; Modify `tables.py`, `pipeline.py` (`_stage_score`); Test `tests/test_items.py`

- `ItemRow`: id, mechanism, channel, concept (canonical text), version (int), metadata JSONB, status (`active`), created_at; unique (mechanism, channel, concept-hash) not needed — resolution dedupes.
- `resolve_item(session, recommendation) -> ResolvedItem(item_id, item_version, resolver_version, created)`:
  structured filter (mechanism, channel) then `difflib.SequenceMatcher` ratio on `pro_facing_concept` over the full corpus slice; ≥ 0.9 → same item/version; ≥ 0.6 → same item, new version (metadata records the drift); else new item v1. `RESOLVER_VERSION = "resolver_v1"`.
- Wire into `_stage_score`: winners get `item_id`/`item_version` at creation; resolution failure degrades to `legacy_unresolved=True`, never blocks the winner.
- Migration 0009 also carries Tasks 6–8 columns (one migration for the release).

### Task 6: Exposure registration endpoint (`POST /api/exposures`)

**Files:** Create `services/api/src/waypoint/exposures.py`; Modify `api.py`, `models.py`; Test `tests/test_exposures.py`

- `ExposureIn`: optional `recommendation_id` (winner-linked), `pro_id`, `org_id`, `item_id`, `item_version`, `arm` (`A|B|control`), `channel`, `send_status`, `sent_at`.
- Winner-linked: identity (pro/org/item) derived from the winner; caller values ignored. Control/neutral: caller identity accepted, no WinnerRow required.
- Idempotent per caller-supplied `exposure_id`; send confirmation updates `send_status`/`sent_at` in place and stamps `learning_version`.

### Task 7: Bounded checkpoint sweep with retry/failure handling

**Files:** Create `services/api/src/waypoint/checkpoints.py`; Modify `tables.py`, `outcomes.py` (reuse promotion); Test `tests/test_checkpoints.py`

- `resolve_due_checkpoints(session, now, limit=500) -> int`: rows with `send_status='confirmed'`, `sent_at` older than horizon + grace (6h), horizon flag `IS NULL`, and no qualifying `first_return_at` → flag becomes measured `False`; stamps `checkpoint_version`. Ordered by `sent_at`, LIMIT-bounded. Re-runs promotion for affected winners (7d flags only). Idempotent; exceptions roll back and the next beat retries.
- `CHECKPOINT_VERSION = "checkpoints_v1"`; `checkpoint_version` column on `touch_outcomes` (migration 0009).

### Task 8: Independent learning kill switch + worker beat

**Files:** Modify `settings.py`, `tables.py` (`FleetControlRow.learning_killed`), `worker.py`, `api.py`/`queue.py` as needed; Test `tests/test_checkpoints.py`

- `Settings.LEARNING_KILL_SWITCH` (env-owned, default False) applied in `apply_fleet_settings`/`_ensure_fleet`, independent of `killed`.
- Maintenance beat: when not learning-killed, run one bounded `resolve_due_checkpoints` sweep per idle beat; failures logged, never crash the loop.

### Task 9: Set-based handoff loading

**Files:** Modify `handoff.py` (`ready_rows`); Test `tests/test_handoff.py`

Replace per-winner `MeasurementRow`/`CandidateRow` selects with two IN() prefetches keyed by winner ids. Same output rows, same lineage guard.

### Task 10: Corpus performance test + full verification

**Files:** Test `tests/test_items.py` (perf case: 500-item corpus resolution < 1s); run `uv run pytest -q -m "not live"`, `uv run ruff check`, `uv run mypy src`. Update the architecture notes in `docs/plans/pathfinder-waypoint-v2-implementation.md` (V3 learning loop section).

---

Self-review: every stated V3 requirement maps to a task (recommendation-only and no-n8n are constraints, not tasks). Promotion semantics (A-only directional → `validation_status="directional"`, A+B causal → validated) land in Task 2 against `_promote_warm_start`.

## Review deltas (independent plan review, accepted)

- Task 1 also fixes a second 07ce754 bug: the new-row branch of `_apply_item` clobbers the `key` tuple with the checkpoint-flag loop variable; and updates the stale spine test to confirm the send (V3 send-authority rule).
- Task 2 drops the Pydantic returned-flag validator — legacy sources post flags without `first_return_at` and must keep working; derived flags already override caller flags. Directional/causal `evidence_kind` lands here.
- Task 3 must also update `tests/conftest.py` (imports `create_measurement_plan`, `MEASURE_JSON`, FakeLLM measure stage), `tests/test_parity.py`, and `tests/test_pipeline.py` measure tests, and remove the now-dead measure-call cost wiring in `_stage_measure`.
- Task 5 adds a unique index on `(mechanism, channel, concept_hash)` with conflict-safe re-resolve so concurrent workers cannot split item identity.
- Task 6: exposure attribution fills `journey_window` from the exposure's run so control rows never pollute another window's evidence; arm vocabulary stays `A`/`B` with `B` ≡ control/neutral (documented, no `"control"` literal).
- Task 7: the sweep also synthesizes measured-negative outcome rows (`source="checkpoint"`) for confirmed exposures with no outcome row — the B/control side of causal evidence — and runs on a timed cadence in the worker (not only idle beats), gated by the learning kill switch.
- Task 8 owns `LEARNING_VERSION` (in `checkpoints.py`) and the `exposures.learning_version` column rides migration 0009.

## Post-implementation review fixes (independent code review, all applied)

1. `exposures.winner_id` link added (schema + registration) so a silent control exposure's synthesized negative reaches its winner's causal comparison; promotion (`outcomes.promote_winners`, shared with the sweep) joins direct outcome rows with linked-exposure rows.
2. `TouchOutcomeIn` no longer carries `returned_*`, `arm`, or item identity — callers cannot assert horizons or rewrite attribution; timestamps are `AwareDatetime`.
3. Outcomes always consult the exposure when `exposure_id` is present; the exposure is the identity authority (arm, item, send state) even for winner outcomes.
4. Synthesized checkpoint rows inherit the run's `journey_window`; `pattern_summaries` groups by `(item_id, item_version)` and excludes arm-B rows from treatment rates.
5. A-only directional evidence now revokes `warm_start_eligible`.
6. Item drift bump is an optimistic guarded UPDATE inside a SAVEPOINT with hash-collision fallback; exact-hash matches resolve across retired items.
7. `_stage_score` rolls back on `SQLAlchemyError` from resolution before persisting the winner.
8. Checkpoint sweep uses `FOR UPDATE SKIP LOCKED`, recovers from synthesis races (`IntegrityError` → retry next tick); exposure send state only advances (no downgrade from confirmed, `sent_at` frozen after confirmation); `exposure_id` is required for idempotent registration.
