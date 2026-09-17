# Context Workbench Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a guided, durable Workbench that audits all experimental variables and deterministically compiles reviewed, feature-verified context for Waypoint.

**Architecture:** Keep `workbench.py` responsible for deterministic source, catalog, audit, and compile primitives; keep `workbench_api.py` responsible for orchestration and AI drafting; keep browser local storage responsible for immutable experimental versions. Replace the mode-heavy form and table with a guided source/catalog/run flow and compact variable cards.

**Tech Stack:** Python 3.14, FastAPI, Pydantic, httpx, pytest, Next.js 16, React 19, TypeScript, Vitest, Testing Library, browser localStorage.

---

### Task 1: Lock deterministic backend contracts

**Files:**
- Modify: `services/api/src/waypoint/workbench.py`
- Modify: `services/api/tests/test_workbench.py`

- [ ] Add failing tests for 650 audit rows, exact observed state/type/query/path, catalog CSV validation, Context Layer feature coverage, and compact compilation that includes only reviewed `include` entries.
- [ ] Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .../python -m pytest -p no:cacheprovider tests/test_workbench.py -q` and confirm the new tests fail for missing functions.
- [ ] Add minimal Pydantic models and pure helpers: `parse_feature_catalog_csv`, `build_audit_inventory`, `context_layer_coverage`, `validate_catalog_entries`, and `compile_context`.
- [ ] Re-run the focused backend suite and confirm it passes.

### Task 2: Separate collect, curate, and compile orchestration

**Files:**
- Modify: `services/api/src/waypoint/workbench_api.py`
- Modify: `services/api/tests/test_workbench.py`

- [ ] Add failing API tests proving environment status exposes booleans/path only, n8n works without Context Layer, both-mode resolves `ORG_UUID` before Context Layer, source failures remain source-specific, authoring prompts omit values, invalid feature mappings are warned and removed, and compile mode never invokes the model.
- [ ] Add `GET /api/context-workbench/status` and `POST /api/context-workbench/catalog/validate`.
- [ ] Update run orchestration to collect n8n first, optionally collect Context Layer, retain partial source success, audit the full scrubbed inventory, draft review-only entries, and compile deterministically.
- [ ] Use a bounded 180-second n8n timeout and report source duration/row counts.
- [ ] Re-run the focused backend suite.

### Task 3: Add immutable browser catalog/version helpers

**Files:**
- Modify: `apps/web/src/lib/catalogVersions.ts`
- Create: `apps/web/src/lib/catalogVersions.test.ts`
- Modify: `apps/web/src/lib/workbench.ts`

- [ ] Add failing tests for immutable feature/context versions, exact-key added/removed/changed diffs, rollback by selection, and corrupt-storage recovery.
- [ ] Add the smallest typed helpers needed to validate a CSV through the API, store feature versions and context versions separately, select either version, and compare exact feature keys.
- [ ] Re-run the focused Vitest file.

### Task 4: Replace the confusing form and table

**Files:**
- Modify: `apps/web/src/app/context-workbench/page.tsx`
- Modify: `apps/web/src/components/WorkbenchRunForm.tsx`
- Modify: `apps/web/src/components/AuthoringCatalog.tsx`
- Modify: `apps/web/src/components/WorkbenchTimeline.tsx`
- Modify: `apps/web/src/app/globals.css`
- Modify: `apps/web/src/components/WorkbenchRunForm.test.tsx`
- Modify: `apps/web/src/components/WorkbenchTimeline.test.tsx`
- Create: `apps/web/src/components/AuthoringCatalog.test.tsx`

- [ ] Add failing component tests for Snowflake-default guided copy, optional Context Layer, visible env status, source-specific error text, searchable compact cards, review toggles, immutable save, and compile action.
- [ ] Build the Connect/Collect/Audit/Curate/Compile flow with one primary action per stage.
- [ ] Render variables as compact expandable cards; remove the six-column editable table.
- [ ] Keep advanced trace JSON collapsed behind a diagnostics section.
- [ ] Re-run focused component tests and ESLint.

### Task 5: Refresh handoff and verify end to end

**Files:**
- Modify: `docs/context-workbench-partner-handoff.md`

- [ ] Document the durable worktree, exact startup commands, current `.env` path, source contracts, feature/context version workflow, and clear live test steps without secret values.
- [ ] Run backend Workbench tests, frontend Workbench tests, frontend lint, type/build verification, and `git diff --check`.
- [ ] Start the backend and frontend from this durable worktree, verify `/health`, `/status`, and the page, then run a safe live n8n smoke test using the existing `.env` and report exact non-secret row count/latency or precise failure.
- [ ] Inspect the full diff and record any remaining live integration or recommendation-quality gaps without claiming they are solved.
