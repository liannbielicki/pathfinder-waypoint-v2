# Waypoint Context Promotion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add shared Waypoint/Workbench navigation and promote approved Workbench context plus its feature catalog into a durable runtime bundle with a final three-column CSV handoff.

**Architecture:** Reuse the existing Workbench catalog, compile, evaluation, and job API. Add a standard-library JSON promotion store and deterministic runtime projection; expose one promotion endpoint and one final UI action. Keep legacy Waypoint context only while no promotion exists; an active promotion is fail-closed even when its expected n8n fields are missing.

**Tech Stack:** Python 3.14, FastAPI, Pydantic, standard-library JSON/CSV, Next.js 16, React 19, TypeScript, Vitest, pytest.

---

### Task 1: Lock the promotion and CSV contract

**Files:**
- Create: `services/api/src/waypoint/context_promotion.py`
- Create: `services/api/tests/test_context_promotion.py`
- Modify: `services/api/src/waypoint/workbench.py`
- Modify: `services/api/tests/test_workbench.py`

- [x] Write tests proving only approved Include rules enter a bundle; CSV headers are exactly `canonical_key,source_table,cohort_aggregate_prompt`; missing lineage becomes `UNKNOWN`; feature rows are limited at runtime to referenced exact feature keys.
- [x] Run the focused tests and verify they fail because the promotion contract does not exist.
- [x] Implement immutable JSON write/read/activate helpers with `pathlib`, `json`, `csv`, and `os.replace`; extend audit inventory with exact `source_table` when supplied.
- [x] Run the focused tests and verify they pass.

### Task 2: Expose promotion after evaluation

**Files:**
- Modify: `services/api/src/waypoint/workbench_api.py`
- Modify: `services/api/tests/test_workbench.py`
- Modify: `apps/web/src/lib/workbench.ts`
- Modify: `apps/web/src/components/AuthoringCatalog.tsx`
- Modify: `apps/web/src/components/AuthoringCatalog.test.tsx`

- [x] Write API and component tests proving promotion is unavailable before compile/evaluation, uses the server-stored selected feature catalog and approved rules, and returns the exact CSV only after success.
- [x] Run the focused tests and verify the missing endpoint/action failures.
- [x] Add `POST /api/context-workbench/promotions`, the typed browser client, and a single final `Promote to Waypoint` action with browser CSV download.
- [x] Run the focused tests and verify they pass.

### Task 3: Make Waypoint consume the active promotion safely

**Files:**
- Modify: `services/api/src/waypoint/n8n.py`
- Modify: `services/api/src/waypoint/catalog.py`
- Modify: `services/api/src/waypoint/pipeline.py`
- Modify: `services/api/tests/test_n8n.py`
- Modify: `services/api/tests/test_catalog.py`
- Modify: `services/api/tests/test_pipeline.py`

- [x] Write tests proving promoted canonical keys are retained, missing/null values stay explicit, only referenced promoted feature descriptions enter the prompt, and legacy behavior remains when no active bundle exists.
- [x] Run the focused tests and verify they fail for the missing runtime bridge.
- [x] Add the minimal runtime projection to `OrgBrief` and use it when building the exact existing Waypoint prompt; retain the packaged catalog and legacy brief only when no promotion is active.
- [x] Run the focused tests and verify they pass.

### Task 4: Add the two-tab app shell

**Files:**
- Create: `apps/web/src/components/AppNav.tsx`
- Create: `apps/web/src/components/AppNav.test.tsx`
- Modify: `apps/web/src/app/layout.tsx`
- Modify: `apps/web/src/app/globals.css`

- [x] Write a component test proving Waypoint is first/default and Context Workbench links to `/context-workbench` with active-route state.
- [x] Run the focused test and verify it fails because the navigation does not exist.
- [x] Implement one shared accessible nav in the root layout using Next.js `Link` and `usePathname`.
- [x] Run the focused test and verify it passes.

### Task 5: Verify, document, and make one commit

**Files:**
- Modify: `docs/context-workbench-partner-handoff.md`
- Modify: `.planning/2026-09-16-waypoint-context-promotion/progress.md`

- [x] Document the exact promotion, CSV, manual n8n, active bundle, feature-catalog, and fallback behavior.
- [x] Run the relevant API tests, Ruff, and mypy. The DB-backed full suite remains unavailable in the sandbox because local PostgreSQL access was not authorized.
- [x] Run all web tests, ESLint, TypeScript, and the production build.
- [x] Inspect the entire diff, confirm no secrets or generated database files are staged, then create the single user-authorized commit.
