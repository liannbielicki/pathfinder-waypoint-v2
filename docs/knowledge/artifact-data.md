# Data-layer reference artifacts (verbatim)

Copy-paste-ready reference for the Pathfinder rebuild spec. Every value below is
extracted verbatim from the current codebase with a `file:line` citation. No
values are invented. **Secrets are named by env-var only; no secret values appear.**

---

## 1. Snowflake org-context query & facts

### 1a. Spine table + row count (VERIFIED)

- **Spine = `analytics.main.dim_organization`**, **926,837 rows, strictly 1:1 `ORG_UUID` ↔ `ORGANIZATION_ID`.**
  - `integrations/n8n/README.md:77` — `| analytics.main.dim_organization | the join spine (ORG_UUID ↔ ORGANIZATION_ID, strictly 1:1) |`
  - Inline SQL comment, `integrations/n8n/pathfinder-org-context.json:28`:
    > `-- SPINE is analytics.main.dim_organization (926,837 rows, strictly 1:1 ORG_UUID <-> ORGANIZATION_ID). marts.customer_base.agg_current_customer is NOT the spine: it holds 64,480 CURRENT customers only ... It is a LEFT JOIN source only.`

### 1b. Where the live SQL actually lives

The runtime SQL is **built inline** inside the n8n workflow node, NOT in a Snowflake view.
`queries/org_context_view_v1.sql:1-11` states the view `ANALYTICS.MAIN.PATHFINDER_ORG_CONTEXT_V1` **was never built and does not exist** (role `SNOWFLAKE_OKTA_READ_ONLY` cannot create a view). The live query text is the `sql` string in `integrations/n8n/pathfinder-org-context.json:28` (Code node). Contract is **`org-context-v2`**, 31 emitted columns (29 allowlisted fields + `org_uuid` + `contract_version`).

### 1c. DUE_AMOUNT cents→dollars divisor + INVOICE_STATUS filter (VERIFIED)

From the inline SQL header comment (`integrations/n8n/pathfinder-org-context.json:28`):

```
-- CORRECTNESS TRAPS -- both are SILENT when wrong (the query still succeeds
-- and still returns a plausible band):
--   * dim_invoice.DUE_AMOUNT is in CENTS. Always /100.0, or every AR band is
--     100x high and every org looks like a collections emergency.
--   * INVOICE_STATUS is paid | draft | open | canceled. Open AR filters to
--     'open' ONLY; for one sampled org 'draft' was ~6x the real open balance.
```

The AR CTE, verbatim from the same inline SQL:

```sql
ar AS (
  SELECT s.org_uuid,
         SUM(IFF(di.invoice_status='open', di.due_amount, 0))/100.0 usd,
         MAX(IFF(di.invoice_status='open' AND di.due_dt < CURRENT_DATE(),
                 DATEDIFF(day, di.due_dt, CURRENT_DATE()), 0)) od
    FROM spine s
    JOIN analytics.main.dim_invoice di ON di.organization_id = s.oid
   GROUP BY 1
)
```

- **Divisor: `/100.0`** (cents→dollars).
- **Filter: `di.invoice_status='open'` ONLY.**

### 1d. Spine CTE + full source-table list

Spine CTE, verbatim:

```sql
WITH spine AS (
  SELECT d.org_uuid, d.organization_id AS oid
    FROM analytics.main.dim_organization d
   WHERE d.org_uuid IN (${list})
)
```

`${list}` is the quoted UUID list built by the workflow's JS validator node (`pathfinder-org-context.json:28`): strict UUID regex, `MAX_ORGS = 5`, dedup, single-quoted — the regex is what makes the string interpolation injection-safe.

Source tables referenced by the inline SQL (all LEFT JOINed to spine):

| Table | Role in query |
|---|---|
| `analytics.main.dim_organization` | spine (926,837 rows, 1:1) |
| `marts.customer_base.agg_current_customer` | 28d bands (jobs/estimates/outreach) + quickbooks usage; LEFT JOIN only (64,480 rows) |
| `analytics.main.feature_by_orgday` | feature attach flags (latest by `date`) |
| `analytics.main.fact_online_booking_product_activity` | 90d usage count |
| `analytics.main.fact_review_product_activity` | 90d usage count |
| `analytics.main.fact_sales_proposal_product_activity` | 90d usage count |
| `analytics.main.fact_service_plans_product_activity` | 90d usage count |
| `analytics.main.fact_hcp_assist_and_csr_ai_call_product_activity` | 90d usage count |
| `production.ltv.ltv_feature_adoption_scores` | plan-gap rev/lift, recommended_focus (latest by `bizdate`) |
| `production.ltv.account_ltv` | LTV `NTILE(4)` quartile |
| `analytics.main.dim_invoice` | open AR (`DUE_AMOUNT`/100.0, status='open') |
| `marts.fintech.agg_invoice_activity` | invoices_sent 28d |
| `hcp_integrations.prod_slave_analytics.communications_sms_consents` | SMS consent (`consent_type='marketing'`, `_fivetran_deleted` filter) |
| `analytics.staging.stg__email_iterable` | email opt-out (`activity='Unsubscribed'`) |
| `analytics.main.orginfo` | vertical (174→16 map), employee count, tenure |

Key band thresholds (verbatim from SELECT list, `pathfinder-org-context.json:28`): plan_gap `<142 / <1489`; recommended_focus_value `<142 / <578 / <1489`; retention_lift `<1.4 / <8.1 / <36.9`; tenure `<12 / <24 / <48` months; open_ar `<1000 / <5000 / <25000`.

### 1e. Measured scan cost + query seconds (VERIFIED)

`integrations/n8n/README.md:104-108`:

```
Measured cost at contract v2: 3 orgs scanning ~48.02 GB in 10-14s
... bytes scanned is the stable figure; elapsed time varied 38% between runs,
which is warehouse queueing. v1 was 44.8 GB, so v2's 18 extra fields and 6
extra tables cost +3.2 GB; the scan is still dominated by stg__email_iterable
```

`README.md:105`: "(live, 2026-07-31, two runs: 10.0s and 13.8s for identical rows)."

- **Bytes scanned: ~48.02 GB (v2) for 3 orgs.** (v1 was 44.8 GB.)
- **Elapsed: 10-14s** (two measured runs 10.0s / 13.8s).
- Warehouse: `wh_consumer`; role `SNOWFLAKE_OKTA_READ_ONLY` (`README.md:41`).

### 1f. HTTP timeouts (adapter + probe)

| Timeout | Value | Citation |
|---|---|---|
| n8n fetch (per call) | **120.0s** (config default) | `models.py:507` `org_context_n8n_timeout: float = 120.0` |
| n8n fetch (adapter default) | **120.0s** | `n8n_org_context.py:513` `timeout: float = 120.0` |
| Diagnostics live probe | **15.0s** | `org_context_diagnostics.py:54` `PROBE_TIMEOUT_SECONDS = 15.0` |

`runner.py:854-858` note: adapter and config defaults are both 120.0; 30s was too little because the inline query measured 12.5s for 3 orgs before n8n/Snowflake queue overhead. The probe timeout (15s) is deliberately NOT the 120s fetch budget (`org_context_diagnostics.py:48`).

---

## 2. n8n → Snowflake path: LIVE vs stubbed, and invocation

### Status: LIVE (not stubbed), flag-gated OFF by default

- `integrations/n8n/README.md:32` — the workflow "queries Snowflake live," which is what this workflow actually does.
- `README.md:215` — "The runner wiring exists and is live in code, not pending. Org-mode runs call [the source]."
- `README.md:220` — `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_SOURCE` still defaults to `"none"`, so it is off until switched on.
- Config default: `models.py:499` `org_context_source: str = "none"`; only `"none"` or `"snowflake"` are accepted (`models.py:643-655`).

### Why HTTP indirection (not direct Snowflake from Pathfinder)

`n8n_org_context.py:1-8` + `README.md:14-16`: Snowflake at HCP is Okta-federated (`authenticator="externalbrowser"`), which cannot work on Railway, so an n8n workflow holds the Snowflake credential and Pathfinder POSTs org UUIDs to the webhook over HTTP.

### Invocation path

- `runner.py` builds the fetch adapter via `_build_org_context_fetch_rows(cfg)` → `build_n8n_fetch_rows(url=..., token=..., timeout=cfg.org_context_n8n_timeout)` (`runner.py:844-882`).
- Adapter POSTs `{"org_uuids": [...]}` (max 5) to the webhook URL; token in an `Authorization: Bearer` header, never the URL/query (`n8n_org_context.py:517`, `464-474`, `554-555`).
- No-redirect opener refuses 3xx to prevent token forwarding (`n8n_org_context.py:360-388`).

### Env-var NAMES (no values)

| Purpose | Env var name | Citation |
|---|---|---|
| Source selector (`none`\|`snowflake`) | `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_SOURCE` | `models.py:554` |
| n8n webhook URL | `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_N8N_URL` | `models.py:561`, `runner.py:791` |
| n8n webhook token | `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_N8N_TOKEN` | `models.py:565`, `runner.py:792` |
| Fetch timeout | `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_N8N_TIMEOUT` | `models.py:569` |
| Max orgs per fetch | `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_MAX_ORGS` | `models.py:557` |

Snowflake connection (used by the separate offline audience-refresh path, `audience_refresh.py:142-146`), env-var names only: `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_ROLE`.

---

## 3. Supabase persistence schema

### DDL files (cited)

- `docs/run-storage-supabase.sql` — full 6-table DDL (source of truth for the schema).
- `sql/create_pathfinder_tables.sql` — subset (only `Pathfinder_runs` + `Pathfinder_experiments`).

Local files (`runs/<run_id>/` + `reports/ledger.jsonl`) are the source of truth; Supabase is a **best-effort, fail-safe mirror** (`supabase_sink.py:2-16`). Every write swallows network/HTTP/JSON errors and never raises into the run loop.

### Tables + conflict/primary keys

| Table | Upsert `on_conflict` key (code) | DDL PK / UNIQUE | Code citation | DDL citation |
|---|---|---|---|---|
| `Pathfinder_runs` | `run_id` | `run_id text primary key` | `supabase_sink.py:121` | `run-storage-supabase.sql:10` |
| `Pathfinder_experiments` | `run_id,experiment_id` | `id uuid primary key` + `unique (run_id, experiment_id)` | `supabase_sink.py:125` | `run-storage-supabase.sql:28,42` |
| `Pathfinder_action_runs` | `run_id` | `run_id text primary key` | `supabase_sink.py:129` | `run-storage-supabase.sql:100` |
| `Pathfinder_action_generated_ideas` | `run_id,idea_id` | `primary key (run_id, idea_id)` | `supabase_sink.py:133` | `run-storage-supabase.sql:146` |
| `Pathfinder_action_org_uuid_evidence` | `run_id,org_uuid` | `primary key (run_id, org_uuid)` | `supabase_sink.py:140,150` | `run-storage-supabase.sql:161` |
| `Pathfinder_action_branch_signals` | `run_id,source_idea_id,signal_key` | `primary key (run_id, source_idea_id, signal_key)` | `supabase_sink.py:158` | `run-storage-supabase.sql:172` |
| `Pathfinder_action_audience_index` | `org_uuid` | `org_uuid uuid primary key` | `supabase_sink.py:338` | `run-storage-supabase.sql:53` |

Table-name constants: `supabase_sink.py:33-40`. Audience-index columns list: `supabase_sink.py:42-60`. Action-run columns: `supabase_sink.py:66-80`.

### Upsert mechanism (PostgREST)

- Zero-dependency stdlib `urllib` against PostgREST `/rest/v1/<table>?on_conflict=<key>` (`supabase_sink.py:425-462`).
- Upsert semantics via header `Prefer: resolution=merge-duplicates,return=minimal` (`supabase_sink.py:422`).
- Timeouts: single-row `_TIMEOUT_S = 4.0` (`supabase_sink.py:82`); bulk `_BULK_TIMEOUT_S` from `PATHFINDER_ACTION_CONSOLE_SUPABASE_WRITE_TIMEOUT_SECONDS` default `30` (`supabase_sink.py:83-84`).
- Env-var names (no values): `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_SERVICE_KEY` (`supabase_sink.py:104-109`). Disabled no-op unless both URL + key present (`supabase_sink.py:112-115`).

### Concurrency mechanism

- **File persistence: atomic write-then-`os.replace`.**
  - `store.py` (`ActionConsoleRunStore`): temp `.json.tmp` then `os.replace` (`store.py:20-30`).
  - `sequence_store.py`: `mkstemp` + `os.fsync` + `os.replace` (`sequence_store.py:14-27`).
- **`sequence_store.py` uses `fcntl.flock(LOCK_EX)` per-sequence** to serialize a transition across threads and processes (`sequence_store.py:4,48-59`). Lock file `<sequence_dir>/.lock`, exclusive lock in `sequence_lock()` context manager.
- **`max_workers` (batch parallelism)** lives in the viewer, not the store: `batch_queue.py:54-55` `ThreadPoolExecutor(max_workers=workers, thread_name_prefix="batch-run")`; `comparator_materialize.py:54,66-67,90` (`max_workers: int = 4`). The Supabase sink itself has no worker pool — writes are per-call best-effort; audience-index refresh batches in chunks of 500 (`supabase_sink.py:301,326-339`).
