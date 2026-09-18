# Per-Run Context Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add a durable Standard/Staging context-source choice per Waypoint run, with Standard unchanged, PII gated before all Workbench and Staging context, and every distinct approved non-PII rule preserved.

**Architecture:** Persist a closed `context_source` value on each run and choose between two long-lived n8n clients in the worker. Apply one shared fail-closed PII sanitizer before Workbench inventory/AI boundaries and again before Staging compilation; do not exclude non-PII canonical conflicts, and require canonical aliases at runtime instead of ambiguous source-key guessing.

**Tech Stack:** Python 3.14, FastAPI, Pydantic, SQLAlchemy/Alembic, httpx, PostgreSQL, pytest, Next.js 16, React 19, TypeScript, Vitest.

---

### Task 1: Persist and expose the run context source

**Files:**
- Create: `services/api/alembic/versions/0014_run_context_source.py`
- Modify: `services/api/src/waypoint/models.py`
- Modify: `services/api/src/waypoint/tables.py`
- Modify: `services/api/src/waypoint/api.py`
- Test: `services/api/tests/test_persistence.py`
- Test: `services/api/tests/test_api.py`

- [x] Add failing tests proving omitted values become `standard`, `staging` is accepted and returned, unknown values fail validation, and persisted rows retain the selection.
- [x] Run the focused tests and confirm failures are caused by the missing field/column.
- [x] Add `ContextSource = Literal["standard", "staging"]`, the request/view fields, the non-null text column with server/default `standard`, and migration upgrade/downgrade.
- [x] Copy `context_source` into `RunRow` at run creation and expose it through `_view`.
- [x] Rerun focused persistence/API tests until green.

### Task 2: Isolate Standard and Staging context projection

**Files:**
- Modify: `services/api/src/waypoint/context_promotion.py`
- Modify: `services/api/src/waypoint/n8n.py`
- Modify: `services/api/src/waypoint/pipeline.py`
- Test: `services/api/tests/test_n8n.py`
- Test: `services/api/tests/test_pipeline.py`

- [x] Add failing tests proving a Standard client ignores promotions, a Staging client applies its injected promotion store, nulls remain explicit, and a run selects the matching client.
- [x] Run the focused tests and confirm the new routing/projection expectations fail.
- [x] Remove the module-global promotion decision from row parsing. Give `N8NContextClient` an optional promotion store; absent means legacy Standard projection, present means curated Staging projection.
- [x] Extend `PipelineDeps` with an optional Staging context client and select it only when `run.context_source == "staging"`; fail clearly if a Staging run has no client.
- [x] Rerun the focused tests until green.

### Task 2A: Enforce PII-only exclusion before Workbench and Staging

**Files:**
- Modify: `services/api/src/waypoint/workbench.py`
- Modify: `services/api/src/waypoint/workbench_api.py`
- Modify: `services/api/src/waypoint/context_promotion.py`
- Modify: `services/api/src/waypoint/n8n.py`
- Modify: `services/api/scripts/export_context_promotion.py`
- Test: `services/api/tests/test_workbench.py`
- Test: `services/api/tests/test_context_promotion.py`
- Test: `services/api/tests/test_n8n.py`

- [x] Add failing tests proving PII variable rows are absent before inventory and AI authoring, PII filtering precedes catalog validation, canonical collisions are preserved, and Staging accepts only exact canonical aliases.
- [x] Run the focused tests and confirm failures describe the current late gate, collision exclusion, and source-key fallback.
- [x] Make the shared source sanitizer drop complete PII variable rows, retain every distinct non-PII row, and record only aggregate removal counts outside AI context.
- [x] Remove canonical collisions as an automatic exclusion reason; deterministically collapse only exact duplicate promotion rules.
- [x] Sanitize feature-card values and require canonical Staging response keys; missing aliases remain missing.
- [x] Use the same pure promotion builder from the API preview and offline exporter so filter order cannot drift.
- [x] Rerun focused tests until green.

### Task 3: Configure worker endpoints and package the approved promotion

**Files:**
- Create: `services/api/data/context-promotions/active.json`
- Create: `services/api/data/context-promotions/promotion-4944dacc87f06d0d.json`
- Modify: `services/api/src/waypoint/context_promotion.py`
- Modify: `services/api/src/waypoint/settings.py`
- Modify: `services/api/src/waypoint/worker.py`
- Modify: `.env.example`
- Test: `services/api/tests/test_context_promotion.py`
- Test: `services/api/tests/test_settings.py`
- Test: `services/api/tests/test_pipeline.py`

- [x] Add failing tests for optional `N8N_CONTEXT_URL_STAGING`, the packaged promotion loader, the approved-to-PII-removed-to-retained count contract, safe feature cards, and worker dependency routing.
- [x] Run those tests and verify the missing setting/artifact behavior fails.
- [x] Generate the immutable artifact from evaluation job `5b7eba5c-97df-4088-b043-c2676089200d`, retaining every distinct approved non-PII rule and safe feature field; verify no organization values or credentials are present.
- [x] Add a packaged-promotion root and make the worker create Standard without a store and Staging with the packaged store. Reuse `N8N_TOKEN`, timeout, and concurrency settings.
- [x] Document all three URL names in `.env.example`, with Staging optional and Workbench separate.
- [x] Rerun focused tests until green.

### Task 4: Validate Staging availability at the API boundary

**Files:**
- Modify: `services/api/src/waypoint/api.py`
- Modify: `services/api/src/waypoint/models.py`
- Test: `services/api/tests/test_api.py`

- [x] Add failing tests proving `/api/fleet/settings` returns only a `staging_context_available` boolean and run creation rejects Staging with 422 when the URL is unset.
- [x] Run the tests and confirm the behavior is absent.
- [x] Add the boolean and fail-closed validation without exposing either URL.
- [x] Rerun focused tests until green.

### Task 5: Add the Start Run control and preserve it on retry

**Files:**
- Modify: `apps/web/src/components/RunStart.tsx`
- Modify: `apps/web/src/components/RetryPanel.tsx`
- Modify: `apps/web/src/lib/api.ts`
- Modify: `apps/web/src/components/RunStart.test.tsx`
- Modify: `apps/web/src/components/RetryPanel.test.tsx`
- Modify: `apps/web/src/test/fixtures.ts`

- [x] Add failing component tests proving Standard is selected and submitted by default, Staging is disabled when unavailable, enabled Staging is submitted explicitly, and retry preserves the original source.
- [x] Run the focused Vitest files and confirm expected failures.
- [x] Add the minimal Standard/Staging radio control and helper text, extend the fleet-settings type, and pass `context_source` through retry.
- [x] Rerun focused frontend tests until green.

### Task 6: Synchronize contracts and documentation

**Files:**
- Modify: `contracts/openapi.json`
- Modify: `apps/web/src/lib/api-types.ts`
- Modify: `docs/HUMAN-TASKS.md`
- Modify: `docs/context-workbench-partner-handoff.md`

- [x] Regenerate `contracts/openapi.json` from `waypoint.api:app` and regenerate frontend API types.
- [x] Update deployment documentation with the Standard/Staging/Workbench boundary, shared token, initial `287 approved → 64 PII removed → 4 duplicate representations merged → 219 retained` artifact, and explicit note that secure Railway Workbench hosting is a separate task.
- [x] Run the contract test and TypeScript compiler.

### Task 7: Full verification and one implementation commit

**Files:** all files above.

- [x] Run the complete backend test suite, Ruff, and mypy.
- [x] Run the complete frontend Vitest suite, ESLint, TypeScript compiler, and production build.
- [x] Run `git diff --check`; inspect the artifact for secrets and verify its exact counts.
- [x] Review every requirement in the approved design against the diff.
- [x] Commit the implementation as one coherent commit on `V4-Improvements`; do not push without explicit approval.
