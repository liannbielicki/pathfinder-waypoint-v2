# Per-Run Context Source Design

## Goal

Let an operator choose the existing Standard context workflow or the new compressed
Staging workflow when starting a Waypoint run, while keeping Standard behavior
unchanged and making the choice durable for every worker, retry, and resume.

## Workflow boundaries

The three n8n workflows have distinct responsibilities:

- `N8N_CONTEXT_URL` is the existing production Waypoint workflow. It remains the
  default and continues to produce the legacy broad context without applying a
  Workbench promotion.
- `N8N_CONTEXT_URL_STAGING` is the compressed workflow. Runs explicitly selecting
  Staging use this URL and the promoted context contract.
- `N8N_CONTEXT_URL_WORKBENCH` is the large experimental workflow used only by the
  Context Workbench for full-variable auditing.
- `N8N_TOKEN` authenticates all three workflows. No second token is introduced
  unless the workflows actually diverge in authentication later.

The Workbench never edits or calls the Standard workflow. The compressed Staging
workflow is also user-owned; this repository only selects it and validates its
response.

## Durable run selection

Add a non-null text `context_source` column to `runs` with a server default of
`standard`. The API contract accepts and returns the closed values `standard` and
`staging`; omitted values become `standard`. Existing rows therefore preserve
their current behavior without a data rewrite.

The Start a Run page presents a small Standard/Staging control, defaulted to
Standard. Staging is disabled when Railway has no `N8N_CONTEXT_URL_STAGING`, and
the API independently rejects a Staging run when the URL is unavailable. Retry
runs inherit the original run's `context_source`.

Persisting the choice on the run is required because API creation, worker
execution, retries, and resumes happen in different processes and at different
times. Browser state, process memory, and global environment switches are not
authoritative enough for paid asynchronous work.

## Worker routing and context behavior

The worker creates one long-lived client for Standard and, when configured, one
for Staging. The pipeline selects the client from the persisted run field before
fetching context. The source cannot change after run creation.

Standard parsing retains today's behavior and never reads a promoted context
bundle. Staging parsing reads the compressed n8n response, keeps only exact keys
approved by the deployed promotion, preserves explicit nulls, drops unexpected or
PII-bearing fields, and attaches the bundled compact feature catalog. A missing
Staging promotion fails closed instead of falling back to broad Standard context.

## PII-only curation policy

PII is the only reason the promotion pipeline may automatically discard source
information. The shared app-side PII gate runs immediately after either source
responds, before Workbench inventory construction, persistence, display, or any AI
call. Rows whose variable key is classified as PII are removed as complete rows,
not retained as empty metadata records. The same gate runs again on Staging n8n
responses before runtime context compilation, so an upstream query regression
cannot expose PII to Waypoint.

Canonical-key collisions and repeated source names are not exclusion reasons.
Exact duplicate rules may be represented once because this loses no information;
all distinct non-PII rules remain in the reviewed catalog and promotion. The
compressed n8n workflow must return the promoted canonical aliases. Runtime does
not guess from ambiguous generic source names such as `count`; missing canonical
aliases remain missing rather than being copied into multiple facts.

The feature catalog passes through a value-level PII gate before it is included in
AI context. Product terms such as email or address features remain valid product
metadata; only detected personal values are removed.

The n8n response continues to self-report `audience_query_version`; Waypoint stamps
that version once exactly as it does today. Run status also exposes
`context_source`, so an operator can see both which endpoint class was selected
and which query version actually answered.

## Initial Staging promotion

The initial deployable Staging artifact is generated from completed evaluation job
`5b7eba5c-97df-4088-b043-c2676089200d`:

- context version `context-1789581748920-43479f5d-2f15-4bf0-89dd-fbf7e5bafe54`;
- 480 reviewed rules, of which 287 are approved Include decisions before the PII
  gate;
- feature version `features-649a8d942fd62ed9` with 181 features.

The artifact contains every distinct approved non-PII rule and safe feature
definitions, never organization values or credentials. Its final rule count is
reported as `approved -> PII removed -> duplicate representations merged -> retained`
rather than asserted to equal the pre-gate 287 decisions. The initial result is
`287 -> 64 -> 4 -> 219`; duplicate metadata is unioned, so no distinct source
information is discarded. It lives under `services/api/data/context-promotions/` so
the Docker image delivers the same immutable file to every API and worker. An
`active.json` pointer selects the current Staging artifact. Future changes follow
Workbench review and evaluation, artifact export, commit, and deployment.

## Environment and deployment

`N8N_CONTEXT_URL_STAGING` is optional at application startup so the code can deploy
before the new workflow is ready. Standard runs remain available when it is
missing. `N8N_CONTEXT_URL_WORKBENCH` is separate from runtime Settings and is read
only by the Workbench. Documentation and `.env.example` use the shared
`N8N_TOKEN` for all three.

This design does not expose the local Workbench API on Railway. That is a separate
deployment/authentication subproject: the current Workbench has local SQLite job
state and localhost-only CORS, while the production API has authenticated routes
and Postgres-backed state. Keeping that work separate prevents a context-source
toggle from accidentally publishing an unauthenticated authoring surface.

## Failure behavior

- Standard selected: use `N8N_CONTEXT_URL`; no promotion is loaded.
- Staging selected but URL missing: reject run creation with a clear 422 error.
- Staging selected but artifact missing or invalid: fail the Pro context fetch and
  use the existing bounded retry/degraded-run behavior; never use Standard.
- Selected n8n workflow times out or returns a contract violation: retain current
  retry and failure behavior, with the source label included in diagnostics.
- A deployment rollback restores both code and the previously committed artifact.

## Verification

Tests must prove:

- the migration gives existing and omitted-source runs `standard`;
- API validation accepts only `standard` or `staging` and exposes the selection;
- Staging creation is rejected when its URL is unavailable;
- the UI defaults to Standard and submits Staging only when selected;
- retry preserves the original selection;
- concurrent Standard and Staging jobs route to different clients;
- Standard does not load a promotion;
- Staging preserves every distinct approved non-PII rule and all safe feature
  cards, with transparent PII-removal counts;
- PII variable rows never enter the Workbench inventory or authoring prompt;
- canonical collisions remain catalog information and ambiguous source-key
  fallback never fabricates duplicate facts;
- missing keys and nulls remain missing/null rather than becoming facts;
- the existing backend, frontend, lint, type, migration, and production-build
  checks remain green.
