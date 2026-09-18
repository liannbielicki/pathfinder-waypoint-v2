# Shared Workbench Catalogs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist context and feature catalog versions in Postgres and identify the exact active Staging context in Waypoint with a second-precise timestamp.

**Architecture:** Add one immutable catalog-version table and a small Postgres store. Expose authenticated catalog and active-promotion metadata endpoints, then replace browser-owned catalog data with server reads/writes while retaining selected IDs locally.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy, Alembic/Postgres JSONB, React 19, TypeScript, Vitest, pytest.

---

### Task 1: Add immutable catalog persistence and backfill

**Files:**
- Create: `services/api/alembic/versions/0016_workbench_catalog_versions.py`
- Modify: `services/api/src/waypoint/tables.py`
- Modify: `services/api/src/waypoint/workbench_jobs.py`
- Test: `services/api/tests/test_workbench_jobs.py`

- [ ] Write failing tests proving context and feature versions are shared, identical saves are idempotent, conflicting saves fail, and versions are ordered newest-first.
- [ ] Run the focused tests and confirm they fail because the row/store methods do not exist.
- [ ] Add `WorkbenchCatalogVersionRow` and Postgres store methods `save_catalog_version`, `list_catalog_versions`, and `read_catalog_version`.
- [ ] Add migration `0016`, including SQL backfill from sanitized versioned `workbench_jobs.request` records.
- [ ] Run the focused store tests and migration-backed suite.

### Task 2: Expose safe catalog and active-promotion APIs

**Files:**
- Modify: `services/api/src/waypoint/hosted_workbench.py`
- Modify: `services/api/src/waypoint/api.py`
- Test: `services/api/tests/test_api.py`

- [ ] Write failing API tests for authenticated list/read/create catalog routes and active-promotion metadata.
- [ ] Confirm the routes return 404 before implementation.
- [ ] Add immutable catalog routes under `/api/context-workbench/catalogs` and persist validated feature CSV versions.
- [ ] Extend `/api/fleet/settings` with `staging_context`, populated from the active promotion and matching shared catalog names; keep `staging_context_available` for compatibility.
- [ ] Make availability false when there is no active promotion, because Staging cannot run successfully without one.
- [ ] Run focused API tests.

### Task 3: Replace browser-owned catalog data

**Files:**
- Modify: `apps/web/src/lib/catalogVersions.ts`
- Modify: `apps/web/src/lib/workbench.ts`
- Modify: `apps/web/src/components/WorkbenchRunForm.tsx`
- Modify: `apps/web/src/components/AuthoringCatalog.tsx`
- Test: `apps/web/src/lib/workbench.test.ts`
- Test: `apps/web/src/components/WorkbenchRunForm.test.tsx`

- [ ] Write failing tests proving shared server versions populate the selector, a selected version opens without a new run, saves POST immutable context versions, and legacy local versions upload once.
- [ ] Add typed list/read/save catalog client functions.
- [ ] Load shared versions from the server and retain only selected IDs in local storage after one-time legacy upload.
- [ ] Make `AuthoringCatalog` await the server save before marking a version saved.
- [ ] Run focused frontend tests.

### Task 4: Show the exact active Staging context

**Files:**
- Modify: `apps/web/src/lib/api.ts`
- Modify: `apps/web/src/components/RunStart.tsx`
- Test: `apps/web/src/components/RunStart.test.tsx`

- [ ] Write a failing component test with a fixed active-promotion timestamp and assert the catalog IDs, names, variable count, and second-precise time are visible only for Staging.
- [ ] Extend `FleetSettings` with the safe active-context summary.
- [ ] Render the active summary beneath the Staging radio option using `Intl.DateTimeFormat` with date, time, seconds, and timezone.
- [ ] Render an explicit unavailable explanation when no active promotion exists.
- [ ] Run focused component tests.

### Task 5: Verify the complete change

**Files:**
- Modify generated contract files only if the repository's normal contract generation requires it.

- [ ] Run backend pytest, Ruff, and mypy.
- [ ] Run frontend Vitest, ESLint, TypeScript, and Next production build.
- [ ] Inspect the final diff for secrets, raw organization values, unintended n8n/SQL edits, and accidental activation behavior.
- [ ] Leave the verified changes uncommitted until the user explicitly requests a commit and push.
