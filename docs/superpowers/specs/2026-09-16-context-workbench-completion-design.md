# Context Workbench Completion Design

## Goal

Finish the V4 Context Workbench as the place where Waypoint's context contract is built, refined, versioned, and tested. The Workbench begins with the complete experimental Snowflake/n8n result, enriches it with the replaceable feature-catalog CSV, resolves most metadata automatically, and asks the user to review no more than 25 important uncertain items before deterministic compilation.

## Product boundary

The Workbench reads the configured experimental n8n webhook and optional Context Layer API. It never edits Snowflake SQL, n8n workflows, production Waypoint state, Iterable, LCM, or outbound messages. Query changes remain user-controlled; each updated source result starts a new audit.

The workflow remains **start big, then narrow**:

1. Collect every PII-safe source row.
2. Preserve a row-level audit, including duplicate source rows.
3. Create one context rule per exact variable key.
4. Use AI to draft compact metadata and report its confidence.
5. Rework low-confidence drafts once.
6. Ask for human review only where uncertainty still matters.
7. Compile only approved rules into a compact runtime contract.

The Workbench does not infer population-wide freshness, missingness, reliability, or conflicts from one organization's values. A null or absent value is only an observation for that run.

## Durable job lifecycle

Authoring becomes a durable local background job instead of one long browser request. A small SQLite database under the ignored local Workbench state directory stores:

- the job ID, status, timestamps, and sanitized request;
- the audited inventory and source summaries;
- the selected feature-catalog version ID;
- completed metadata entries and pending variable keys;
- per-key attempt counts, model usage, warnings, and safe errors;
- the current exception queue and compiled result, when available.

The backend checkpoints after every successful or failed batch. It never stores credentials, raw pre-PII source payloads, or verbose model prompts. The browser starts a job, polls its status, and reconnects to the newest unfinished job after refresh or reopening. Restarting the backend resumes pending work without repeating completed variables.

Refreshing or reopening reconnects to the selected unfinished job. Starting a new run requires an explicit user action, creates a new job ID, and never deletes or replaces an earlier result.

## Source and audit contracts

The n8n source accepts `{organization_id}` and returns the existing `QUERY_NAME`, `VARIABLE_NAME`, `VALUE`, and optional `METADATA` rows. Its current four-minute read timeout remains bounded and produces source-specific errors.

The row audit retains all safe rows so duplicates remain visible. Metadata authoring deduplicates by exact `VARIABLE_NAME`, because one deterministic runtime rule must govern each key. The UI reports both source-row count and unique-rule count instead of treating their difference as unfinished work.

The optional Context Layer call remains independent. A Snowflake success is usable when Context Layer fails, and vice versa where identity requirements permit it. Source errors remain explicit and are never converted into missing facts.

## Feature-catalog contract

The supplied CSV schema is supported directly. `Display Name`, `Feature`, or `Feature Key` can provide the exact feature key. All other columns, including duplicate headings such as `Engaged`, are preserved under stable unique names. Blank feature keys are skipped; duplicate feature keys fail validation.

Each upload creates an immutable content-addressed feature-catalog version containing its filename, upload time, original CSV, normalized rows, and exact-key index. Selecting an older version is the rollback mechanism. A context-catalog version records the feature-catalog version that produced it.

The complete catalog is available during metadata authoring. To control tokens, each variable batch receives a compact feature index containing exact key, product area, and a bounded value statement rather than every CSV column.

## AI metadata and confidence

The model receives scrubbed variable identity only: exact key, source query/path, observed type, and observed present/null state. Organization values are never sent for authoring.

For every unique variable key, the model returns:

- `key`
- `canonical_key`
- `value_category`
- `related_features` using no more than three exact catalog keys
- `usefulness_rank` from 1 through 5
- `disposition` as `include`, `deprioritize`, or `exclude`
- `aggregate_prompt`, restricted to cohort-level statistics for rank 4 or 5
- `confidence` from `0.00` through `1.00`
- `uncertainty_reason`, which is required below `0.80` and omitted otherwise

Confidence is model-reported, as approved by the user. Deterministic code still validates structure, key identity, canonical-key collisions, allowed values, exact feature mappings, and aggregate-prompt policy. A draft cannot advance merely because it reports high confidence when deterministic validation fails.

Missing or invalid entries are requeued. Batches that reach output limits are split and requeued. Each key has a bounded attempt count, and completed keys are never repeated after a checkpoint.

## Revision and limited human review

Every draft below `0.80` receives one targeted AI revision using its original metadata, its uncertainty reason, the exact variable evidence, and the compact feature index. The revision must either produce a valid draft with a new confidence score or remain unresolved.

After revision:

- Valid entries at or above `0.80` become `auto_approved`.
- Entries below `0.80`, or entries with unresolved deterministic concerns, enter the exception pool.
- The exception pool is ordered by compilation impact: included entries first, then usefulness rank descending, confidence ascending, deterministic conflicts, and exact key as a stable tiebreaker.
- The UI presents at most the first 25 exceptions.
- The user can edit and approve or exclude each displayed exception.
- Unresolved exceptions beyond the first 25 are automatically excluded from compilation, remain visible in diagnostics, and are never silently accepted.

The UI does not ask the user to inspect the hundreds of high-confidence metadata prompts or responses. It shows counts, confidence, concise uncertainty reasons, and the limited exception queue.

## Approval and immutable versions

Approval states are explicit: `draft`, `auto_approved`, `review_required`, `human_approved`, and `excluded`. The UI never labels an automatic decision as human-reviewed.

The recoverable job snapshot is saved continuously. Saving an immutable context-catalog version remains a separate explicit action. The immutable version contains the approved rules, excluded-key record, source and feature-catalog version references, authoring instructions, thresholds, timestamps, and usage totals. Saving does not overwrite an older version.

Compilation becomes available only after all displayed exceptions are resolved and an immutable context version is saved. Compilation never calls an AI model.

## Deterministic runtime context

For each organization, deterministic code:

1. fetches and PII-scrubs the current source values;
2. applies only `auto_approved` and `human_approved` rules with `include` disposition;
3. preserves null as an observed state instead of a fact;
4. records exact related feature keys;
5. looks up only those feature keys in the approved feature-catalog version;
6. attaches compact feature cards before Waypoint generates ideas.

A compact feature card contains the exact key, product area when present, and a bounded value statement. The full 275-row catalog is never repeated for every organization, and Waypoint is never expected to infer product meaning from an unexplained key.

The compiled payload excludes authoring confidence, uncertainty reasons, human labels, prompts, and review history. It reports measured characters and bytes plus an explicit token estimate.

## UI

The page keeps the guided sequence **Connect -> Collect and audit -> Curate exceptions -> Compile**.

The live job panel shows source collection, unique variables, drafted variables, revision progress, remaining variables, output tokens, warnings, and last checkpoint time. Refreshing the page reconnects to the job. Failed jobs retain completed work and offer resume, not destructive restart.

The curation view defaults to the limited exception queue. High-confidence and excluded entries remain searchable under diagnostics but do not require review. Save and compile controls explain their exact remaining prerequisite and remain in a stable, readable action bar.

## Failure and security behavior

- Credentials remain server-side and never enter SQLite, browser storage, traces, or logs.
- Raw source values are never persisted before the PII gate.
- A process restart resumes from durable checkpoints.
- Source timeout, transport failure, invalid JSON, model parse failure, token exhaustion, and incomplete variables remain distinct errors.
- Partial model batches preserve valid entries and requeue only missing or invalid keys.
- No failed or uncertain result becomes runtime context by default.
- Existing local versions and yesterday's uncommitted V4 work are preserved.

## Testing and completion criteria

Backend tests must prove:

- row-level audit and unique-key authoring produce correct counts;
- durable jobs checkpoint and resume without repeating completed keys;
- credentials and raw pre-PII values are not persisted;
- invalid or missing model metadata is retried;
- low-confidence entries receive one revision;
- only valid entries at or above `0.80` auto-approve;
- exception ordering is deterministic and capped at 25;
- unresolved overflow is excluded;
- the supplied `Display Name` CSV is versioned and mapped by exact keys;
- runtime compilation attaches only relevant compact feature cards;
- compilation is deterministic and makes no model call.

Frontend tests must prove:

- an unfinished job reconnects after remount;
- progress and checkpoint state are visible;
- no more than 25 review items are presented;
- high-confidence entries do not require manual review;
- unresolved exceptions block immutable save and compile where applicable;
- saved versions survive reload;
- compile prerequisites and source failures are understandable.

Completion requires fresh backend and frontend test suites, type and lint checks, a production frontend build, restart-resume verification, and one live end-to-end local run using the configured `.env` without printing secrets. The final report must distinguish locally verified behavior from any production integration that was not exercised.
