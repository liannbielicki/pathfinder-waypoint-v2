# Context Workbench Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend yesterday's V4 Workbench so long-running context jobs survive disconnects and restarts, feature-catalog context reaches Waypoint compactly, and the user reviews no more than 25 unresolved low-confidence rules.

**Architecture:** Preserve the current source, audit, authoring, versioning, and compile flow. Add a standard-library SQLite job store and thin FastAPI job runner around the existing orchestration, then make authoring resumable through per-batch checkpoints. Keep deterministic validation in Python, model-reported confidence in the draft contract, and browser polling/reconnection in the existing Workbench UI.

**Tech Stack:** Python 3.14, standard-library `sqlite3`, FastAPI, Pydantic, httpx, pytest, Next.js 16, React 19, TypeScript, Vitest, Testing Library, browser localStorage.

**Commit policy:** Do not commit or push. The user requires explicit approval first; each task ends with tests and diff inspection instead.

---

### Task 1: Lock confidence, exception, and compact feature-card contracts

**Files:**
- Modify: `services/api/tests/test_workbench.py`
- Modify: `services/api/src/waypoint/workbench.py`

- [ ] **Step 1: Add failing deterministic tests**

Add focused tests proving:

Define small test-only `catalog_entry` and `approved_entry` factories immediately above these tests; each factory returns the complete dictionary required by `validate_catalog_entries`, and `approved_entry` sets `approval_status="auto_approved"`.

```python
def test_validate_catalog_entries_preserves_model_confidence_and_sets_approval():
    validated, warnings = validate_catalog_entries(
        [{
            "key": "SMS_SENT",
            "canonical_key": "sms_sent",
            "value_category": "communication",
            "related_features": ["sms_number"],
            "usefulness_rank": 5,
            "disposition": "include",
            "aggregate_prompt": None,
            "confidence": 0.91,
            "uncertainty_reason": None,
        }],
        {"sms_number"},
    )
    assert warnings == []
    assert validated[0]["confidence"] == 0.91
    assert validated[0]["approval_status"] == "auto_approved"


def test_prioritize_review_exceptions_caps_at_25_and_excludes_overflow():
    entries = [catalog_entry(key=f"KEY_{index}", confidence=0.5) for index in range(30)]
    selected, output = prioritize_review_exceptions(entries, limit=25)
    assert len(selected) == 25
    assert sum(item["approval_status"] == "excluded" for item in output) == 5


def test_compile_context_includes_only_approved_rules_and_relevant_feature_cards():
    compiled = compile_context(
        sources={"snowflake": {"rows": [{"VARIABLE_NAME": "SMS_SENT", "VALUE": 0}]}},
        entries=[approved_entry("SMS_SENT", related_features=["sms_number"])],
        feature_catalog_entries=[
            {"feature": "sms_number", "Product Area": "SMS", "Value Statement": "Customer texting number"},
            {"feature": "jobs", "Product Area": "Jobs", "Value Statement": "Job management"},
        ],
        feature_catalog_version_id="features-v1",
        context_catalog_version_id="context-v1",
    )
    assert compiled["context"]["pc"] == {
        "sms_number": {"a": "SMS", "v": "Customer texting number"}
    }
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
cd services/api
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m pytest -p no:cacheprovider tests/test_workbench.py -q
```

Expected: failures because confidence, `approval_status`, exception prioritization, and feature-card compilation do not yet exist.

- [ ] **Step 3: Implement the minimum deterministic contract**

In `workbench.py`:

- extend `validate_catalog_entries` to normalize `confidence` to `0.0..1.0`, preserve a concise `uncertainty_reason`, and assign `auto_approved` only when confidence is at least `0.80` and deterministic validation passes;
- preserve legacy `review_status="reviewed"` as human-approved when loading yesterday's versions;
- add `prioritize_review_exceptions(entries, limit=25)` with stable ordering: included first, usefulness descending, confidence ascending, conflict flag, exact key;
- mark exception overflow `excluded` with a machine reason;
- extend `compile_context` to accept feature-catalog entries, include only `auto_approved` and `human_approved` rules, and attach compact cards only for referenced exact keys;
- keep confidence, uncertainty, prompts, and approval history out of compiled runtime context.

- [ ] **Step 4: Run focused tests and inspect the diff**

Run the Task 1 pytest command again. Expected: PASS. Run `git diff --check` and inspect only the two Task 1 files. Do not commit.

### Task 2: Make authoring resumable and revise low-confidence entries once

**Files:**
- Modify: `services/api/tests/test_workbench.py`
- Modify: `services/api/src/waypoint/workbench_api.py`
- Modify: `services/api/src/waypoint/workbench.py`

- [ ] **Step 1: Add failing orchestration tests**

Add async tests proving:

Define test-only `authoring_request()` and `complete_entry(key)` factories in `test_workbench.py`. The request uses the existing mocked Context Layer source pattern; the entry contains every required metadata field, `confidence=0.9`, and `approval_status="auto_approved"`.

```python
async def test_authoring_reports_confidence_and_revises_low_confidence_once(monkeypatch):
    # First response is valid but confidence 0.60; revision returns 0.88.
    result = await execute_run(authoring_request(), checkpoint=checkpoints.append)
    entry = result["outputs"]["authoring"]["draft"][0]
    assert entry["confidence"] == 0.88
    assert entry["approval_status"] == "auto_approved"
    assert result["outputs"]["authoring"]["revision_attempted"] == 1


async def test_resume_does_not_redraft_checkpointed_keys(monkeypatch):
    result = await execute_run(
        authoring_request(),
        resume_state={"entries": [complete_entry("DONE")], "revised_keys": []},
        checkpoint=checkpoints.append,
    )
    assert "DONE" not in model_requested_keys
```

Also prove batch checkpoints contain only sanitized inventory and metadata, partial batches retain complete entries, invalid entries requeue, and revision happens no more than once per key.

- [ ] **Step 2: Run the focused tests and verify RED**

Run the Task 1 pytest command. Expected: new orchestration tests fail because `execute_run` has no resume/checkpoint contract and the model schema has no confidence fields.

- [ ] **Step 3: Add the resumable authoring state**

Update `execute_run` with internal-only keyword arguments:

```python
async def execute_run(
    body: WorkbenchRunRequest,
    *,
    resume_state: Mapping[str, Any] | None = None,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
```

Reuse the existing pending-batch loop. Seed completed entries and revised keys from `resume_state`; deduplicate the audit by exact key before batching; checkpoint after every draft, split, retry, revision, and terminal failure. Add `confidence` and `uncertainty_reason` to the requested model fields and validation projection.

After initial drafting, batch only entries below `0.80` that have not already been revised. Give each one targeted revision evidence, validate the response, then call `prioritize_review_exceptions`. Preserve bounded token and attempt budgets. Do not send organization values to either model pass.

- [ ] **Step 4: Run focused tests and inspect the diff**

Run the Task 1 pytest command and `git diff --check`. Expected: PASS with no whitespace errors. Do not commit.

### Task 3: Add the durable SQLite job store and API

**Files:**
- Modify: `.gitignore`
- Create: `services/api/src/waypoint/workbench_jobs.py`
- Create: `services/api/tests/test_workbench_jobs.py`
- Modify: `services/api/src/waypoint/workbench_api.py`

- [ ] **Step 1: Add failing job-store and API tests**

Test a temporary SQLite path and prove:

Define `complete_entry(key)` and sanitized request fixtures in `test_workbench_jobs.py`; the latter deliberately includes secret override fields so the persistence assertion exercises their removal.

```python
def test_job_store_checkpoints_and_reopens(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = WorkbenchJobStore(path)
    job = store.create(sanitized_request())
    store.checkpoint(job.id, {"entries": [complete_entry("DONE")]})
    reopened = WorkbenchJobStore(path).get(job.id)
    assert reopened.state["entries"][0]["key"] == "DONE"


def test_job_store_never_persists_secret_request_fields(tmp_path):
    job = WorkbenchJobStore(tmp_path / "jobs.sqlite3").create(
        request_with_secret_overrides()
    )
    assert "api_key" not in json.dumps(job.request)
    assert "token" not in json.dumps(job.request)
```

Add TestClient tests for `POST /api/context-workbench/jobs`, `GET /api/context-workbench/jobs/{job_id}`, `POST /api/context-workbench/jobs/{job_id}/resume`, and latest unfinished-job lookup. Prove a completed result is pruned of prompt/response stage bodies before persistence.

- [ ] **Step 2: Run the new test file and verify RED**

Run:

```bash
cd services/api
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m pytest -p no:cacheprovider tests/test_workbench_jobs.py -q
```

Expected: import failure because `workbench_jobs.py` does not exist.

- [ ] **Step 3: Implement the smallest durable store**

Use standard-library `sqlite3`, one `jobs` table, JSON columns, and an injected database path. The production default is `services/api/.workbench/jobs.sqlite3`; add `services/api/.workbench/` to `.gitignore`. Use atomic transactions and a process-local lock around writes. Store status values `queued`, `running`, `needs_review`, `completed`, and `failed`.

The API runner must:

- create and return a job immediately;
- run `execute_run` in an in-process async task;
- pass checkpoint state into the store after every batch;
- reconstruct `WorkbenchRunRequest` from sanitized persisted input;
- resume `queued` or `running` jobs on FastAPI startup;
- make resume idempotent while a process-local task is already active;
- persist only pruned safe results and errors.

Do not add Celery, Redis, SQLAlchemy models, migrations, or a second service.

- [ ] **Step 4: Run job tests, Workbench tests, and inspect the diff**

Run both backend test files, Ruff on the changed Python files, strict mypy on `src/waypoint`, and `git diff --check`. Do not commit.

### Task 4: Add browser job polling, reconnection, and recoverable state

**Files:**
- Modify: `apps/web/src/lib/workbench.ts`
- Modify: `apps/web/src/lib/workbench.test.ts`
- Modify: `apps/web/src/app/context-workbench/page.tsx`
- Modify: `apps/web/src/components/WorkbenchRunForm.tsx`
- Modify: `apps/web/src/components/WorkbenchRunForm.test.tsx`

- [ ] **Step 1: Add failing client and component tests**

Prove the client can start, poll, resume, and reconnect to a stored job ID. In component tests, remount the page with `waypoint-context-workbench-active-job` in localStorage and assert that progress/result restoration occurs without starting another run.

Use fake timers for polling; assert polling stops on `needs_review`, `completed`, or `failed`. Assert a backend restart or temporary network error retains the job ID and offers reconnect instead of clearing the view.

- [ ] **Step 2: Run focused frontend tests and verify RED**

Run:

```bash
cd apps/web
./node_modules/.bin/vitest run src/lib/workbench.test.ts src/components/WorkbenchRunForm.test.tsx
```

Expected: failures because job client functions and reconnection UI do not exist.

- [ ] **Step 3: Implement minimal polling and reconnection**

Add typed helpers `startWorkbenchJob`, `getWorkbenchJob`, and `resumeWorkbenchJob` to `workbench.ts`. Keep the active job ID in one localStorage key. Poll every two seconds while work is active and immediately render persisted progress after remount.

Keep job ownership in the page/form flow rather than introducing a state library. Preserve the existing environment status, source choices, catalog upload, and prior-version selection. A new-run action creates a new ID without deleting the prior job.

- [ ] **Step 4: Run focused tests and inspect the diff**

Run the focused Vitest command, TypeScript `--noEmit`, focused ESLint, and `git diff --check`. Do not commit.

### Task 5: Replace full metadata review with the 25-item exception queue

**Files:**
- Modify: `apps/web/src/components/AuthoringCatalog.tsx`
- Modify: `apps/web/src/components/AuthoringCatalog.test.tsx`
- Modify: `apps/web/src/app/globals.css`
- Modify: `apps/web/src/lib/catalogVersions.ts`
- Modify: `apps/web/src/lib/catalogVersions.test.ts`

- [ ] **Step 1: Add failing exception-queue tests**

Render a trace with 100 auto-approved rules and 30 low-confidence exceptions. Assert:

- only 25 review cards appear;
- auto-approved rules are summarized, not presented for manual review;
- confidence and concise uncertainty reason are visible on exceptions;
- the user can mark each exception human-approved or excluded;
- save remains disabled until the displayed exception queue is resolved;
- immutable versions preserve approval state, confidence threshold, feature-catalog version, and excluded overflow;
- compile includes auto-approved and human-approved rules without a bulk “approve everything” action.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
cd apps/web
./node_modules/.bin/vitest run src/components/AuthoringCatalog.test.tsx src/lib/catalogVersions.test.ts
```

Expected: failures because the current component renders every draft and requires broad manual approval.

- [ ] **Step 3: Implement the exception-first UI**

Default the curation view to `review_required` entries only. Keep auto-approved and excluded rules under a collapsed diagnostics/search section. Remove the broad approve-all action. Add explicit Approve and Exclude controls to each exception and show a `resolved / total` counter capped at 25.

Save an immutable version only after displayed exceptions are resolved. Compile through the durable job client, not the old long direct fetch. Keep the action bar compact and responsive.

- [ ] **Step 4: Run focused tests and inspect the diff**

Run the focused Vitest command, TypeScript, ESLint, and `git diff --check`. Do not commit.

### Task 6: Verify restart recovery and the complete local pipeline

**Files:**
- Modify: `docs/context-workbench-partner-handoff.md`
- Modify: `.planning/2026-09-15-context-workbench-rebuild/progress.md`

- [ ] **Step 1: Update the operator handoff**

Document the durable V4 path, unchanged `.env` location, start commands, job recovery behavior, feature-catalog version flow, confidence threshold, 25-item review limit, immutable-save rule, and compile/runtime feature-card contract. Do not include any secret value.

- [ ] **Step 2: Run complete backend verification**

Run:

```bash
cd services/api
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m pytest -p no:cacheprovider -q
.venv/bin/ruff check src tests
PYTHONPATH=src .venv/bin/mypy src/waypoint
```

Expected: zero failures and exit code 0 for each command.

- [ ] **Step 3: Run complete frontend verification**

Run:

```bash
cd apps/web
./node_modules/.bin/vitest run
./node_modules/.bin/eslint src
./node_modules/.bin/tsc --noEmit
./node_modules/.bin/next build
```

Expected: zero test failures, zero lint/type errors, and a successful production build.

- [ ] **Step 4: Verify durable restart behavior locally**

Start a deterministic test job against a stubbed model/source or a purpose-built integration fixture, wait for at least one checkpoint, stop and restart the backend, and prove the same job ID resumes without repeating the checkpointed key. Inspect the SQLite record only for field names/status/counts; never print stored organization values or secrets.

- [ ] **Step 5: Run one authorized live local pipeline**

Using the existing ignored `services/api/.env`, run the configured n8n source and AI authoring through the UI or job API. Confirm source count, unique-rule count, revision count, review queue size, saved version, and deterministic compile output. Do not print secret values or raw organization data. If the external run requires material token spend not already authorized for this implementation, stop and ask before starting it.

- [ ] **Step 6: Perform the post-edit audit**

Record:

```text
Git state checked:
Diff inspected:
Acceptance check satisfied:
Verification evidence:
10/10 gate stayed green:
```

Report any external integration gap separately from local correctness. Do not commit or push.
