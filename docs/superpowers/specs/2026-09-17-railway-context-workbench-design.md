# Railway Context Workbench Design

## Goal

Run the Context Workbench through the existing authenticated Railway Waypoint API, persist its sanitized state in Postgres, and prevent Workbench execution from overlapping normal Waypoint runs.

## Boundaries

- Reuse the existing Next.js `/api/*` rewrite and Railway API service. Do not create a second service or a new browser origin.
- Reuse the existing Waypoint signed-cookie login. Every Workbench endpoint is operator-authenticated.
- Preserve the existing PII gate. Only sanitized Workbench requests, checkpoints, results, and promotion bundles may be persisted.
- Preserve all three n8n roles: `N8N_CONTEXT_URL` for Standard, `N8N_CONTEXT_URL_WORKBENCH` for full authoring, and `N8N_CONTEXT_URL_STAGING` for curated runtime context.
- Do not modify n8n workflows or Snowflake SQL.

## Architecture

The main `waypoint.api` application owns the Workbench routes under `/api/context-workbench/*`. A Postgres-backed Workbench service schedules at most one queued or running Workbench job and resumes interrupted work after an API restart. The existing local Workbench application remains available for local tests, but the web application no longer talks directly to port 8766.

Alembic revision `0015` creates immutable `context_promotions` and durable `workbench_jobs` tables. JSONB columns store only the already-sanitized request, checkpoint, pruned result, and promotion bundle. The packaged promotion remains a fallback until an operator activates a Postgres promotion.

## Mutual-exclusion gate

The singleton `fleet_control` row is the serialization point for starts. Both run creation and Workbench job creation lock it in their transaction before checking the other workload:

- A non-terminal Waypoint run blocks creation of a Workbench job.
- A queued or running Workbench job blocks Waypoint login and run creation.
- A queued or running Workbench job also blocks another Workbench job.
- Existing work is never killed. The gate clears only when the owning workload reaches a terminal status.
- Read/status endpoints remain available to an already authenticated operator so progress and failures are observable.

This database-backed gate remains correct if two browser requests arrive together; a frontend-only disabled button would not.

## Promotion and runtime flow

The Workbench continues to start from the complete experimental workflow. Promotion writes one immutable sanitized bundle to Postgres and marks it active in the same transaction. Staging runtime loads the active Postgres bundle; if none exists, it loads the packaged promotion committed with the application. Standard runtime never loads a promotion.

Because Workbench execution cannot overlap a Waypoint run, the active promotion cannot change while a run is executing.

## Frontend behavior

The Workbench client uses the same relative `/api` helper as Waypoint. Direct navigation to the Workbench requires the existing Waypoint login. While a Waypoint run is active, the Workbench page displays a clear locked state and polls until the run is terminal. If a Workbench job is active, login and run creation return a precise conflict message.

## Failure handling

- An API restart moves interrupted Workbench work back to queued and resumes it from the last sanitized checkpoint.
- Promotion activation is transactional and preserves immutable prior versions.
- Missing Staging configuration still fails closed.
- A missing Postgres promotion falls back only to the packaged reviewed artifact, never to unreviewed Workbench output.

## Verification

- Migration tests prove revision `0015` creates the expected tables and constraints.
- API tests prove authentication and both directions of the mutual-exclusion gate.
- Workbench tests prove durable resume, one active job, sanitized persistence, and promotion activation.
- Runtime tests prove Postgres promotion preference, packaged fallback, and Standard isolation.
- Frontend tests prove relative `/api` routing, login gating, and the locked Workbench state.

