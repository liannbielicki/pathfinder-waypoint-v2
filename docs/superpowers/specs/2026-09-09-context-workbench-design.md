# Context Layer Workbench Design

Date: 2026-09-09
Status: Approved conversationally; awaiting written-spec review
Scope: Local V3 development tooling only

## Goal

Build a local browser workbench for testing and iterating on Waypoint's Context
Layer integration. A user can run one Pro or organization through the context
retrieval and LLM generation path, inspect every transformation, compare the
current V3 behavior with the proposed layered-context behavior, and see the
resulting prompt, response, latency, token usage, cost, and failures.

The workbench is an inspection and evaluation surface. It does not become a
production send path and does not perform n8n writes, Iterable writes, LCM
handoffs, or autonomous messaging.

## Product decisions

- The first unit of work is one Pro or organization per run.
- The primary interface is a local browser page at `/context-workbench`.
- Live mode uses a read-only Context Layer GET and a user-entered API key.
- Fixture mode replays local redacted JSON without external calls.
- The workbench reuses V3's existing prompt contracts, product catalog logic,
  and LLM gateway behavior instead of copying them into a parallel system.
- Context starts broad. Narrowing happens at the candidate/product decision
  boundary, not before the first opportunity pass.
- PII removal is a mandatory, fail-closed stage before any model call. The raw
  Context Layer payload is never sent to Anthropic.
- Promptfoo is a later batch-evaluation companion. It is not a second runtime
  context assembler.

## Architecture

The workbench consists of a Next.js page in `apps/web` and local-only API
endpoints plus an in-memory execution trace in `services/api`. The first
version requires no database. Each run is request-scoped and is discarded when
the request completes unless the user explicitly downloads a redacted trace.

The browser sends entered credentials only to localhost. The API uses them for
that run and never logs, returns, persists, or places them in URLs. Sensitive
context is masked by default in the UI.

The Context Layer client is a direct read-only GET client, separate from the
existing n8n context client. Optional Waypoint/n8n fixture context can be used
for comparison, but the live Context Layer payload remains visible as its own
source and stage.

## Execution trace

Every run exposes these ordered stages:

```text
input
  -> identifier resolution
  -> raw Context Layer response
  -> optional Waypoint SQL/n8n context
  -> merged broad context
  -> normalized and sanitized context
  -> PII gate and removal ledger
  -> current V3 product context
  -> proposed candidate-specific product enrichment
  -> exact generation prompt
  -> model response
  -> parsed candidates
  -> candidate diagnostics
```

Each stage records status, duration, summary, raw/redacted representation,
errors, and before/after diffs where applicable. Model stages also record model,
prompt version, input/output tokens, estimated cost, parse status, and request
metadata without secrets.

The PII stage is special: it may inspect raw values in process memory, but the
trace only exposes field paths, categories, counts, and redacted placeholders.
Original PII values are never returned to the browser, written to a fixture, or
included in a prompt.

## Context adaptation paths

The workbench runs a baseline and proposed path against the same input when
requested.

### Baseline

- Capture the full raw Context Layer response.
- Normalize it into the current Waypoint shape.
- Use the existing `evolve_prompt` or `generator_prompt` behavior.
- Show the existing V3 feature-catalog context exactly as supplied today.

### Proposed layered context

1. Retain the broad org context after normalization.
2. Run the mandatory PII gate. Use a conservative allowlist plus deterministic
   key, pattern, and identity-value detection to remove names, last names,
   emails, phones, addresses, city/state/country/zip, organization and contact
   identifiers, and free text that contains them. Unknown or ambiguous fields
   fail closed.
3. Add a compact product index before generation containing stable product key,
   one-line capability, broad use case, and eligibility signal.
4. Generate candidates with the existing V3 prompt contract.
5. Resolve each candidate's product or mechanism to an authoritative product
   card.
6. Attach the full product card before critic, ranking, persona, or final
   recommendation steps.
7. Display grounding, eligibility, missing-context, and unsupported-claim
   diagnostics for each candidate.

The product registry is authoritative for what a product does. The model may
propose HCP AI as a candidate, but it may not invent HCP AI's capabilities from
the product name or a generic feature label.

## Product-card contract

The expanded product card should support:

- canonical product name and stable key;
- actual capabilities and problem solved;
- best-fit Pro profiles;
- prerequisites and eligibility;
- related or competing features;
- adoption signals;
- common objections and limitations;
- expected business mechanism;
- supported channels and CTA types;
- prohibited or unsupported claims;
- owner, source, version, and effective date.

If product definition or eligibility is incomplete, the candidate resolves to a
visible `needs_product_context` or abstention state rather than a guessed
recommendation.

## Browser experience

Run controls include identifier and type, live/fixture mode, Context Layer base
URL and key, AI provider/model key and model, prompt version, current/proposed
context policy, product enrichment mode, and run/rerun controls.

The main view is a step timeline with expandable cards for summary, raw JSON,
diffs, inclusion/omission reasons, duration, tokens, cost, and errors. A
side-by-side comparison shows current V3 versus proposed context and exact
prompts.

The candidate inspector shows triggering org facts, resolved product key,
product-card fields used, fit evidence, missing/conflicting information,
unsupported-claim warnings, and product-comprehension-gate outcome.

## Security and failure behavior

- Context Layer access is read-only.
- No LCM handoff, Iterable access, n8n write, or outbound send is reachable.
- Keys are request-scoped, memory-only, and absent from logs, traces, URLs,
  browser storage, and downloaded artifacts.
- Raw context is never sent to the model. The trace distinguishes raw capture,
  PII findings, scrubbed context, and prompt-visible data; raw values remain
  masked in the browser.
- PII scrubbing is deterministic and fail-closed. It uses field allowlisting,
  key denylisting, email/phone/address/identifier patterns, and matching against
  identity values discovered during the same request. Fuzzy matching may flag a
  value for removal, but it is never the sole safety control.
- Unknown sensitivity, invalid grain, missing identity resolution, malformed
  payloads, missing product cards, rate limits, and model failures are explicit
  states.
- Live calls use bounded timeout, retry, and concurrency controls.
- Fixture mode supports deterministic replay and requires no external keys.

## Evaluation

The first workbench release supports baseline/proposed comparison on the same
fixture. It reports:

- context added, removed, masked, or omitted;
- product-card resolution and comprehension-gate result;
- grounding and unsupported-claim warnings;
- candidate differences;
- latency, token usage, estimated cost, parse status, and failure state.

The rendered prompt and redacted context can later be exported as Promptfoo
cases. Promptfoo evaluates workbench outputs; it does not own runtime context
assembly or product truth.

## Acceptance criteria

- One command starts the local workbench.
- A user can enter Context Layer and AI credentials without persistence.
- One live or fixture Pro/org run completes through generation and parsing.
- Raw, normalized, sanitized, current, and proposed contexts are inspectable.
- A mandatory PII gate removes or blocks names, last names, emails, phones,
  addresses, city/state/country/zip, organization/contact identifiers, and
  unknown sensitive fields before any AI call.
- The PII stage visibly shows removed field paths, categories, counts, and
  reasons without exposing the removed values.
- Exact prompts and model responses are inspectable.
- HCP AI or another candidate can be resolved to a product card and visibly
  pass, fail, or require context.
- Baseline and proposed paths can be compared on identical input.
- Token, cost, latency, parse, and error metadata are visible.
- No secret appears in logs, traces, URLs, browser storage, or downloaded
  artifacts.
- No PII appears in any model prompt, model request, saved fixture, downloaded
  trace, or browser-visible raw-value panel.
- No production write or send path is reachable from the workbench.
- Existing V3 prompt behavior remains covered by its current tests, with new
  workbench tests covering trace stages, redaction, fixture replay, and failure
  states.
