# Context Workbench handoff

## Purpose

The Workbench builds a safe, compact context contract for Waypoint. It starts with every variable returned by the experimental Snowflake-through-n8n workflow, optionally adds Context Layer evidence, audits the full safe inventory, drafts AI-useful metadata, and deterministically compiles approved rules. Every variable remains visible under Include, Deprioritize, or Exclude; only variables in Include enter the promoted runtime contract.

The Workbench never edits n8n or Snowflake queries. Change queries outside the Workbench, then run a fresh audit.

## Durable checkout

- Branch: `V4-Improvements`
- Worktree: `/Users/jakefassora/projects/pathfinder-waypoint-v2/.claude/worktrees/pathfinder-waypoint-v4`
- Environment: `/Users/jakefassora/projects/pathfinder-waypoint-v2/.claude/worktrees/pathfinder-waypoint-v4/services/api/.env`
- The environment file is ignored, local, and loaded by the backend. Do not print its values or replace it with a vault flow.
- No implementation changes are committed or pushed without explicit approval.

## Start

Backend:

```bash
cd /Users/jakefassora/projects/pathfinder-waypoint-v2/.claude/worktrees/pathfinder-waypoint-v4/services/api
PYTHONPATH=src .venv/bin/python scripts/run_workbench.py
```

Frontend:

```bash
cd /Users/jakefassora/projects/pathfinder-waypoint-v2/.claude/worktrees/pathfinder-waypoint-v4/apps/web
./node_modules/.bin/next dev
```

Open `http://localhost:3000`. **Waypoint** is the default top tab and **Context Workbench** is the second tab at `/context-workbench`.

## Use

1. Confirm the page shows the exact `services/api/.env` path and marks Snowflake and AI configured.
2. Enter the numeric organization ID.
3. Leave **Add Context Layer API coverage** off to prove the experimental n8n source by itself. Turn it on only when you want the additional source.
4. Use the built-in feature catalog or upload a CSV. The feature-key column may be named `feature`, `Feature Key`, or `Display Name`; every key must be unique, and every validated upload is stored as an immutable local version. Duplicate descriptive column names are preserved with numbered suffixes.
5. Leave **Fresh audit** selected to start from every current source variable. Select an older context version only to resume it deliberately.
6. Press **Collect and curate all variables**. The backend creates a durable SQLite job and returns immediately. The n8n client accepts the full variable-audit response and waits up to 240 seconds; if that limit is reached, the source reports an explicit timeout instead of an empty failure reason.
7. Leave the page open or close it. The backend checkpoints after every batch, and the page reconnects to the same job after refresh, browser closure, or backend restart.
8. Review all variables using the Include, Deprioritize, and Exclude filters. The model reports confidence from `0.00` through `1.00`; anything below `0.80` gets one automatic revision. Valid results at or above `0.80` start in Include automatically.
9. Approve any Deprioritized variable you want to move into Include. Variables left in Deprioritize remain parked and do not block saving.
10. Save a new immutable context version. It records the feature-catalog version, confidence threshold, approval states, and excluded keys.
11. Press **Compile reviewed context**. Compilation runs as another durable job and deterministically turns only `auto_approved` or `human_approved` `include` rules into an organization-independent context contract. It does not recollect org data or make an AI call.
12. Press **Test curated context**. The Workbench fetches fresh data for the selected organization, applies the PII gate, and runs the exact Waypoint evolve prompt twice: once with the full scrubbed source context and once with the curated packet. It shows both idea sets, token and latency metrics, and a low-cost `MODEL_FAST` verdict with no more than three small suggested context changes. This is recommendation-only; it never sends outreach or writes to production systems.
13. Press **Promote to Waypoint** only after the context test. Promotion activates the approved Include rules and the selected feature-catalog version for everyday Waypoint. It also exposes a CSV download containing exactly `canonical_key`, `source_table`, and `cohort_aggregate_prompt` for the approved Include variables.
14. Give that CSV to Claude with the Snowflake MCP to create or revise the production query. Update n8n manually; the Workbench never edits it. Missing source lineage is written as `UNKNOWN`, never guessed from a query name.

Selecting an older feature version is rollback. Uploading a changed feature catalog shows exact added, removed, and changed counts. Unknown feature mappings are removed and reported as warnings.

Authoring uses low model effort, a 20,000-token generation cap per batch, and a 150,000-output-token cap for the full run. Each batch receives a compact feature index containing exact keys, product areas, and short descriptions instead of every CSV column. Variables returned without every required metadata field are requeued up to three times. A batch that reaches `max_tokens` is split and retried; it is never treated as complete.

Durable job state lives in ignored local storage at `services/api/.workbench/jobs.sqlite3`. It contains sanitized requests, variable metadata, progress, usage, warnings, and pruned results. It does not contain credentials, raw pre-PII payloads, or full authoring prompts/responses.

Promoted versions live as immutable ignored JSON bundles under `services/api/.workbench/promotions/`; `active.json` points to the version used by Waypoint. Re-promoting another saved context version changes the active pointer without changing the older bundle. A bundle contains no organization values or credentials.

## Data boundaries

- The browser never collects credentials.
- Raw provider values remain server-side until the app-side PII gate completes.
- Identity-variable values are removed. Business fields such as `FEATURE_VOIP_STATE` are not confused with geographic state.
- The authoring model receives variable keys, source query/path, observed type/state, and the verified feature catalog. It does not receive organization values.
- Runtime compilation attaches compact product cards only for feature keys referenced by approved included rules. Waypoint receives exact feature meaning without receiving all 275 catalog rows for every organization.
- Everyday Waypoint keeps only promoted canonical keys returned by the production n8n response. Missing promoted keys remain missing, nulls remain explicit nulls, and unapproved response columns are dropped.
- With no active promotion, Waypoint keeps the existing `org-context-v2` prompt context. Once a promotion is active, Waypoint uses only the promoted contract; promoted keys missing from the n8n result stay missing and the packet may be empty rather than silently reverting to the old broad context.
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

The only endpoint is `GET /api/context_layer/:organization_uuid`. It returns all related features for an organization in one response. In combined mode, the Workbench obtains `ORG_UUID` from the n8n result, performs one read-only GET, and compares every returned feature name against every exact key in the active feature catalog.

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
