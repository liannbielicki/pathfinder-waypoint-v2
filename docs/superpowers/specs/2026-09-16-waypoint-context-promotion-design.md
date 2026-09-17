# Waypoint Context Promotion Design

## Goal

Keep Pathfinder Waypoint as the default product surface, place the existing Context Workbench beside it as a second top-level tab, and add the smallest durable promotion bridge that lets everyday Waypoint use an approved context catalog and its selected feature catalog.

## Product boundary

- `/` remains Waypoint and is the default tab.
- `/context-workbench` remains the existing Workbench and is the second tab.
- The Workbench discovers, audits, curates, compiles, and evaluates context.
- Promotion happens only after compilation and evaluation.
- Promotion never edits n8n or Snowflake.
- The user receives a CSV handoff for Claude and the Snowflake MCP, then updates n8n separately.

## Promotion artifact

Promotion creates an immutable JSON bundle under the existing local Workbench data directory and atomically updates a small active-version pointer. The bundle contains the approved Include rules, the selected feature-catalog version and rows, and version metadata. It contains no organization values or credentials.

The final handoff CSV is generated from the same promoted rules and contains exactly:

1. `canonical_key`
2. `source_table`
3. `cohort_aggregate_prompt`

Only approved Include rules appear. Missing table lineage is emitted as `UNKNOWN`, never inferred from a query name.

## Runtime behavior

The production n8n response remains the source of current per-organization values. Waypoint reads the active promotion bundle, keeps only exact promoted canonical keys, preserves nulls, and attaches compact product meaning only for exact feature keys referenced by those retained variables. If there is no active bundle, the existing `org-context-v2` behavior remains unchanged. If a bundle is active but none of its keys are returned, Waypoint uses an explicit empty promoted packet instead of silently falling back to the broad legacy context.

This creates a safe rollout boundary: promotion activates the deterministic contract, while the user-owned n8n query can be updated manually from the CSV. Missing promoted values remain missing and never become zero or facts.

## UI flow

The shared header contains `Waypoint` and `Context Workbench`; `Waypoint` is first and links to `/`. After a compiled context has been evaluated, the Workbench shows one final action: `Promote to Waypoint`. Successful promotion exposes the three-column CSV download and identifies the active promoted version.

## Non-goals

- Editing or invoking n8n workflow configuration.
- Generating Snowflake SQL.
- Sending the entire feature catalog on every Waypoint model call.
- Adding a new database, state-management library, or separate product.
- Guessing source-table lineage.

## Acceptance

- Both routes show the shared two-tab navigation and `/` remains the default.
- Promotion rejects non-Include or unapproved rules and persists no organization values.
- The CSV has exactly three headers and only promoted rules.
- The active bundle supplies compact relevant feature definitions to Waypoint runtime prompts.
- Existing behavior remains available when no promotion exists.
