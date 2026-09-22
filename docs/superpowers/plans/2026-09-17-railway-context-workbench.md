# Railway Context Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the authenticated Context Workbench from the existing Railway API with durable Postgres state and strict mutual exclusion from Waypoint runs.

**Architecture:** Add Alembic-managed Workbench job and promotion tables, a small async Postgres store, and authenticated Workbench routes registered on `waypoint.api`. Serialize Workbench and Waypoint starts through the existing `fleet_control` row; route the frontend through the existing `/api` proxy and let Staging prefer the active Postgres promotion with the packaged artifact as fallback.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, PostgreSQL JSONB, Next.js/React, Vitest, pytest.

---

### Task 1: Add durable Workbench tables and stores

**Files:**
- Modify: `services/api/src/waypoint/tables.py`
- Create: `services/api/alembic/versions/0015_workbench_state.py`
- Modify: `services/api/src/waypoint/workbench_jobs.py`
- Modify: `services/api/tests/conftest.py`
- Modify: `services/api/tests/test_workbench_jobs.py`

- [ ] Write failing database tests for sanitized job creation/checkpoint/completion, one-active-job enforcement, immutable promotion activation, and active promotion reads.
- [ ] Run the focused tests and confirm they fail because the Postgres models/store do not exist.
- [ ] Add `WorkbenchJobRow` and `ContextPromotionRow`, revision `0015`, and the minimal async store using the existing session factory.
- [ ] Run focused persistence tests and confirm they pass.

### Task 2: Register authenticated hosted Workbench routes

**Files:**
- Modify: `services/api/src/waypoint/workbench_api.py`
- Create: `services/api/src/waypoint/hosted_workbench.py`
- Modify: `services/api/src/waypoint/api.py`
- Modify: `services/api/tests/test_api.py`
- Modify: `services/api/tests/test_workbench.py`

- [ ] Write failing API tests proving Workbench routes exist on the main app, require the Waypoint session, persist jobs, resume checkpoints, and activate promotions.
- [ ] Write failing concurrency tests proving active Waypoint blocks Workbench, active Workbench blocks login/run creation, and two Workbench starts cannot overlap.
- [ ] Make checkpoint callbacks awaitable so Postgres checkpoints are durable before execution continues while retaining the local test application.
- [ ] Register the hosted routes and implement the `fleet_control` transactional gate with precise HTTP 409 errors.
- [ ] Run the focused API and Workbench suites.

### Task 3: Load active Postgres promotion in Staging

**Files:**
- Modify: `services/api/src/waypoint/n8n.py`
- Modify: `services/api/src/waypoint/worker.py`
- Modify: `services/api/tests/test_n8n.py`
- Modify: `services/api/tests/test_pipeline.py`

- [ ] Write failing tests that Staging prefers the active database promotion, falls back to the packaged reviewed promotion, and Standard never loads either.
- [ ] Add the smallest async promotion-loader seam to `N8NContextClient` and wire it from the worker session factory.
- [ ] Run the focused n8n and pipeline tests.

### Task 4: Move the Workbench frontend onto the shared Railway API

**Files:**
- Modify: `apps/web/src/lib/workbench.ts`
- Modify: `apps/web/src/lib/workbench.test.ts`
- Modify: `apps/web/src/lib/api.ts`
- Modify: `apps/web/src/app/context-workbench/page.tsx`
- Modify: `apps/web/src/components/WorkbenchRunForm.tsx`
- Modify: relevant frontend tests

- [ ] Write failing frontend tests for relative `/api/context-workbench/*` calls, shared login, and a locked page while Waypoint is active.
- [ ] Replace the localhost client with the existing relative API helper and add minimal authenticated activity polling.
- [ ] Keep all server-side conflict checks authoritative; UI locking is explanatory only.
- [ ] Run focused Vitest tests.

### Task 5: Documentation, full verification, review, and one commit

**Files:**
- Modify: `.env.example`
- Modify: `docs/HUMAN-TASKS.md`
- Modify: `docs/context-workbench-partner-handoff.md`
- Modify: `contracts/openapi.json` and generated frontend types if required

- [ ] Document the single Railway service, the three n8n variables, migration `0015`, and the mutual-exclusion behavior without exposing values.
- [ ] Run the full backend test, Ruff, and mypy commands.
- [ ] Run the full frontend test, lint, typecheck, and production build commands.
- [ ] Request an independent code review, fix all Critical and Important findings, and rerun affected verification.
- [ ] Commit the complete change once on `V4-Improvements`; do not push without separate authorization.

