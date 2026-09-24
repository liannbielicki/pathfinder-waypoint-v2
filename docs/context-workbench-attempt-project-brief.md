# Context Workbench Attempt: Project Brief and Failure Review

## Status

The Context Workbench attempt did not produce a reliable or understandable end-to-end tool. The underlying product goal remains useful, but this implementation should not be treated as a successful prototype or as evidence that the proposed context improves Waypoint recommendations.

## What we attempted to build

The Workbench was intended to be the place where we build and refine useful, durable context for Waypoint. That context must help the agent determine a user's core issues and generate solutions or outreach that help the Pro successfully use the app.

The discovery strategy is intentionally expansive. More context is not automatically better in the final prompt, but more source data gives Waypoint a larger evidence base from which to discover what matters. The Workbench must therefore begin with everything available, audit it, and prioritize the AI-facing context only after learning from it. It must not edit the underlying Snowflake or n8n queries.

The intended workflow was:

1. Enter an organization identifier.
2. Pull the complete experimental dataset from the new Snowflake-through-n8n flow, beginning with all 600-plus variables.
3. If Context Layer is enabled, query it feature by feature across the complete verified feature catalog and collect every result.
4. Remove PII before any model call.
5. Audit the full safe evidence set for relevance, coverage, reliability, duplication, conflicts, and usefulness.
6. Build up an AI-oriented catalog for the variables by adding categories, exact matching feature keys, usefulness priority, safe interpretation metadata, and aggregate prompts where appropriate.
7. Prioritize what enters the AI context while preserving the complete audited inventory and the reasons behind each decision.
8. When the user changes or expands the Snowflake queries, ingest the new result and repeat the audit, enrichment, and prioritization cycle. The Workbench does not author or modify the queries.
9. Deterministically compile organization-specific values through the resulting approved contract.
10. Use the final prioritized result as context for nondeterministic idea generation.
11. Manually judge whether the additional recommendation quality justifies its token and latency cost.

The central idea was to pay the reasoning cost during curation, then make runtime assembly deterministic, safe, and inexpensive.

## Intended sources

The attempt focused on two configurable inputs:

- **Experimental Snowflake-through-n8n flow:** a non-production workflow where the user can add or change queries to expose richer candidate context. The Workbench reads and audits the result but never edits the workflow or its SQL. The starting point is the full 600-plus-variable result, not a pre-prioritized contract.
- **Context Layer API:** an optional additional source that could be included or removed. When included, the Workbench must query every verified feature individually and retain the complete safe result for auditing before narrowing.

Comparing against the original Waypoint production flow was not required inside the Workbench; that comparison could be done manually.

## Why the attempt failed

### 1. The UI was confusing

This was the first and most visible failure.

The interface exposed too many concepts at once: source mode, context policy, runtime mode, authoring mode, feature catalog options, saved versions, prompts, raw execution stages, and a large editable catalog. It did not give the user one obvious starting action or explain which controls mattered for the current phase.

Specific problems included:

- The default screen emphasized a full runtime trace when the first useful action was authoring from the experimental source.
- **Both** was ambiguous and encouraged the user to combine sources before either source had been independently proven.
- **Runtime trace**, **Context authoring lab**, **baseline**, and **proposed** were implementation terms rather than a clear user journey.
- The catalog was rendered as a very wide, dense table with too many fields visible at once.
- Feature-catalog and delivery-channel options appeared important during authoring even though they primarily affected later compiled or recommendation context.
- Generic errors such as **All selected context sources failed** hid which source failed and why.
- The interface did not clearly separate these phases: connect, inspect, curate, test, and generate.

The result was that a technically knowledgeable user still could not tell what to select or what a successful run should look like. That is a product failure, not a documentation problem.

### 2. It was not connected to the correct `.env` setup

The V4 worktree did not contain the existing ignored `services/api/.env` file. Instead of first locating and carrying forward the exact environment configuration used by the prior Workbench, the setup initially diverted to an unrelated vault workflow.

The later `.env` copy was byte-for-byte correct, but the configured `N8N_CONTEXT_WEBHOOK_URL` pointed to the exported workflow named **Waypoint Variable Audit Context - Snowflake v1**. That is the experimental authoring flow, which returns variable-audit rows. The Workbench's default runtime path treated that same variable as though it pointed to an already-compiled organization-context endpoint.

This created a configuration-contract mismatch:

- The environment variable existed.
- Authentication could succeed.
- The experimental workflow could return data.
- The receiving code expected a different response contract.

Checking only that environment variables were populated was therefore insufficient. We failed to verify that each configured value pointed to the exact service and response shape expected by the selected Workbench mode.

### 3. We incorrectly treated the broad discovery set as a runtime-contract problem

The new n8n flow returned a large variable inventory shaped like:

```text
QUERY_NAME
VARIABLE_NAME
VALUE
METADATA
```

One observed run returned 649 rows. That large result was intentional and should have been accepted as the starting evidence set. The purpose of the Workbench was to audit all of those variables first, then build up the AI-useful metadata around them: categories, exact feature relationships, usefulness priority, safe interpretation rules, and aggregate prompts where appropriate.

Instead, the implementation pushed the discovery result too early into a compact `org-context-v2` runtime contract and rejected it because it did not report that contract version. The failure was not that the experimental response was too large or shaped incorrectly. The failure was that the Workbench did not preserve a deliberate discovery phase before runtime compilation.

The required loop is:

```text
START WITH ALL VARIABLES -> AUDIT -> ENRICH FOR AI -> PRIORITIZE -> COMPILE
USER CHANGES QUERIES -> RE-AUDIT -> ENRICH FOR AI -> RE-PRIORITIZE
```

The large source dataset and the final runtime context serve different purposes. The source dataset should be broad so the system can discover useful evidence. The eventual runtime contract should be prioritized and token-efficient only after repeated evaluation.

### 4. Tests did not prove the live workflow

Unit tests covered local transformations and mocked integrations, but passing them did not demonstrate that:

- the correct `.env` file was loaded;
- the n8n URL targeted the intended workflow;
- the live workflow completed within the client timeout;
- the live payload matched the selected mode;
- a real authoring run produced a reviewable draft;
- the resulting context improved recommendations enough to justify its cost.

Live checks produced inconsistent timing: the experimental source returned its inventory during one run and later exceeded the client's 60-second timeout. That performance was not resolved or measured well enough to call the integration reliable.

### 5. The work was not stored durably

Later uncommitted improvements were kept only in `/private/tmp/pathfinder-waypoint-v4`. Temporary-directory cleanup removed that worktree. Because those changes were not checkpointed in Git or stored in a durable project directory, recreating the `V4-Improvements` worktree restored the older committed Workbench instead of the latest local version.

This was an execution failure independent of the product design. Future worktrees must live under `/Users/jakefassora/projects/`, and meaningful work must be continuously preserved there. Commits still require explicit approval.

## What we learned

- The experimental n8n source is deliberately a broad discovery and authoring input. Its 600-plus variables are the correct starting point.
- Broad source coverage and compact runtime context are not opposites; the Workbench must use the former to discover and refine the latter.
- Query changes remain user-controlled. The Workbench re-audits and re-enriches each new result instead of attempting to modify the queries itself.
- Context Layer coverage must begin with a feature-by-feature sweep across the complete verified catalog rather than a small preselected subset.
- A source being reachable does not mean its payload is compatible with the current consumer.
- The Workbench must prove one source at a time before offering combined-source behavior.
- The first screen should guide the user through one task instead of exposing the entire internal pipeline.
- Source-specific errors, payload shape, row count, and latency must be visible without revealing raw values or secrets.
- Context rules need a compact review experience; a spreadsheet with every advanced field visible is not usable.
- Recommendation-quality claims require a real evaluation set and measured token, latency, and output-quality data.

## Requirements for a successful retry

### User flow

The next attempt should present five explicit stages:

1. **Connect:** confirm which `.env` file is loaded and whether each selected source is configured.
2. **Collect:** fetch the full experimental result for one organization and, when enabled, query Context Layer once for every verified feature.
3. **Audit:** retain all safe variables and distinguish what was directly observed for this organization from what can only be inferred from the variable name and output.
4. **Enrich:** add only decision-useful metadata to each variable, including category, exact feature mappings, usefulness ranking, and aggregate prompts where appropriate.
5. **Prioritize:** decide what enters the compiled AI context, preserve the full audited inventory, and record why each variable was included, deprioritized, or excluded.
6. **Repeat:** when the user supplies results from changed or expanded queries, rerun the audit, enrichment, and prioritization process.
7. **Test:** compile the prioritized organization-specific context deterministically, show its exact token estimate, and optionally generate recommendations.

Each stage must remain unavailable until the previous stage succeeds.

### Integration contract

- The experimental n8n workflow must have its own clearly named configuration.
- Its expected request and response shapes must be documented and validated at the boundary.
- Authoring must accept and account for the complete variable-audit row format deliberately, including all 600-plus starting variables.
- Context Layer collection must iterate over every exact feature key in the verified feature catalog and report complete, partial, or failed coverage.
- The Workbench must never edit the n8n workflow or Snowflake SQL. Query changes are external, user-controlled inputs.
- Every prioritization decision must be reversible so later user-supplied query changes can restart the audit loop.
- Runtime compilation must accept only a reviewed, versioned context contract.
- Missing, stale, conflicting, or duplicated values must not silently become facts.
- Context Layer must remain optional and independently testable.

### Audit evidence boundaries

The audit must not pretend that one organization reveals population-level data quality. A null or missing value proves only that the value was absent in that observed response. It does not prove that the variable is broadly sparse, broken, stale, or unhelpful.

The minimum useful authoring record is:

- exact source key and path;
- observed value type or shape;
- observed state for the current run, such as present, null, or absent;
- inferred value category;
- inferred usefulness priority;
- inferred related features, restricted to exact keys from the selected feature catalog;
- optional aggregate prompt when cohort-level evidence would materially improve the decision;
- include, deprioritize, or exclude recommendation;
- a short basis label: **observed**, **inferred**, or **unavailable**.

From a single organization response, the Workbench may directly observe keys, paths, types, shapes, and whether a value was present in that response. It may infer likely meaning, category, usefulness, and feature relationships from the variable name, source metadata, and safe output shape.

The Workbench must mark the following as unavailable unless the source explicitly supplies the evidence or the audit spans enough organizations to measure it:

- overall missingness or coverage;
- freshness or staleness without a trustworthy timestamp;
- reliability;
- population distribution;
- conflict frequency.

These authoring judgments support curation but do not all belong in Waypoint's runtime prompt. Runtime context should contain only the approved organization values, compact state markers needed to avoid false claims, and exact relevant feature references. Human rationale and unavailable metrics remain in the Workbench.

### Versioned feature catalog

The feature catalog must be replaceable without a code change. The expected input is a CSV whose exact required columns will be locked after the authoritative catalog is provided.

- Every upload creates a new immutable catalog version instead of overwriting the prior version.
- A version records its ID, name, source filename, creation time, and validated feature entries.
- The user can select the active version, compare added, removed, and changed feature keys, and return to an earlier version.
- Every audit and context-catalog version records the exact feature-catalog version it used.
- Feature mappings are validated against exact keys from that selected version.
- Uploading a new catalog revalidates existing mappings and flags removed or changed keys for review.
- No upload automatically approves or publishes new mappings.

### Acceptance criteria

A rebuild is not successful until all of the following are demonstrated live:

- The UI identifies the exact environment file in use without displaying secret values.
- The experimental n8n source succeeds for a known organization within an agreed timeout.
- A source test shows a useful sanitized payload summary and a precise failure reason when it fails.
- The authoring flow accounts for the complete experimental inventory without silently skipping variables or sending raw organization values to the model.
- When Context Layer is enabled, every verified feature is queried and the UI reports the exact coverage count.
- A CSV feature catalog can be uploaded as a new version, selected, compared, and rolled back without changing code.
- Each audit identifies the feature-catalog version it used and flags mappings invalidated by a later catalog.
- Single-organization absence is reported only as observed absence, not global missingness or variable failure.
- Freshness is reported only when supported by a trustworthy source timestamp; otherwise it is explicitly unavailable.
- Inferred category, usefulness, and feature mappings are visibly distinguished from directly observed facts.
- The user can change the experimental query outside the Workbench, rerun the audit, and compare successive catalog versions without changing production.
- The user can understand what to click without external instructions.
- The rule editor is compact and does not render a giant table.
- Draft, approved, and published states are visibly different and never advance automatically.
- A deterministic preview produces compact organization context from the reviewed contract.
- Token count and latency are measured, not estimated when telemetry is available.
- Recommendation comparison is performed only after the context path is proven.

## Recommended next step

Do not begin with another large UI rewrite. First build and run one live, non-secret-bearing acceptance check for the complete experimental discovery flow:

```text
known organization
  -> all experimental n8n variables
  -> all Context Layer feature queries when enabled
  -> PII-safe full inventory
  -> audit every variable
  -> add categories, exact feature mappings, and AI-useful metadata
  -> prioritize the compiled context
  -> re-audit whenever the user supplies updated query results
```

Once that path is repeatable and its request, response, timeout, and error behavior are known, put a thin guided UI around it. This keeps the next attempt focused on proving the context workflow rather than displaying an unproven pipeline.
