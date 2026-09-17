# Context Workbench handoff

## Purpose

The Workbench builds a safe, compact context contract for Waypoint. It starts with every variable returned by the experimental Snowflake-through-n8n workflow, optionally adds Context Layer evidence, audits the full safe inventory, drafts AI-useful metadata, and deterministically compiles approved rules. Every variable remains visible under Include, Deprioritize, or Exclude; only variables in Include enter the promoted runtime contract.

The Workbench never edits n8n or Snowflake queries. Change queries outside the Workbench, then run a fresh audit. Its only automatic information-removal rule is PII: the gate removes complete PII variable rows before inventory, persistence, display, or AI authoring. Exact duplicates may collapse, but canonical collisions and other distinct non-PII rules remain.

## Durable checkout

- Branch: `V4-Improvements`
- Worktree: `/Users/jakefassora/projects/pathfinder-waypoint-v2/.claude/worktrees/pathfinder-waypoint-v4`
- Environment: `/Users/jakefassora/projects/pathfinder-waypoint-v2/.claude/worktrees/pathfinder-waypoint-v4/services/api/.env`
- The environment file is ignored, local, and loaded by the backend. Do not print its values or replace it with a vault flow.
- No implementation changes are committed or pushed without explicit approval.

## Start

The deployed Workbench is part of the existing Railway Waypoint API and uses
the existing Vercel `/api/*` proxy. Open the deployed app, choose **Context
Workbench**, and sign in with the same operator password as Waypoint. No
separate port `8766` backend is required. For local development, start the
normal Waypoint API and Next.js applications.

## Use

1. Confirm the page reports the Railway service environment and marks Snowflake and AI configured.
2. Enter the numeric organization ID.
3. Leave **Add Context Layer API coverage** off to prove the experimental n8n source by itself. Turn it on only when you want the additional source.
4. Use the built-in feature catalog or upload a CSV. The feature-key column may be named `feature`, `Feature Key`, or `Display Name`; every key must be unique, and every validated upload is stored as an immutable local version. Duplicate descriptive column names are preserved with numbered suffixes.
5. Leave **Fresh audit** selected to start from every current source variable. Select an older context version only to resume it deliberately.
6. Press **Collect and curate all variables**. Railway creates a durable Postgres job and returns immediately. The n8n client accepts the full variable-audit response and waits up to 240 seconds; if that limit is reached, the source reports an explicit timeout instead of an empty failure reason.
7. Leave the page open or close it. The backend checkpoints after every batch, and the page reconnects to the same job after refresh, browser closure, or backend restart.
8. Review all variables using the Include, Deprioritize, and Exclude filters. The model reports confidence from `0.00` through `1.00`; anything below `0.80` gets one automatic revision. Valid results at or above `0.80` start in Include automatically.
9. Approve any Deprioritized variable you want to move into Include. Variables left in Deprioritize remain parked and do not block saving.
10. Save a new immutable context version. It records the feature-catalog version, confidence threshold, approval states, and excluded keys.
11. Press **Compile reviewed context**. Compilation runs as another durable job and deterministically turns only `auto_approved` or `human_approved` `include` rules into an organization-independent context contract. It does not recollect org data or make an AI call.
12. Press **Test curated context**. The Workbench fetches fresh data for the selected organization, applies the PII gate, and runs the exact Waypoint evolve prompt twice: once with the full scrubbed source context and once with the curated packet. It shows both idea sets, token and latency metrics, and a low-cost `MODEL_FAST` verdict with no more than three small suggested context changes. This is recommendation-only; it never sends outreach or writes to production systems.
13. After the context test, download the Snowflake handoff CSV containing exactly `canonical_key`, `source_table`, and `cohort_aggregate_prompt` for the approved Include variables. Downloading does not activate the context.
14. Give that CSV to Claude with the Snowflake MCP to create or revise the production query. Update n8n manually; the Workbench never edits it. Missing source lineage is written as `UNKNOWN`, never guessed from a query name.
15. Press **Activate in Waypoint** after the compressed n8n query is ready. Activation packages every distinct approved non-PII Include rule and the safe selected feature-catalog version as an immutable Postgres promotion. The promotion summary shows `approved → PII removed → duplicate representations merged → retained`. Duplicate metadata is unioned, so merging removes no source information.
16. On Waypoint's **Start a run** page, leave **Standard context** selected for the existing production workflow, or explicitly choose **Staging context** to test the compressed workflow and active promotion. Retries preserve that choice.

Selecting an older feature version is rollback. Uploading a changed feature catalog shows exact added, removed, and changed counts. Unknown feature mappings are removed and reported as warnings.

Authoring uses low model effort, a 20,000-token generation cap per batch, and a 150,000-output-token cap for the full run. Each batch receives a compact feature index containing exact keys, product areas, and short descriptions instead of every CSV column. Variables returned without every required metadata field are requeued up to three times. A batch that reaches `max_tokens` is split and retried; it is never treated as complete.

Durable deployed job state lives in the Alembic-managed Postgres
`workbench_jobs` table. It contains PII-gated requests, safe variable metadata,
aggregate removal counts, progress, usage, warnings, and pruned results. It
does not contain credentials, PII variable rows, raw pre-PII payloads, or full
authoring prompts/responses. The ignored SQLite store remains only for the
standalone local test application.

Deployed promotions live in the Postgres `context_promotions` table. Staging
prefers its one active immutable promotion and falls back to the reviewed
artifact under `services/api/data/context-promotions/` only when Postgres has
no active version. A bundle contains no organization values or credentials.

Only one workload can execute at a time. An active Waypoint run disables a new
Workbench collection, and an active Workbench job blocks Waypoint login and run
creation. Existing work is never interrupted; the lock clears at a terminal
status.

## Data boundaries

- The browser never collects credentials.
- Raw provider values remain server-side until the app-side PII gate completes.
- Identity-variable rows are removed before the Workbench inventory exists. Business fields such as `FEATURE_VOIP_STATE`, and feature-catalog labels such as `Value Statement`, are not confused with geographic state.
- The authoring model receives variable keys, source query/path, observed type/state, and the verified feature catalog. It does not receive organization values.
- Runtime compilation attaches the complete selected feature catalog as compact product cards containing only the exact feature key, product area, and short value statement. Waypoint receives company-wide feature knowledge without the unused CSV columns.
- Standard Waypoint runs keep the existing `org-context-v2` prompt context and never load a Workbench promotion.
- Staging runs keep only exact promoted canonical aliases returned by `N8N_CONTEXT_URL_STAGING`. Missing promoted keys remain missing, nulls remain explicit nulls, ambiguous generic names such as `count` are never guessed, and unapproved response columns are dropped.
- A missing Staging promotion or URL fails closed; it never silently falls back to Standard.
- A missing or null value describes only this observed organization response.
- Global missingness, freshness, reliability, distributions, and conflict frequency remain unavailable unless evidence is supplied.
- Aggregate prompts must request cohort-level statistics, not calculations from one Pro's value.

## Source contracts

### Experimental n8n

Request:

```json
{"organization_id":"889901"}
```

Response: a list of rows containing `QUERY_NAME`, `VARIABLE_NAME`, `VALUE`, optional `SOURCE_TABLE`, and optional `METADATA`. The Workbench retains and audits every safe row; it does not require an `org-context-v2` wrapper. `SOURCE_TABLE` is required for a complete Snowflake handoff CSV.

### Context Layer

The only endpoint is `GET /api/context_layer/:org`. It returns all related features for an organization in one response. In combined mode, the Workbench sends an `organization_id` directly, so a slow or failed n8n request does not block the Context Layer request. A submitted `pro_uuid` still resolves through n8n first. The response is compared against every exact key in the active feature catalog.

## Current live evidence

On 2026-09-15, a safe compile-mode smoke test for organization `889901` returned 649 n8n rows in 18.4 seconds with no source warning. All 649 rows were accounted for; 66 identity-variable values were removed before authoring.

The documented Context Layer org smoke test returned HTTP 200 through the Workbench in 1.0 second. Against the packaged 26-key catalog, three keys matched and thirteen returned feature names were unmatched. That mismatch is catalog-refinement evidence, not permission to invent aliases.

The 2026-09-15 authoring attempt was not durably saved before the browser state disappeared. The durable job implementation prevents that failure mode for future runs. A new live Anthropic run still transfers scrubbed internal variable metadata and consumes model tokens, so it remains an explicit final verification action.

## Verification

```bash
cd /Users/jakefassora/projects/pathfinder-waypoint-v2/.claude/worktrees/pathfinder-waypoint-v4/services/api
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m pytest -p no:cacheprovider -q
.venv/bin/ruff check src tests
PYTHONPATH=src .venv/bin/mypy src/waypoint
```

```bash
cd /Users/jakefassora/projects/pathfinder-waypoint-v2/.claude/worktrees/pathfinder-waypoint-v4/apps/web
./node_modules/.bin/vitest run
./node_modules/.bin/eslint src
./node_modules/.bin/tsc --noEmit
./node_modules/.bin/next build
```

Passing local tests does not prove that the curated context improves recommendations. Recommendation quality, model-token usage, and end-to-end latency still need evaluation after the real authoring run produces a reviewed catalog.
