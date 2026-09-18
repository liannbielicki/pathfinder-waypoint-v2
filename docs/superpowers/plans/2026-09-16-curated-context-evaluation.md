# Curated Context Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one durable Workbench action that compares Waypoint ideas generated with raw scrubbed context against ideas generated with the curated context packet, then asks a cheap model for a concise verdict.

**Architecture:** Extend the existing Workbench job request with an `evaluate` mode and reuse its source collection, PII gate, prompt builder, model runner, and job persistence. Add the action and compact results to `AuthoringCatalog` so the user can test immediately after compiling without another form or page.

**Tech Stack:** FastAPI, Pydantic, Anthropic SDK, React, TypeScript, Vitest, pytest.

---

### Task 1: Backend evaluation orchestration

**Files:**
- Modify: `services/api/src/waypoint/workbench_api.py`
- Modify: `services/api/src/waypoint/workbench.py`
- Test: `services/api/tests/test_workbench.py`

- [x] Write a failing API test that submits `workbench_mode="evaluate"`, stubs source and model calls, and asserts two generation calls use the Waypoint prompt contract with different baseline and curated fenced contexts.

```python
request = WorkbenchRunRequest(
    identifier="889901",
    workbench_mode="evaluate",
    catalog_override=[approved_rule],
    feature_catalog_entries=[verified_feature],
)
result = await execute_run(request)
assert result["outputs"]["evaluation"]["baseline"]["prompt"] != result["outputs"]["evaluation"]["curated"]["prompt"]
assert "You are running one round of an evolutionary search" in result["outputs"]["evaluation"]["baseline"]["prompt"]
```
- [x] Run the focused pytest test and confirm it fails because `evaluate` is not an allowed mode.
- [x] Add `evaluate` to the request model, compile the curated organization packet with `compile_context`, run baseline and curated generation with the same parameters, and return each prompt, candidates, context, and measured metrics.
- [x] Run the focused test and confirm it passes.

### Task 2: Cheap comparison judge

**Files:**
- Modify: `services/api/src/waypoint/workbench_api.py`
- Modify: `services/api/src/waypoint/workbench.py`
- Test: `services/api/tests/test_workbench.py`

- [x] Extend the failing backend test to expect a third low-effort call using `MODEL_FAST` and strict verdict JSON: `winner`, `reason`, and at most three `suggested_changes`.

```python
assert result["outputs"]["evaluation"]["judge"] == {
    "winner": "curated",
    "reason": "More grounded recommendations.",
    "suggested_changes": ["Keep the strongest usage signals first."],
}
assert model_calls[-1]["model"] == "claude-haiku-4-5"
assert model_calls[-1]["effort"] == "low"
```
- [x] Run the focused test and confirm the judge assertion fails.
- [x] Add the smallest judge prompt/parser implementation, preserving explicit failure when the response is invalid.
- [x] Run focused backend tests and confirm they pass.

### Task 3: One-button UI and compact results

**Files:**
- Modify: `apps/web/src/components/AuthoringCatalog.tsx`
- Test: `apps/web/src/components/AuthoringCatalog.test.tsx`
- Modify: `apps/web/src/app/globals.css`

- [x] Write a failing component test asserting **Test curated context** appears after compile, starts one evaluate job, disables while running/completed, and shows baseline ideas, curated ideas, token/latency metrics, and the judge verdict.

```tsx
expect(await screen.findByRole("button", { name: /test curated context/i })).toBeEnabled();
fireEvent.click(screen.getByRole("button", { name: /test curated context/i }));
expect(await screen.findByText(/curated performed better/i)).toBeInTheDocument();
expect(screen.getByRole("heading", { name: /baseline ideas/i })).toBeInTheDocument();
expect(screen.getByRole("heading", { name: /curated ideas/i })).toBeInTheDocument();
```
- [x] Run the focused Vitest test and confirm it fails on the missing action.
- [x] Implement the button and a compact comparison section; keep exact contexts and prompts collapsed under native `details` elements.
- [x] Run the focused component test and confirm it passes.

### Task 4: Verification and live smoke test

**Files:**
- Modify: `docs/context-workbench-partner-handoff.md`

- [x] Update the handoff with the single-button evaluation workflow and its no-send boundary.
- [x] Run the complete backend pytest, Ruff, and mypy checks.
- [x] Run the complete frontend Vitest, ESLint, TypeScript, and Next build checks.
- [x] Restart the local backend if required, verify the compiled state remains visible, and confirm the new button is present without starting a paid test automatically.
