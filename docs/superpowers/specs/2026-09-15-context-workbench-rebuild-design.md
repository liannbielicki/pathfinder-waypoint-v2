# Context Workbench Rebuild Design

## Goal

Build a local Workbench that begins with the complete experimental n8n result, audits every safe variable, enriches that inventory against a versioned feature catalog, and deterministically compiles only reviewed context for Waypoint.

## Product boundary

The Workbench reads the configured experimental n8n webhook and optional Context Layer API. It never edits Snowflake SQL, n8n workflows, Waypoint production state, Iterable, LCM, or outbound messages. Query changes remain external and user-controlled; a new source result starts a new audit.

Broad discovery and compact runtime context are separate artifacts:

1. **Collect** retains every PII-safe source variable.
2. **Audit** records source path, query, observed type, and present/null state for this run.
3. **Curate** adds inferred category, exact feature mappings, usefulness rank, disposition, and optional cohort aggregate prompt.
4. **Compile** selects reviewed entries and assembles compact organization-specific context deterministically.

Absence for one organization is only an observed absence. Global coverage, freshness, reliability, distributions, and conflict frequency remain unavailable unless the source provides evidence.

## Source contracts

The n8n source accepts `{organization_id}` and returns the existing variable-audit rows: `QUERY_NAME`, `VARIABLE_NAME`, `VALUE`, and optional `METADATA`. The client accepts the full 600-plus-row response and uses a longer bounded timeout suitable for the experimental query.

The Context Layer has one read-only endpoint, `GET /api/context_layer/:organization_uuid`, which returns the org's complete related-feature set. The Workbench calls it once and compares the returned feature names against every exact key in the selected feature catalog, producing present, absent, unmatched, and total counts. It does not invent a per-feature endpoint.

When both sources are enabled, the Workbench first looks for an exact `ORG_UUID` value in the scrubbed n8n rows and uses it for Context Layer. If none exists, it reports a source-specific identity error instead of silently calling Context Layer with a numeric organization ID.

## Feature catalog versions

A CSV upload is validated by the local API. `feature` is the required exact-key column; all other columns are retained. Duplicate or blank feature keys fail validation. A content hash creates an immutable version ID. The browser stores version ID, name, filename, creation time, validated entries, and original CSV locally.

Selecting a version makes it active. The UI compares it with the previously selected version by exact feature key and reports added, removed, and changed keys. Selecting an older version is the rollback action. No upload approves or publishes context mappings.

Every context-catalog version records the feature-catalog version used. Existing related-feature mappings are revalidated against the active version; unknown keys are removed from AI output and surfaced as warnings.

## Authoring contract

Deterministic code creates the audit inventory without sending organization values to the model. Each inventory item supplies only the exact variable key, source path/query, observed type, and observed state. The model may infer only:

- `canonical_key`
- `value_category`
- `related_features` (maximum three exact active feature keys)
- `usefulness_rank` (1-5)
- `disposition` (`include`, `deprioritize`, or `exclude`)
- `aggregate_prompt` (cohort-level only, rank 4-5)

The model output is a draft. Invalid fields are normalized or rejected, partial batches requeue missing keys, and no state advances to reviewed automatically. Human rationale stays in the Workbench and is excluded from runtime context.

## Deterministic compiler

Compilation requires a selected context-catalog version. It matches exact source keys to scrubbed organization values, includes only entries marked `include` and explicitly reviewed, preserves `null` as an observed state instead of a fact, attaches only exact verified feature keys, and emits compact JSON plus measured serialized byte/character counts and a deterministic token estimate. Compilation never calls an AI model.

## UI

The page presents one guided sequence: **Connect -> Collect & audit -> Curate -> Compile**. Environment status shows the resolved `services/api/.env` path and configured/not-configured booleans only. Snowflake/n8n is selected by default; Context Layer is an optional checkbox.

The catalog editor is a searchable list of compact variable cards, not a wide table. Each card shows the key, observed state/type, usefulness, disposition, and review state; advanced fields open on demand. Source errors name the failing source, response contract, duration, and safe reason.

## Failure and security behavior

- Credentials remain server-side and never appear in responses, browser storage, traces, or logs.
- Raw source values are withheld until the deterministic PII gate finishes.
- One source may succeed while another fails; partial results are visible and usable.
- Malformed source rows and invalid catalog keys are explicit warnings, never silently converted to facts.
- Authoring stops cleanly at its token budget and reports all remaining keys.
- Draft, reviewed, and compiled states are visibly distinct.

## Verification

Backend unit tests cover the 600-plus-row contract, observed/inferred boundaries, Context Layer coverage, catalog validation, exact-key enforcement, partial-batch requeue, and deterministic compilation. Frontend tests cover the guided defaults, environment status, immutable catalog versions/diffs, compact editor behavior, and source-specific failures. A final live smoke test uses the existing ignored `.env` without printing any value.
