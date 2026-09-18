# Workbench-Backed Staging Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Waypoint Staging fetch the unchanged Workbench n8n inventory and Context Layer API, then deterministically send only values and feature cards permitted by the active approved Postgres promotion.

**Architecture:** Add one composite `WorkbenchStagingContextClient` behind the existing `PipelineDeps.staging_context` interface. Reuse the existing Workbench source clients, promotion bundle, `OrgBrief`, pipeline retry behavior, and Standard client; keep source responses ephemeral and compile the two complete responses only after both return.

**Tech Stack:** Python 3.12, asyncio, httpx, Pydantic, FastAPI, SQLAlchemy/Postgres, pytest, React/TypeScript, Vitest.

**Commit rule:** The user requested one commit. Do not commit individual tasks; commit the reviewed spec, plan, implementation, tests, and docs together only after full verification.

---

## File map

- Create `services/api/src/waypoint/staging_context.py`: composite source fetch, exact allowlist matching, canonical compilation, safe diagnostics, and `OrgBrief` projection.
- Create `services/api/tests/test_staging_context.py`: source-contract, matching, failure, concurrency, and diagnostics tests.
- Modify `services/api/src/waypoint/context_promotion.py`: attach only feature cards referenced by retained rules.
- Modify `services/api/tests/test_context_promotion.py`: prove irrelevant catalog cards do not reach runtime.
- Modify `services/api/src/waypoint/workbench.py`: expose the existing Context Layer flattening helper for reuse.
- Modify `services/api/src/waypoint/settings.py`: declare the Workbench and Context Layer runtime variables while retaining the deprecated staging URL.
- Modify `services/api/src/waypoint/worker.py`: wire the composite Staging client to the active Postgres promotion only.
- Modify `services/api/src/waypoint/api.py`: compute Staging readiness from the new dependencies and reject non-numeric Staging IDs.
- Modify `services/api/tests/conftest.py`, `services/api/tests/test_settings.py`, and `services/api/tests/test_api.py`: cover runtime configuration and API validation.
- Modify `apps/web/src/components/RunStart.tsx` and `apps/web/src/components/RunStart.test.tsx`: switch identifier guidance and validate numeric Staging IDs.
- Modify `.env.example` and relevant deployment documentation only where existing text still claims runtime Staging uses `N8N_CONTEXT_URL_STAGING`.

### Task 1: Lock the compact promotion contract

- [ ] **Step 1: Write a failing feature-card test**

Update `services/api/tests/test_context_promotion.py` so `compile_promoted_context()` receives two retained rules but asserts `pc` contains only the `jobs` and `voip` cards referenced by those retained rules, excluding `unused` and `feature_only`.

```python
assert context["pc"] == {
    "jobs": {"a": "Jobs", "v": "Manage job workflows."},
    "voip": {"a": "Phones", "v": "Manage customer calls."},
}
```

- [ ] **Step 2: Verify the test fails for the current full-catalog behavior**

Run:

```bash
cd services/api && UV_CACHE_DIR=/tmp/pathfinder-uv-cache uv run pytest tests/test_context_promotion.py::test_runtime_compilation_keeps_promoted_values_and_referenced_feature_cards -q
```

Expected: failure showing unrelated feature cards in `pc`.

- [ ] **Step 3: Implement the smallest card filter**

In `compile_promoted_context()`, collect the exact related feature keys only while processing rules whose canonical values are present. Filter `_feature_cards()` to those keys before assigning `pc`; preserve `f` for explicit nulls because null is still a retained observation.

- [ ] **Step 4: Verify the focused test passes**

Run the same test and expect one pass.

### Task 2: Define the composite Staging client through failing tests

- [ ] **Step 1: Add source fakes and the successful-path test**

Create `services/api/tests/test_staging_context.py` with injected fakes implementing the existing Workbench source-client call signatures and an async promotion loader. The successful test must prove:

```python
batch = await client.fetch(["889901"])
assert snowflake.calls == ["889901"]
assert context_layer.calls == ["889901"]
assert batch.organizations[0].pro_id == "889901"
assert batch.organizations[0].curated_context == {
    "v": {"jobs_created_t28": 12, "trade": "plumbing"},
    "f": {"jobs_created_t28": ["jobs"]},
    "pc": {"jobs": {"a": "Jobs", "v": "Manage job workflows."}},
}
```

Use a Snowflake tall response containing approved and unapproved rows, plus a Context Layer response whose stable prefixed key is approved.

- [ ] **Step 2: Add exact-lineage, ambiguity, conflict, and null tests**

Tests must prove:

- exact `source_key` + `source_table` selects the correct duplicate;
- an `UNKNOWN` lineage rule matches only a unique source key;
- ambiguous duplicates are omitted;
- two different values targeting one canonical key are omitted;
- an explicit null enters `n`, while an absent rule does not;
- irrelevant feature cards are excluded.

- [ ] **Step 3: Add failure and diagnostics tests**

Tests must prove:

- missing active promotion raises `ContextUnavailable`;
- either upstream exception becomes a source-specific `ContextUnavailable` without response values;
- malformed Snowflake rows and malformed Context Layer objects fail clearly;
- zero approved matches raises `ContextUnavailable`;
- diagnostics log only promotion ID and counts, never raw values;
- two source calls for one organization overlap by coordinating test events;
- multiple organizations never exceed the configured semaphore limit.

- [ ] **Step 4: Run the new test file and verify RED**

Run:

```bash
cd services/api && UV_CACHE_DIR=/tmp/pathfinder-uv-cache uv run pytest tests/test_staging_context.py -q
```

Expected: collection/import failure because `waypoint.staging_context` does not exist.

### Task 3: Implement the minimal composite client

- [ ] **Step 1: Expose the existing Context Layer key normalizer**

Rename `_context_layer_values()` to `context_layer_values()` in `workbench.py` and update its internal callers. Do not create a second flattener.

- [ ] **Step 2: Implement `WorkbenchStagingContextClient`**

Create `staging_context.py` with:

```python
class WorkbenchStagingContextClient:
    def __init__(
        self,
        *,
        workbench_url: str,
        n8n_token: str,
        context_layer_url: str,
        context_layer_key: str,
        promotion_loader: Callable[[], Awaitable[dict[str, Any] | None]],
        max_concurrent: int = 3,
        snowflake: WorkbenchN8NClient | None = None,
        context_layer: ContextLayerClient | None = None,
    ) -> None: ...

    async def fetch(self, organization_ids: list[str]) -> OrgContextBatch: ...
```

Implementation requirements:

- reject any identifier for which `identifier.isdigit()` is false;
- load one active promotion per `fetch()` call and fail when absent;
- process organizations through one `asyncio.Semaphore(max_concurrent)`;
- use `asyncio.gather()` for the two source requests per organization;
- normalize Snowflake tall rows without canonicalizing first;
- reuse `context_layer_values()` for the API payload;
- apply exact source-key and lineage rules, with `UNKNOWN` unique-key fallback;
- preserve explicit nulls and omit absent, ambiguous, and conflicting values;
- fail when no canonical value, including null, was retained;
- call `compile_promoted_context()` only after deterministic matching;
- project retained string values onto matching `OrgBrief` fields;
- log only the promotion ID and counts.

- [ ] **Step 3: Run the focused tests and make them GREEN**

Run:

```bash
cd services/api && UV_CACHE_DIR=/tmp/pathfinder-uv-cache uv run pytest tests/test_staging_context.py tests/test_context_promotion.py -q
```

Expected: all tests pass.

### Task 4: Wire hosted configuration and enforce the API boundary

- [ ] **Step 1: Write failing settings and API tests**

Update tests to require optional declarations for:

```python
N8N_CONTEXT_URL_WORKBENCH: AnyHttpUrl | None
CONTEXT_LAYER_BASE_URL: AnyHttpUrl | None
CONTEXT_LAYER_API_KEY: SecretStr | None
```

API tests must prove Staging availability requires all three new optional values, ignores `N8N_CONTEXT_URL_STAGING`, and rejects `pro_ids=["pro_abc"]` in Staging while preserving `"889901"` as a string.

- [ ] **Step 2: Verify the focused tests fail**

Run:

```bash
cd services/api && UV_CACHE_DIR=/tmp/pathfinder-uv-cache uv run pytest tests/test_settings.py tests/test_api.py -q
```

Expected: failures show missing settings declarations and old readiness/validation behavior.

- [ ] **Step 3: Add settings and a single readiness helper**

Declare the three optional fields in `Settings`. Add a small shared predicate in `api.py` or `settings.py` so both `/api/runs` and `/api/fleet/settings` use the same rule. Extend the empty-string validator so blank Railway placeholders become `None`.

- [ ] **Step 4: Wire the worker without packaged fallback**

Replace the Staging `N8NContextClient` construction with `WorkbenchStagingContextClient`. Pass `PostgresWorkbenchStore.read_active_promotion` directly; do not compose it with `PromotionStore(PACKAGED_PROMOTION_ROOT)` in hosted runtime. Leave Standard client construction unchanged.

- [ ] **Step 5: Validate digit-only Staging IDs before enqueue**

In `create_run()`, reject any Staging ID that is not a non-empty digit-only string with a 422 message naming numeric organization IDs. Perform this before creating a run or jobs. Do not alter Standard identifier validation.

- [ ] **Step 6: Run focused backend tests**

Run the settings, API, staging-context, worker/pipeline, and promotion tests. Expected: all selected tests pass.

### Task 5: Make the Start-a-Run UI truthful

- [ ] **Step 1: Write failing component tests**

Add tests proving:

- Standard remains selected by default and displays `Pro IDs (one per line)`;
- selecting Staging changes the label/helper to numeric organization IDs;
- a non-numeric Staging value blocks submission and shows an accessible error;
- `889901` submits unchanged with `context_source: "staging"`;
- unavailable Staging remains disabled.

- [ ] **Step 2: Verify the tests fail**

Run:

```bash
cd apps/web && ./node_modules/.bin/vitest run src/components/RunStart.test.tsx
```

- [ ] **Step 3: Implement minimal conditional copy and validation**

Use the existing `contextSource`, `ids`, `error`, and form controls. Add no new component or state machine. Update the helper text so Staging says it uses the approved Workbench catalog over the full Workbench source plus Context Layer API; remove the obsolete compressed/PII-gate wording.

- [ ] **Step 4: Verify the component tests pass**

Run the same Vitest command and expect all tests to pass.

### Task 6: Align deployment documentation and verify everything

- [ ] **Step 1: Update only stale configuration documentation**

Keep all four variables documented, but mark `N8N_CONTEXT_URL_STAGING` deprecated and unused by runtime routing. State that hosted Staging requires:

```text
N8N_CONTEXT_URL_WORKBENCH
N8N_TOKEN
CONTEXT_LAYER_BASE_URL
CONTEXT_LAYER_API_KEY
```

Do not edit n8n workflow JSON or Snowflake SQL.

- [ ] **Step 2: Run backend verification**

```bash
cd services/api
UV_CACHE_DIR=/tmp/pathfinder-uv-cache uv run pytest -q
UV_CACHE_DIR=/tmp/pathfinder-uv-cache uv run ruff check .
UV_CACHE_DIR=/tmp/pathfinder-uv-cache uv run mypy src
```

- [ ] **Step 3: Run frontend verification**

```bash
cd apps/web
./node_modules/.bin/vitest run
./node_modules/.bin/eslint .
./node_modules/.bin/tsc --noEmit
./node_modules/.bin/next build
```

- [ ] **Step 4: Review the final diff against the spec**

Confirm Standard is unchanged, hosted Staging has no packaged fallback, no raw source value is persisted/logged, no second runtime PII classifier was added, and no n8n/SQL file changed.

- [ ] **Step 5: Create the single commit**

After every verification command succeeds:

```bash
git add .env.example docs/superpowers/specs/2026-09-18-workbench-backed-staging-context-design.md docs/superpowers/plans/2026-09-18-workbench-backed-staging-context.md services/api apps/web
git commit -m "feat: compile staging context from workbench sources"
```

Do not push without a separate user request.
