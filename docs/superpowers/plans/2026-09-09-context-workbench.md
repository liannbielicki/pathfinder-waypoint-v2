# Context Layer Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local browser workbench that runs one Pro/org through live or fixture context retrieval and the existing V3 prompt path while exposing every context transformation, prompt, response, and cost metric.

**Architecture:** Add a dev-only workbench API to the existing Python service and a `/context-workbench` Next page. Keep traces in memory, use a direct read-only Context Layer client, reuse existing prompt/catalog/LLM primitives, and provide an in-memory usage recorder so the workbench needs no database.

**Tech Stack:** FastAPI, httpx, Pydantic, existing Anthropic-compatible gateway primitives, Next.js, React, TypeScript, Vitest, pytest.

---

## File map

- Create `services/api/src/waypoint/workbench.py`: request models, trace stages, fail-closed PII gate, direct Context Layer client, fixture loading, context layering, and product-card resolution.
- Create `services/api/src/waypoint/workbench_api.py`: local workbench FastAPI app/route with no production run, handoff, or database dependency.
- Modify `services/api/src/waypoint/llm.py`: allow the existing gateway call path to report usage to an in-memory recorder in addition to the production database sink.
- Create `services/api/tests/test_workbench.py`: unit tests for redaction, stage ordering, live-client contract, fixture replay, layering, product enrichment, and failure states.
- Create `services/api/tests/fixtures/context_layer_workbench.json`: fully redacted Context Layer fixture with at least one candidate-resolvable feature.
- Create `services/api/scripts/run_workbench.py`: one-command local launcher for the workbench API and frontend instructions.
- Create `apps/web/src/app/context-workbench/page.tsx`: workbench controls, timeline, comparison, and candidate inspector.
- Create `apps/web/src/components/WorkbenchTimeline.tsx`: expandable stage cards with summary/raw/diff/error/metrics views.
- Create `apps/web/src/components/WorkbenchRunForm.tsx`: identifier, mode, endpoint, key, model, prompt-policy, and enrichment controls.
- Create `apps/web/src/lib/workbench.ts`: typed API client and redacted trace types.
- Create `apps/web/src/components/WorkbenchTimeline.test.tsx` and `apps/web/src/components/WorkbenchRunForm.test.tsx`: focused UI behavior tests.
- Modify `apps/web/src/app/globals.css`: minimal accessible layout styles for the workbench.
- Modify `README.md`: local startup and security instructions.

## Task 1: Define the trace and boundary behavior

**Files:**
- Create: `services/api/src/waypoint/workbench.py`
- Test: `services/api/tests/test_workbench.py`
- Test fixture: `services/api/tests/fixtures/context_layer_workbench.json`

- [ ] **Step 1: Write failing tests for trace shape and secret redaction.**

  Test that a trace preserves the ordered stage names, that values matching API-key fields are removed from serialized output, and that raw context is marked separately from sanitized prompt-visible context.

- [ ] **Step 2: Run the focused tests and verify they fail for the missing workbench module.**

  Run: `uv run pytest tests/test_workbench.py -q`

  Expected: collection/import failure because `waypoint.workbench` does not yet exist.

- [ ] **Step 3: Implement the minimum Pydantic trace models and redaction helper.**

  Define `WorkbenchStage(name, status, summary, data, error, duration_ms, metrics)`, `WorkbenchTrace(stages, warnings, candidates)`, and `redact(value)` that recursively masks keys containing `key`, `token`, `secret`, `authorization`, `password`, or `credential`. Keep raw context available only through an explicit masked representation.

- [ ] **Step 4: Add the redacted fixture and rerun the focused tests.**

  Run: `uv run pytest tests/test_workbench.py -q`

  Expected: trace-order and redaction tests pass.

## Task 2: Add live Context Layer retrieval and fail-closed PII assembly

**Files:**
- Modify: `services/api/src/waypoint/workbench.py`
- Test: `services/api/tests/test_workbench.py`

- [ ] **Step 1: Write failing tests for the direct GET contract and explicit errors.**

  Test that the client sends `GET {base_url}/api/context_layer/{organization_uuid}` with `Authorization: Bearer <key>`, refuses redirects, returns JSON on HTTP 200, and reports 401, timeout, malformed JSON, and non-object payloads as visible stage failures without returning the key.

- [ ] **Step 2: Run the focused tests and verify the contract tests fail.**

  Run: `uv run pytest tests/test_workbench.py -k 'context_layer or live' -q`

  Expected: failures for the missing client and stage implementation.

- [ ] **Step 3: Implement the direct client and fixture/live source adapter.**

  Add `ContextLayerClient.fetch(org_uuid, base_url, api_key)` using the existing `httpx` dependency, `follow_redirects=False`, bounded connect/read timeouts, and no logging of headers. Add fixture loading that never makes network calls.

- [ ] **Step 4: Write failing tests for broad merge and proposed product enrichment.**

  Test that the broad context retains all non-sensitive source fields, normalized context records source/grain/freshness metadata, product index entries are present before generation, and candidate keys resolve to a product card after generation.

- [ ] **Step 5: Write failing PII-gate tests before implementation.**

  Test that names, last names, emails, phones, addresses, city/state/country/zip, organization/contact IDs, and free text containing identity values are removed; unknown fields fail closed; and the returned removal ledger contains only paths, categories, counts, and reasons—not original values.

- [ ] **Step 6: Implement the layering and PII functions.**

  Add `build_broad_context`, `scrub_pii`, `sanitize_context`, `build_product_index`, `resolve_product_cards`, and `build_proposed_context`. Use an explicit safe-field allowlist, denylisted field-name patterns, deterministic email/phone/address/identifier detection, and same-request identity-value matching. Preserve only a removal ledger and emit `pii_gate_failed` when ambiguity remains. Emit `needs_product_context` when a product key has no authoritative card.

- [ ] **Step 7: Rerun all backend workbench tests.**

  Run: `uv run pytest tests/test_workbench.py -q`

  Expected: all client, fixture, merge, sanitization, and enrichment tests pass.

## Task 3: Reuse the V3 prompt and metering paths in a local run endpoint

**Files:**
- Create: `services/api/src/waypoint/workbench_api.py`
- Modify: `services/api/src/waypoint/llm.py`
- Modify: `services/api/src/waypoint/workbench.py`
- Test: `services/api/tests/test_workbench.py`

- [ ] **Step 1: Write failing tests for baseline/proposed prompt rendering.**

  Test that a fixture run calls the existing `evolve_prompt` with the selected prompt version and channel/journey settings, produces separate baseline and proposed exact prompts, records model metadata and parse status, and never invokes production persistence or handoff dependencies.

- [ ] **Step 2: Implement a small in-memory usage sink behind the existing LLM gateway boundary.**

  Preserve the production SQL usage path. Add an optional usage-recorder callback/protocol; the workbench passes an in-memory recorder that captures model, stage, token counts, cache counts, and cost. Existing production callers continue using their database recorder.

- [ ] **Step 3: Implement the workbench run service and local API route.**

  Add `POST /api/context-workbench/run` with request fields for identifier, source mode, fixture name, Context Layer base URL/key, AI key/model, prompt version, context policy, enrichment mode, channel, and journey window. Execute input, retrieval, normalization, baseline/proposed prompt construction, optional model generation, JSON parsing, and candidate diagnostics in order. Return a redacted `WorkbenchTrace`.

- [ ] **Step 4: Add failure-path tests and run them.**

  Cover missing live credentials, fixture mode without credentials, invalid identifier, unauthorized Context Layer response, missing product card, malformed model JSON, and model failure.

  Run: `uv run pytest tests/test_workbench.py -q`

  Expected: all endpoint and failure tests pass.

## Task 4: Build the browser workbench

**Files:**
- Create: `apps/web/src/lib/workbench.ts`
- Create: `apps/web/src/app/context-workbench/page.tsx`
- Create: `apps/web/src/components/WorkbenchRunForm.tsx`
- Create: `apps/web/src/components/WorkbenchTimeline.tsx`
- Modify: `apps/web/src/app/globals.css`
- Test: `apps/web/src/components/WorkbenchRunForm.test.tsx`
- Test: `apps/web/src/components/WorkbenchTimeline.test.tsx`

- [ ] **Step 1: Write failing UI tests for run controls and stage expansion.**

  Test that the form submits identifier/mode/policy values, never stores credentials in local storage or the URL, and that the timeline renders stage status, raw/summary/diff tabs, duration, token/cost metrics, errors, and candidate diagnostics.

- [ ] **Step 2: Run focused UI tests and verify they fail for missing components.**

  Run: `pnpm test -- WorkbenchRunForm WorkbenchTimeline`

  Expected: module/component failures.

- [ ] **Step 3: Implement typed client, form, timeline, and candidate inspector.**

  Use native inputs and CSS only. Keep credentials in React state, send them in the request body, clear them after completion, and render masked values from the API trace. Support fixture mode, run, rerun, stage expansion, summary/raw/diff views, and baseline/proposed comparison.

- [ ] **Step 4: Add accessible styling and rerun UI tests.**

  Run: `pnpm test -- WorkbenchRunForm WorkbenchTimeline`

  Expected: focused UI tests pass with keyboard-accessible controls and visible error states.

## Task 5: Add one-command local startup and verification

**Files:**
- Create: `services/api/scripts/run_workbench.py`
- Modify: `README.md`
- Test: `services/api/tests/test_workbench.py`

- [ ] **Step 1: Add launcher and documented local flow.**

  Start the local API on a non-production development port and document the existing frontend command, fixture mode, live-mode key entry, and the explicit statement that the workbench has no send/handoff/write path.

- [ ] **Step 2: Add a smoke test for the fixture run.**

  Run a fixture request through the API and assert that the ordered trace contains retrieval, sanitization, prompt, response, parse, and candidate-diagnostics stages.

- [ ] **Step 3: Run backend and frontend verification.**

  Run: `uv run pytest -q`

  Run: `pnpm lint`

  Run: `pnpm test -- --run`

  Run: `pnpm build`

  Expected: existing V3 tests and new workbench tests pass; frontend lint/build succeed.

## Self-review checklist

- The design's live/fixture modes map to Tasks 2 and 3.
- The mandatory PII gate and visible removal ledger map to Tasks 1, 2, 3, and 4.
- Broad context plus candidate-specific product enrichment maps to Tasks 2 and 3.
- Existing V3 prompts and metering map to Task 3.
- Visual stage inspection and baseline/proposed comparison map to Task 4.
- Secret handling and no-send boundaries map to Tasks 1, 3, and 4.
- Promptfoo export is intentionally deferred until the trace format is stable.
- No unresolved implementation placeholders remain in this plan.
