# Workbench-Backed Staging Context Design

## Goal

Make Waypoint's Staging context mode reuse the unchanged
`N8N_CONTEXT_URL_WORKBENCH` flow and the direct Context Layer API, then apply the
active approved Workbench catalog as a deterministic runtime allowlist before any
context reaches the existing recommendation pipeline.

Standard context remains unchanged. This design supersedes the earlier requirement
that Staging use a separately maintained compressed n8n workflow which emits
canonical aliases.

## Fixed decisions

- Waypoint Staging inputs are digit-only organization-ID strings such as
  `889901`. They are never coerced to integers.
- The same numeric organization ID is sent independently to the Workbench n8n
  webhook and the Context Layer API.
- The Workbench n8n query and workflow remain unchanged and may continue returning
  roughly 640 variables.
- Both upstream responses must complete before the Staging packet is compiled.
- Only rules in the active approved Workbench promotion can reach the AI.
- Runtime does not perform another AI audit, catalog curation pass, or broad PII
  classification pass. The approved catalog is the runtime allowlist.
- Standard mode continues to use `N8N_CONTEXT_URL` and the current Standard client.
- The existing Waypoint generation, ranking, screening, measurement, and handoff
  pipeline is unchanged after context creation.
- Design, plan, implementation, tests, and supporting documentation ship in one
  commit.

## Configuration and routing

Runtime Settings add optional declarations for:

- `N8N_CONTEXT_URL_WORKBENCH`
- `CONTEXT_LAYER_BASE_URL`
- `CONTEXT_LAYER_API_KEY`

`N8N_TOKEN` remains the Workbench webhook credential. A Staging run is available
only when all four Staging dependencies are configured: the Workbench URL, shared
n8n token, Context Layer base URL, and Context Layer API key.

`N8N_CONTEXT_URL_STAGING` remains accepted temporarily for deployment compatibility,
but it is deprecated: routing, readiness checks, and tests never read or select it.
No database migration is required: the persisted `runs.context_source` value remains
`standard` or `staging`.

The worker owns one long-lived Standard client and one composite Staging client.
The pipeline continues selecting the client from the run's persisted
`context_source`; it does not learn about either Staging upstream.

## Source contracts

For each organization ID, the composite Staging client performs these requests
concurrently and within the existing configured context concurrency limit:

1. `POST N8N_CONTEXT_URL_WORKBENCH` with
   `{"organization_id":"889901"}` and the existing bearer token.
2. `GET {CONTEXT_LAYER_BASE_URL}/api/context_layer/889901` with the Context Layer
   bearer token.

The n8n response uses the existing Workbench variable inventory format: an array
of rows containing `VARIABLE_NAME`, `VALUE`, and optional `QUERY_NAME`,
`SOURCE_TABLE`, or `METADATA` lineage. The Context Layer response remains its
existing object containing firmographics, feature rows, and other supported
fields.

Raw source responses are ephemeral. They are not stored in run checkpoints,
included in model prompts, or logged as values. Safe diagnostics may contain only
the promotion ID and counts for received, matched, discarded, missing, ambiguous,
and conflicting variables.

## Normalization and deterministic selection

The composite client normalizes both complete responses into source values without
canonicalizing them first:

- Snowflake values retain their exact `VARIABLE_NAME` and available source-table
  lineage.
- Context Layer values use the same stable source keys created by the Workbench,
  including `context_layer.firmographics.*`, `context_layer.features.<feature>.*`,
  and supported top-level `context_layer.*` keys.

The active promotion is then applied as an allowlist:

1. A Snowflake rule matches its exact `source_key` and `source_table`. If the
   promotion has no usable table lineage, the source key may match only when it is
   unique in the response. Ambiguous duplicates are omitted rather than guessed.
2. A Context Layer rule matches its exact prefixed `source_key`.
3. Rules whose source values are absent remain absent. Null remains null.
4. Variables without an active rule are discarded, regardless of how many extra
   variables either source returns.
5. A matched value is written under the rule's compact `canonical_key`.
6. Conflicting values for the same canonical key are omitted and logged without
   their values; they are never silently converted into a fact.

This selection occurs entirely before prompt construction. An unapproved source
variable cannot appear in the promoted value map, product-feature map, or AI
prompt.

## Compiled Staging packet

The final packet continues using the existing compact promoted-context shape:

- `v`: retained canonical organization values;
- `n`: retained canonical keys whose source value is explicitly null;
- `f`: exact approved feature mappings for retained rules;
- `pc`: approved feature-catalog cards referenced by retained rules.

The complete approved feature catalog remains in the immutable promoted artifact,
but runtime sends only exact feature cards referenced by rules whose values were
retained for that organization. The Context Layer API response supplies current
organization-specific context; it does not replace the feature catalog.

Canonical values whose names correspond to existing `OrgBrief` fields are also
projected into those typed fields so existing deterministic gates and persona
matching continue operating without pipeline changes. `waypoint_context()` uses
the compact promoted packet for the model prompt exactly as it does today.

The returned `OrgBrief` is keyed by the submitted numeric organization ID. This
preserves the pipeline's existing lookup contract even though the historical
property is named `pro_id`.

## Failure behavior

- Standard selected: use only `N8N_CONTEXT_URL`; do not call Workbench n8n,
  Context Layer, or load the active promotion.
- Staging selected with missing configuration: reject creation with a clear 422
  response listing Staging as unavailable.
- Missing active Postgres promotion: fail the hosted Staging context fetch; never
  fall back to Standard, broad raw context, or an older packaged promotion. The
  packaged promotion remains available only to explicit local/test wiring.
- Workbench n8n or Context Layer failure: fail the Staging fetch after existing
  bounded job retry behavior; do not construct a partial packet.
- Invalid source response: fail with a source-specific contract error that does
  not include raw values or credentials.
- Missing approved variables: omit them. If both sources return valid responses but
  zero approved variables match, fail with a value-free contract error so schema
  drift cannot silently produce data-poor recommendations.
- Extra variables: ignore them without changing the promotion or failing the run.
- Multiple organization IDs use bounded per-organization concurrency. Each
  organization independently requires both sources; a failed organization follows
  the existing per-job retry behavior and never falls back to Standard.

## User interface

The Start a Run context toggle remains Standard/Staging and defaults to Standard.
The identifier label and helper text make the Staging contract explicit:

- Standard retains its existing identifier behavior.
- Staging expects digit-only organization-ID strings, one per line. The browser and
  API both reject a non-numeric Staging identifier before work is queued.

The UI does not expose source URLs, credentials, catalog internals, or additional
pipeline controls.

## Security boundary

This change guarantees that only active approved catalog variables reach the AI.
It deliberately does not change the user-owned n8n/Snowflake workflow or solve the
separate question of what data may exist inside n8n or cross its webhook. That
transport-side concern remains outside this one-commit application change.

Secrets remain server-side. Raw responses and discarded values are never persisted
or returned to the browser by the runtime Staging path.

## Verification

Tests must prove:

- Standard mode calls only the original Standard client and produces unchanged
  context.
- Staging sends the same numeric organization ID to the Workbench webhook and
  Context Layer API and waits for both.
- Staging accepts the existing tall 640-variable response contract without
  requiring canonical aliases from n8n.
- Exact source-key and source-table rules retain approved values and ignore every
  unapproved value.
- Context Layer prefixed keys participate in the same allowlist and canonical
  compilation.
- Duplicate source keys without sufficient lineage and conflicting canonical
  values fail closed by omission.
- Null and missing values remain distinguishable.
- Only feature cards referenced by retained approved rules are attached to the
  compact packet.
- Existing typed `OrgBrief` fields are populated only from retained canonical
  values needed by deterministic downstream behavior.
- Missing promotion, either-source failure, and malformed responses never fall
  back to Standard or expose raw values.
- A valid response with zero approved matches fails clearly rather than producing
  an empty context packet.
- Hosted Staging requires the active Postgres promotion and never silently loads a
  packaged fallback.
- Safe diagnostics identify the promotion and report counts without logging source
  values.
- Staging availability requires the Workbench and Context Layer configuration;
  Standard remains available without them.
- The UI and API accept only digit-only Staging organization IDs, preserve them as
  strings, and leave Standard as the default.
- `N8N_CONTEXT_URL_STAGING` is not used for availability or runtime routing.
- Backend tests, frontend tests, lint, static types, and the production web build
  pass before the single commit is created.

## Non-goals

- Editing Snowflake SQL or n8n nodes.
- Creating or using a separate compressed n8n workflow.
- Re-running catalog authoring or AI classification during a Waypoint run.
- Changing recommendation generation, ranking, persona screening, measurement,
  sending, or handoff behavior.
- Adding a database migration or new persistence layer.
- Solving n8n execution-history or webhook transport PII policy in this change.
