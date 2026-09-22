# Progress

## Pre-edit contract

Git reality: GREEN after approved fast-forward; local V4 equals `origin/V4-Improvements`, prior Workbench edits are preserved, and two conflicts were manually combined.
Target behavior: Shared tabs, final promotion, exact three-column approved-Include CSV, active runtime bundle with relevant feature definitions, no n8n mutation.
Files likely touched: Workbench API/compiler/UI, one promotion module, Waypoint n8n/catalog/pipeline, root layout/nav, focused tests and handoff docs.
Existing pattern to reuse: Workbench compiled catalog, feature catalog validation, standard-library job persistence, exact existing Waypoint evolve prompt.
Ponytail decision: reuse existing plus stdlib/native; no new database, service, state library, SQL generator, or n8n client mutation endpoint.
Acceptance check: Focused contract/API/runtime/nav tests plus complete backend/frontend verification.
Verification command: API pytest/Ruff/mypy and web Vitest/ESLint/tsc/Next build.
Do not build: n8n editing, Snowflake SQL generation, full-catalog runtime dumps, inferred source tables, or a separate app.

## Red-green evidence

- Promotion contract: import failed before `context_promotion.py`; 4 focused tests pass after implementation.
- Promotion API/UI: endpoint rejected the new argument and the UI lacked the action before implementation; backend test and 11 AuthoringCatalog tests pass afterward.
- Runtime bridge: promoted helper imports were missing before implementation; promoted n8n and prompt-context tests pass afterward.
- Shared tabs: `AppNav` import failed before implementation; 2 navigation tests pass afterward.
- Combined focused verification: 87 backend tests and 28 frontend tests pass. Ruff, strict mypy, ESLint, and TypeScript pass.
- Independent review found that promotion trusted browser-supplied content, edits retained stale evaluation state, zero-match active promotions fell back to broad legacy context, and repeat promotion was not idempotent.
- Added server-authoritative promotion by completed evaluation job ID, strict stored field allowlists plus the deterministic PII-key gate, full downstream UI invalidation on edits, fail-closed empty promoted packets, and stable repeat promotion.
- Final relevant verification: 98 backend tests and all 103 frontend tests pass; Ruff, strict mypy, ESLint, TypeScript, and the Next production build pass. The DB-backed full API suite could not be rerun because local PostgreSQL permission was declined.
