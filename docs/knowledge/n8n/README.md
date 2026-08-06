# n8n: Pathfinder Org Context Pack

`pathfinder-org-context.json` — 4 nodes. The workflow *accepts* up to 5 org
UUIDs per request, but Pathfinder never sends more than 1: the runner
prefetches per run, org mode requires exactly one org per run, so a 5-org
batch makes 5 separate webhook calls rather than one batched call.

```
Webhook  ->  Validate + Build SQL  ->  Snowflake  ->  Respond
```

## Why n8n and not a direct connection

Snowflake at HCP is Okta-federated. `authenticator="externalbrowser"` needs a
browser and a human fingerprint on the machine running the process, so it
works locally and cannot work on Railway. Putting `SNOWFLAKE_PASSWORD` in
Railway does not fix that — it is a code path production never exercises.

n8n already holds a working Snowflake credential. So n8n does the pull and
Pathfinder reads the result. The consequence worth stating plainly:

**Pathfinder holds no Snowflake credential at all** — not locally, not on
Railway. It holds an n8n webhook URL and one header token. There is no
Snowflake secret to place, rotate, or leak, which is a strictly better
security posture than the OAuth design this replaces.

`Codefied/hcp-synthetic-research` runs a related pattern in production
(`ARCHITECTURE.md:232`): n8n holds the Snowflake credential and lands gzipped
Snowflake CSVs into a Supabase bucket as batch input to a long-running
refresh. That supports "n8n holds the Snowflake credential" — it does **not**
support "a service calls n8n synchronously over an authenticated webhook and
n8n queries Snowflake live," which is what this workflow actually does. The
two designs share only the credential-custody decision, not the transport
shape.

## Setup

1. **Import** `pathfinder-org-context.json` into n8n.

2. **Snowflake credential** — attach the existing one, or create it with role
   `SNOWFLAKE_OKTA_READ_ONLY` and warehouse `wh_consumer`. Read-only is
   sufficient; the workflow only ever runs one `SELECT`.

3. **Header Auth credential** — create a *Header Auth* credential. The webhook
   returns business data, so it must not be open. **Verified live on
   2026-07-31** against a real n8n instance, so this is now a confirmed
   fact rather than an actionable default:

   | Field | Value |
   |---|---|
   | Name | `Authorization` |
   | Value | `Bearer <the token you put in PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_N8N_TOKEN>` |

   **The two values are deliberately not identical.** Pathfinder adds the
   `Bearer ` prefix itself (`n8n_org_context.auth_headers`), so the n8n
   credential's Value must include it while the env var must not. n8n does a
   literal compare of the whole header value, so pasting the same string into
   both places is the single most likely mistake here, and it produces
   **HTTP 403** — confirmed, not assumed. The diagnostics pill's failure text
   points at this.

   The other 403 shape is the mirror image: a `Bearer `-prefixed env var makes
   n8n see `Bearer Bearer …`. The diagnostics panel warns on that one, because
   it can be detected from the env var alone.

4. **No view to create.** The Code node builds the whole query inline over
   real tables. `ANALYTICS.MAIN.PATHFINDER_ORG_CONTEXT_V1` does not exist and
   is no longer referenced — `SNOWFLAKE_OKTA_READ_ONLY` cannot create a view
   anyway, and nothing needs one.
   [`queries/org_context_view_v1.sql`](../../queries/org_context_view_v1.sql)
   is kept only as a reference record of the output contract.

   The fifteen tables the credential's role must be able to read:

   | Table | What it supplies |
   |---|---|
   | `analytics.main.dim_organization` | the join spine (`ORG_UUID` ↔ `ORGANIZATION_ID`, strictly 1:1) |
   | `marts.customer_base.agg_current_customer` | `t28_*` jobs / estimates / outreach counts |
   | `analytics.main.feature_by_orgday` | the 10 feature ATTACH flags: `feature_adoption_band` and the attach half of every `feature_*_state` |
   | `production.ltv.ltv_feature_adoption_scores` | `plan_gap_band`, the three `recommended_focus*` fields, `plan_tier`, and the ranking behind `top_unused_paid_feature` |
   | `production.ltv.account_ltv` | `total_ltv`, quartiled into `ltv_score_band` |
   | `analytics.main.dim_invoice` | open AR amount and aging (**cents**, status `'open'`) |
   | `marts.fintech.agg_invoice_activity` | 28-day invoices-sent count |
   | `hcp_integrations.prod_slave_analytics.communications_sms_consents` | `sms_consent_state` |
   | `analytics.staging.stg__email_iterable` | `email_consent_state` (opt-out only) |
   | `analytics.main.fact_online_booking_product_activity` | 90-day usage behind `feature_online_booking_state` |
   | `analytics.main.fact_review_product_activity` | 90-day usage behind `feature_premium_reviews_state` |
   | `analytics.main.fact_sales_proposal_product_activity` | 90-day usage behind `feature_sales_proposal_state` |
   | `analytics.main.fact_service_plans_product_activity` | 90-day usage behind `feature_service_agreements_state` |
   | `analytics.main.fact_hcp_assist_and_csr_ai_call_product_activity` | 90-day usage behind `feature_hcp_assist_state` |
   | `analytics.main.orginfo` | `vertical`, `org_size_band`, `tenure_band` |

   Only `ORGANIZATION_ID` and `ACTIVITY_BIZDATE` are read from the five
   activity facts, and only as `COUNT(*)`. That matters most for
   `fact_review_product_activity`, which also carries `CUSTOMER_NAME` and
   free-text `COMMENTS` — never selected, and asserted so by
   `tests/test_n8n_workflow_sql.py`.

   Two of those readings are silent-error traps, guarded in the SQL and
   asserted by `tests/test_n8n_workflow_sql.py`: `dim_invoice.DUE_AMOUNT` is
   in **cents** (`/100.0`), and `INVOICE_STATUS` must be filtered to `'open'`
   (`draft` was ~6x the real open balance for a sampled org).

   Measured cost at contract v2: **3 orgs scanning ~48.02 GB in 10-14s**
   (live, 2026-07-31, two runs: 10.0s and 13.8s for identical rows). Bytes
   scanned is the stable figure; elapsed time varied 38% between runs, which is
   warehouse queueing. v1 was 44.8 GB, so v2's 18 extra fields and 6 extra
   tables cost +3.2 GB; the scan is still dominated by `stg__email_iterable`
   (all-time) and `dim_invoice`.
   That is why the adapter timeout default is **120s**, configurable via
   `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_N8N_TIMEOUT`.

5. **Activate** the workflow and copy the production webhook URL.

   `n8n_org_context.py` enforces some constraints on the URL and token
   before it ever sends a request:

   - **URL must be `https://`.** The bearer token is a capability granting
     access to a Snowflake gateway; a plain `http://` webhook (e.g. a
     self-hosted or local n8n reached without TLS) is refused at build
     time, not just discouraged. A local n8n instance must be put behind
     `https` (a tunnel, reverse proxy, etc.) before Pathfinder can call it.
   - **URL and token must be free of interior whitespace or control
     characters** (a raw space, tab, newline, or other control character
     anywhere in the middle of either value is refused). A *trailing*
     newline or surrounding whitespace on either field — the canonical way
     a Railway env var paste goes wrong — is stripped automatically rather
     than rejected.
   - **URL must be ASCII.** The token is checked against the wider Latin-1
     range instead (what `http.client` actually encodes header values as),
     so a non-ASCII token (e.g. an accented character) is accepted; only the
     URL is ASCII-only.
   - **Token must be at least 8 characters** after stripping. A blank,
     whitespace-only, or shorter token is refused at build time rather than
     sent as an empty or near-empty `Authorization: Bearer ` header.

## Contract

Request:

```json
{ "org_uuids": ["11111111-1111-4111-8111-111111111111"] }
```

Response — one object per org, keys already lowercase:

```json
[
  {
    "org_uuid": "11111111-1111-4111-8111-111111111111",
    "contract_version": "org-context-v2",
    "open_ar_band": "1k_5k",
    "sms_consent_state": "opted_in",
    "feature_quickbooks_state": "attached_unused",
    "recommended_focus": "quickbooks",
    "top_unused_paid_feature": "quickbooks"
  }
]
```

That shape is exactly what `SnowflakeOrgContextSource(fetch_rows=...)` in
`src/pathfinder/action_console/snowflake_org_context.py` expects, so the n8n
call *is* the `fetch_rows` implementation — no adapter layer needed.

## Two things that are load-bearing, not style

**Lowercase quoted aliases in the SQL.** Snowflake returns column names
uppercase. `minimize_row()` matches field names case-insensitively, but
`index_rows_by_org()` looks up the literal key `org_uuid`. Drop the
`AS "org_uuid"` quoting and every request fails with "source row has no
org_uuid".

**The UUID regex in the Code node.** It is what makes interpolating the org
list into the SQL string injection-safe — only `[0-9a-fA-F-]` in a fixed
36-char shape survives it. If you loosen that regex you have introduced SQL
injection on a webhook. Use bound parameters instead if you need other input
shapes.

## Failure behaviour

Every failure mode is fail-closed by design — a refused fetch leaves
Pathfinder with no context, and the run refuses rather than proceeding with a
silently empty pack:

| Condition | Where it fails |
|---|---|
| More than 5 orgs, or 0 | Code node, before Snowflake is touched |
| Duplicate org UUID | Code node |
| Malformed UUID | Code node |
| Missing/wrong header token | Webhook node, before anything runs |
| `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_N8N_URL` is not `https://` | `n8n_org_context.py`, at build time, before any request |
| URL or token contains interior whitespace/control characters, or URL is non-ASCII | `n8n_org_context.py`, at build time, before any request |
| Token is blank, whitespace-only, or under 8 characters | `n8n_org_context.py`, at build time, before any request |
| A source table missing, or a renamed column | Snowflake node |
| A row for an org nobody asked for | Pathfinder (`index_rows_by_org`) |
| Fewer rows than orgs requested | Pathfinder (`index_rows_by_org`) |
| A PII column appearing in the query | Pathfinder (`ForbiddenFieldViolation`) |

Note the last three: n8n is not trusted to be correct. Pathfinder re-validates
isolation and the field allowlist on everything that comes back.

### First-enable surprise: org UUID case sensitivity

Snowflake string comparison is case-sensitive. If the org UUID as stored in
Pathfinder's audience index differs in case from `dim_organization.ORG_UUID`
(e.g. one side uppercases, the other doesn't), the spine CTE matches nothing
for that org and Pathfinder refuses the run with "no row for requested org."
This is fail-closed and loud, not a leak, but it is a plausible surprise the
first time the flag is switched on — if the very first live call refuses every
org, check case normalization on both sides before assuming the workflow is
broken.

## Current state

The runner wiring exists and is live in code, not pending. Org-mode runs call
this workflow through `_org_context_pack_for_run`
(`src/pathfinder/action_console/runner.py`), which builds the fetch adapter
via `_build_org_context_fetch_rows` and calls
`build_n8n_fetch_rows` (`n8n_org_context.py`) with the two env vars below.
`PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_SOURCE` still defaults to `"none"`, so
none of this runs anywhere until an operator opts in.

Setup, both steps, for a deployment that has not enabled this yet:

1. **Import and activate** `pathfinder-org-context.json` in n8n, with both
   credentials attached (Snowflake connection + Header Auth, see Setup
   above).
2. **Set the env vars** on the Pathfinder deployment:
   `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_SOURCE=snowflake`,
   `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_N8N_URL`,
   `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_N8N_TOKEN`, and optionally
   `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_N8N_TIMEOUT` (default 120s).

Waiting on an analytics-built view is no longer one of the steps: the query
is inline and its nine source tables were verified live on 2026-07-31.

**Both steps have now been done once, and the end-to-end path works**: on
2026-07-31 a real org UUID through this webhook returned real per-org rows
from Snowflake. That settles the two things the progress ledger's Phase 2
listed as unverified-by-construction — the Header Auth credential format
(see Setup step 3) and that the workflow runs at all against a live
instance.

Still open, and not settled by one successful call:

- The banding **thresholds** are authored by us rather than by analytics
  (NOTED DEVIATION 2 in the spec), so security and analytics still owe a
  review of what the model gets told.
- Behavior under load — a single org proves the path, not the 5-org batch
  concurrency or the timeout headroom.
- Whether the query returns a **complete** row per org. This matters more
  than it used to: `build_org_context_pack` no longer backfills missing
  fields from the audience index, so a partial row now means a thinner pack
  and a model that asserts less, with no error raised. Check
  `field_sources.snowflake_live` against the 29 contract fields.
