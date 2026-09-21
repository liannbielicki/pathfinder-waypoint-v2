# Context Window Curation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply one consistent prompt-only policy that prioritizes T28 and removes redundant metric variants from runtime-approved context.

**Architecture:** Define the policy once in the existing Workbench authoring module and interpolate it into all model prompts that can create or revise catalog metadata. Preserve the existing model response schema and human-review workflow.

**Tech Stack:** Python, FastAPI service code, pytest

---

### Task 1: Lock the policy into every authoring prompt

**Files:**
- Modify: `services/api/src/waypoint/workbench_api.py`
- Test: `services/api/tests/test_workbench.py`

- [x] **Step 1: Write failing prompt-content tests**

Add tests that capture the initial batch, single-variable retry, and confidence-revision prompts and assert each contains rules for T28 preference, T1/T7 exclusion, exceptional T90 retention, and count-versus-amount family comparison.

- [x] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
cd services/api
UV_CACHE_DIR=/tmp/waypoint-uv-cache uv run pytest tests/test_workbench.py -q
```

Expected: the new assertions fail because the shared policy is not yet present.

- [x] **Step 3: Add the shared policy and reuse it in all prompt paths**

Define `_AUTHORING_SELECTION_POLICY` next to the existing authoring constants. Interpolate it into the initial authoring prompt, malformed-response single-variable retry prompt, and low-confidence revision prompt. Do not add deterministic filtering, new metadata fields, or changes to catalog validation.

- [x] **Step 4: Run focused and full verification**

Run:

```bash
cd services/api
UV_CACHE_DIR=/tmp/waypoint-uv-cache uv run ruff check .
UV_CACHE_DIR=/tmp/waypoint-uv-cache uv run mypy src
UV_CACHE_DIR=/tmp/waypoint-uv-cache uv run pytest -q
```

Then run the existing web lint, unit tests, and production build because generated contracts and the shared branch must remain releasable.

- [x] **Step 5: Review, commit, and push**

Inspect the final diff for prompt-only scope, commit the design, plan, implementation, and tests together, then push `V4-Improvements` to `origin` as explicitly requested.
