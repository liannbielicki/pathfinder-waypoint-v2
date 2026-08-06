# L2 — Integrations Dossier (Pathfinder rebuild audit, Liann's primed run)

Date: 2026-08-05. Lane: cross-cutting L2. Scope: Supabase, n8n, Snowflake, Railway
persona-cards service (+ legacy sequence-react), Google Sheets + LCM/Iterable POST,
OpenAI/Anthropic LLM APIs. Owned concerns: **C12, C13, C15**. Feeds: **C7**.
All paths repo-relative to `/Users/liannbielicki/pathfinder`. Code read directly;
docs cited only as claims and flagged where code disagrees.

North star tested throughout: *Org/Pro ID(s) in → intelligent themes out → LCM tool.*

---

## 1. Supabase

**Purpose in this repo.** Three distinct jobs share one PostgREST client
(`src/pathfinder/store/supabase_sink.py`):

1. **Run-history mirror** (telemetry): `Pathfinder_runs` / `Pathfinder_experiments`
   for the Phase-1 loop; `Pathfinder_action_runs` / `Pathfinder_action_generated_ideas`
   / `Pathfinder_action_org_uuid_evidence` / `Pathfinder_action_branch_signals` for the
   action console. Table names at `store/supabase_sink.py:33-40`. Local files are declared
   the source of truth; the mirror is "best-effort" (`store/supabase_sink.py:2-14`).
2. **Runtime audience index** (data plane): `Pathfinder_action_audience_index` — a
   Snowflake-derived org roster that run-time audience resolution and org-UUID lookup
   read from Supabase, not Snowflake (`store/supabase_sink.py:40,247-294`).
3. **Export source**: winner selection for the LCM/Sheets export reads run + idea rows
   back out of Supabase (`export/winner_select.py:103-104`).

**Where called — complete read/write inventory (non-test):**

Writes:
- `src/pathfinder/runner.py:633,1174` — `upsert_run` (Phase-1 mirror); `:1316` — `upsert_experiment`. Sink built at `runner.py:566`.
- `src/pathfinder/action_console/runner.py:1087` — `upsert_action_run` (run start, async via `_AsyncSinkWrites`, a 1-worker executor at `:288-290`); `:1105-1109` — org-uuid evidence rows (bulk then per-row); `:1230-1231,1278-1292` — per-idea upserts during the loop; `:1245-1246` — mid-run run-row updates; `:1433` (terminal), `:1515` (failed), `:1535,1564` (winner validation), `:1653` — `upsert_action_branch_signal`.
- `src/pathfinder/action_console/audience_refresh.py:87` — `replace_action_audience_index` (batch upsert 500/req + stale-refresh delete, `store/supabase_sink.py:296-374`).

Reads:
- `src/pathfinder/viewer/reader.py:504` — `list_runs(limit)` merged with local run dirs (`reader.py:495-511`; each summary tagged `source: "supabase"|"local"`).
- `src/pathfinder/action_console/live_view.py:408-410` — `list_action_runs()` for the console run list; `:478-516` — `list_action_runs` + `list_action_generated_ideas_bulk` to rebuild whole runs from the mirror; `:556` — `get_action_run`.
- `src/pathfinder/action_console/audience.py:155` — `list_action_audience_index({})` (all rows) to derive factor groups; `:309` — filtered audience resolution (`_resolve_supabase_audience`, `:303-336`).
- `src/pathfinder/viewer/app.py:853` — `get_action_audience_org` (org-lookup endpoint); `app.py:430,643,943,1005` — `resolve_audience(..., source="supabase")`; `app.py:393` — the dashboard constructs one `SupabaseSink` for everything.
- `src/pathfinder/export/winner_select.py:103-104` — `get_action_run` + `list_action_generated_ideas` per exported run.
- `src/pathfinder/export/cli.py:128-133` — hard-requires a configured sink (`error: Supabase is not configured`) before export.

**Auth & env vars.** `SUPABASE_URL` + first of `SUPABASE_KEY` / `SUPABASE_SERVICE_KEY`
(`store/supabase_sink.py:104-110`), sent as `apikey` + `Bearer` headers (`:414-423`).
Zero-dependency stdlib `urllib` client. Note: the runtime-status endpoint additionally
accepts `SUPABASE_SERVICE_ROLE_KEY` (`action_console/live_view.py:125-128`) **which the
sink itself never reads** — a deployment configured with only that name reports
`supabase.available: true` while every sink call is silently disabled (see §9 NOVEL-3).

**Cost / limits / timeouts in code.** Read+single-write timeout `_TIMEOUT_S = 4.0` s
(`store/supabase_sink.py:82`); bulk-write timeout 30 s default via
`PATHFINDER_ACTION_CONSOLE_SUPABASE_WRITE_TIMEOUT_SECONDS` (`:83-85`); audience read cap
100,000 rows via `PATHFINDER_ACTION_CONSOLE_SUPABASE_AUDIENCE_LIMIT`, page size 1,000 via
`PATHFINDER_ACTION_CONSOLE_SUPABASE_AUDIENCE_PAGE_SIZE` (`:262-271`); ideas bulk read
default limit 20,000 / page 1,000 (`:196-202`).

**Failure/retry behavior.** No retries anywhere. Every write swallows all
network/HTTP/JSON errors and returns `False`/`{"ok": False}` (`:425-473`); every read
returns `None` on any failure (`:396-412`), and callers treat `None` as "fall back to
local files". Bulk upserts surface the HTTP body (`:464-471`). This is deliberate
fail-open telemetry — but the *audience index* and the *export source* are on the same
fail-open client, so data-plane failures degrade silently too (export CLI at least
refuses when unconfigured, `export/cli.py:129-131`).

**Known live defect in current code (NOVEL-1, also C1/C7-adjacent).**
`list_action_runs` selects the full `audience` JSON blob for up to 200 runs
(`ACTION_RUN_COLUMNS` includes `"audience"`, `store/supabase_sink.py:66-80`; query at
`:172-176`) under the 4-second timeout (`:82`). The run row writes the entire
`audience.to_dict()` including per-org evidence (`action_console/runner.py:236`).
Measured 2026-08-04: 200 rows ≈ 74 MB ≈ 13 s → the GET times out and returns `None`,
so the console run list silently degrades. A `select=audience->filters` projection
(0.13 MB / 0.56 s) fixes it; not yet in code.

**Vendor risk.** Low-moderate: plain PostgREST, no Supabase-specific features beyond
upsert `Prefer` headers; trivially portable to any Postgres+PostgREST or a thin API.
Real risk is organizational: the table schemas have already drifted from the code twice
(undefined-column 42703 silently ate a run's ideas — comment at
`action_console/runner.py:244-259`; payload-only idea rows in memory of Phase-2 work).

**Required for MVP?** Split verdict. The **audience index** (org-UUID → cohort facts)
is required *as a capability* — the north star starts from an org/Pro ID, and this is
today the only production-reachable org roster (Railway cannot reach Snowflake, §3).
The **run mirror** is not required for the core path but is what makes cross-machine
history/export work today (`export/winner_select.py` reads it). A rebuilt repo could
keep one small store for (a) org index and (b) run/idea rows the exporter reads, or
replace both with n8n-landed extracts + local files. What should *not* survive is the
fail-open dual-role client where the data plane and telemetry share silent-failure
semantics.

---

## 2. n8n (org-context webhook)

**Purpose.** Per-org Snowflake context for org-mode runs, fetched over an authenticated
n8n webhook because Pathfinder holds **no Snowflake credential at all** — n8n holds it
(`integrations/n8n/README.md:12-25`; workflow: Webhook → Validate+Build SQL → Snowflake
→ Respond, `integrations/n8n/pathfinder-org-context.json` nodes 1-4).

**Where called.**
- Adapter: `src/pathfinder/action_console/n8n_org_context.py` — `build_n8n_fetch_rows`
  (`:503-597`) POSTs `{"org_uuids": [...]}` and returns the JSON array; transport with
  redirect refusal (`_NoRedirectHandler`, `:360-388`), 5 MB response cap (`:171`,
  `:333-357`), and dual-pass secret scrubbing (`_scrub_message`, `:295-321`).
- Wiring: `src/pathfinder/action_console/runner.py:75` (import), `:791-792` (env names),
  `:878-883` (build), `_org_context_pack_for_run` `:885-925` — **prefetch of exactly one
  org per run**, cleared in a `finally`.
- Gate: only runs when `cfg.org_context_source == "snowflake"` (`runner.py:912`) and only
  in org mode (`runner.py:1043-1049`). Default source is `"none"`
  (`action_console/models.py:499`; `.env.example:69`).
- Isolation/minimization: `SnowflakeOrgContextSource`
  (`action_console/snowflake_org_context.py:57-262`) enforces 1:1 org isolation and the
  field allowlist; ≤5 orgs per prefetch (`DEFAULT_MAX_ORGS = 5`, `:35`).

**Auth & env vars.** `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_N8N_URL` (must be https,
ASCII, control-char-free — `n8n_org_context.py:234-265,191-231`) +
`PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_N8N_TOKEN` (bare token ≥8 chars, sent as
`Authorization: Bearer …`, `:391-474`); flag
`PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_SOURCE=snowflake`; timeout
`PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_N8N_TIMEOUT` default 120 s
(`models.py:507`, `n8n_org_context.py:513`); `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_MAX_ORGS`
default 5 (`models.py:500`) — and the n8n Validate node independently refuses >5
(workflow Code node `MAX_ORGS = 5`), so raising the env alone does nothing
(`.env.example:83-85`).

**Cost / limits found.** Measured in-workflow query cost: ~48 GB scanned, 10-14 s for 3
orgs (`integrations/n8n/README.md:104-111` — doc claim, consistent with the 120 s
timeout in code). No client-side rate limit; one call per org-mode run.

**Failure/retry.** Zero retries; every failure (non-2xx, timeout, bad JSON, non-array,
isolation/contract violation) raises `OrgContextSourceError` and **fails the run**
fail-closed (`n8n_org_context.py:554-595`; `snowflake_org_context.py:108-160`). n8n is
explicitly not trusted: Pathfinder re-validates row isolation and the PII allowlist on
everything returned (`README.md:199-201`, enforced at `org_context_contract.py`).

**Vendor risk.** Moderate. The n8n instance is a single point of failure for org
context, and — much more importantly — **the entire SQL contract lives inside the n8n
workflow JSON as a string in a Code node** (`pathfinder-org-context.json` node
`a1…0002`), with a matching Python-side allowlist that must be changed in lockstep with
a `CONTRACT_VERSION` bump (`org_context_contract.py:70,113-155`; the workflow SQL emits
the literal `'org-context-v2'`). Version skew fail-closes every row by design.

### C12 verdict (owned): what comes through n8n today, what is plumbed but unused

**Today's actual flow:** at most **29 banded fields per org** (v2 contract:
`ALLOWED_FIELDS`, `org_context_contract.py:113-152` — 19 named + 10
`feature_*_state` fields), one org per webhook call, org-mode runs only, and **only when
an operator has opted in** — the flag defaults to `none`
(`models.py:499`), so out of the box *nothing* flows through n8n.

**Plumbed but unused:**
1. The workflow accepts 5 orgs/request; Pathfinder always sends 1
   (`integrations/n8n/README.md:2-6`; `runner.py:894-903` documents a 5-org batch =
   5 separate webhook calls, by design).
2. Segment-mode runs get no n8n data at all (`runner.py:1043` — pack only built in org
   mode), even though most console runs are segment/batch runs.
3. `minimize_row` drops anything the SQL returns beyond the allowlist, so "more data
   through n8n" is a three-place change: workflow JSON SQL + `ALLOWED_FIELDS` +
   `CONTRACT_VERSION` bump with re-sign-off (`org_context_contract.py:63-70`).
4. The much larger Snowflake pull — the **audience index refresh** — does *not* go
   through n8n; it needs a human with a browser (§3). The README itself documents the
   sibling pattern (n8n landing gzipped Snowflake CSVs into a bucket,
   `README.md:27-34`) that would carry it.

**New-repo decision that delivers C12:** keep the n8n-holds-the-credential posture (it
is the only production-viable Snowflake path found in code) but (a) generalize the
webhook to named query templates instead of one hardcoded SQL string, (b) batch the org
list (the 5-org ceiling is a validate-node constant, not a Snowflake constraint), and
(c) route the audience refresh through an n8n-landed extract so no pull requires local
SSO. The 29-field banded contract with fail-closed validation is worth carrying over
as-is — it is the best-engineered boundary in the repo.

---

## 3. Snowflake

**Purpose.** All real org/cohort data originates in Snowflake. Two disjoint access
patterns exist:

**(a) Direct connector, local-only, browser SSO** — cannot run on Railway
(Okta-federated `authenticator="externalbrowser"`, `integrations/n8n/README.md:13-17`):
- `src/pathfinder/action_console/audience_refresh.py:132-159` —
  `fetch_snowflake_audience_rows()` runs `queries/action_console_audience_boundary.sql`
  and upserts into the Supabase audience index (`:86-91`). Gated by
  `PATHFINDER_ACTION_CONSOLE_REFRESH_ENABLED=1` (`:15,58-70`).
- `scripts/extract.py:35-39,45-60` — the Phase-1 extract pipeline; fixed ordered list of
  4 query files (`wave5_02..04b`).
- `scripts/pull_group_baseline_churn.py:30-47` — group churn baselines
  (`queries/group_baseline_churn.sql`).
- `scripts/build_retention_covariates.py`, `scripts/run_option_inventory.py`,
  `scripts/probe.py`, `scripts/build_action_catalog.py`, `scripts/enrich_and_compare.py`,
  archive discovery scripts — same connector pattern.
- Guard: `src/pathfinder/import_guard.py` treats `snowflake` as a
  forbidden-at-runtime import for sandboxed strategy code.

**(b) Via n8n webhook** (production path) — §2 above. Pathfinder itself never holds a
Snowflake credential.

**Auth & env vars (direct path).** `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`,
`SNOWFLAKE_WAREHOUSE`, `SNOWFLAKE_ROLE` with hardcoded defaults — including a
**personal identity**: `_DEFAULT_USER = "JAKE.FASSORA@HOUSECALLPRO.COM"` and
`_DEFAULT_ACCOUNT = "EE60573-CN70374"` at `audience_refresh.py:18-21` and
`scripts/extract.py:28-32` (`pull_group_baseline_churn.py:30-35` mirrors them). Role
default `SNOWFLAKE_OKTA_READ_ONLY`, warehouse `wh_consumer`.

**Cost/limits.** No query timeouts or row caps on the direct path;
`_execute_snowflake_query` fetches everything into memory
(`audience_refresh.py:162-172`). No retries.

**Failure/retry.** Fail-loud: missing connector raises with install instructions
(`audience_refresh.py:134-140`); refresh returns a structured `{ok, status, message}`
result; extract scripts track per-table failures and continue
(`scripts/extract.py:148-169`).

### C13 verdict (owned): is what we pull adjustable?

**Adjustable today (evidence against C13):**
- The audience-refresh **query text** is a file, swappable via
  `PATHFINDER_ACTION_CONSOLE_AUDIENCE_QUERY_PATH`
  (`audience_refresh.py:16,146`; default `queries/action_console_audience_boundary.sql`).
- All SQL lives in `queries/*.sql` files, not Python strings (12 files), except the n8n
  workflow (below).
- Connection identity is env-overridable (`audience_refresh.py:142-145`).

**Hardcoded today (evidence for C13):**
- **Columns are pinned in at least four places** that must all agree: the SQL file, the
  upsert allowlist `_UPSERT_COLUMNS` (`audience_refresh.py:23-42`), the sink's
  read/filter column tuples `ACTION_AUDIENCE_INDEX_COLUMNS` /
  `ACTION_AUDIENCE_FILTER_COLUMNS` (`store/supabase_sink.py:42-65`), the runtime row
  normalizer `normalize_audience_index_row` (`action_console/audience.py:262-298`), and
  ultimately the live Supabase table schema. Adding one column = 4 code edits + a DB
  migration.
- Phase-1 extract tables/queries are a fixed in-code list (`scripts/extract.py:35-39`);
  no CLI flag selects or adds queries.
- The org-context SQL is a **string constant inside n8n workflow JSON**
  (`integrations/n8n/pathfinder-org-context.json`, Code node), pinned to the Python
  allowlist + version literal (`org_context_contract.py:70,113-155`) — deliberate
  (security sign-off per field set), but it means "adjust what we pull" is a
  cross-system, versioned change, never a config knob.
- Filters: run-time audience filters are constrained to the pinned column set
  (`_audience_filter_query` returns `None` — i.e. refuses — on any non-allowlisted key,
  `store/supabase_sink.py:378-394`).

**New-repo decision that delivers C13:** make the column contract single-sourced — one
schema definition (e.g. the JSON-schema files already in `data_contracts/`) from which
the SQL SELECT list, the upsert allowlist, the filter allowlist, and the normalizer are
all derived, so adding a covariate is a one-file change plus migration. Keep the
security property of the n8n allowlist, but generate both sides from the same contract
file instead of hand-synchronizing SQL-in-JSON with Python frozensets.

**Required for MVP?** Yes — Snowflake is the only source of real org facts. But the
**local-SSO leg is not production-viable** (browser + human required), so the MVP needs
either the n8n-landed-extract pattern or a service account; the current
refresh-from-a-laptop step is the single most fragile link between "org IDs in" and
everything downstream.

---

## 4. Railway persona-cards service (Riley's) + legacy sequence-react fork

**Purpose.** The evaluation axis: synthetic-persona reactions to candidate touches,
converted to churn-risk deltas. Two providers behind one seam
(`action_console/reaction_provider.py:13-103`), selected by
`PATHFINDER_REACTION_PROVIDER` (default `persona-cards`,
`persona_provider.py:23-27`, `.env.example:17`).

**(a) persona-cards (default).** Riley's service serves *cards only* (persona
descriptions); **the reaction LLM calls run locally in this repo**:
- Cards client: `src/pathfinder/persona_cards_client.py:57-72` — `httpx.post
  {base}/api/persona-cards`, GET refetch by panel id; 60 s timeout; **no retries**; any
  non-200 or schema drift raises `PersonaCardsError` (fail-closed abstain).
- Panel floor: Riley's API rejects `panel_size < 24` with HTTP 422 — client fetches
  `max(24, panel_size)` and slices locally (`local_reaction_client.py:23,46-57`), so a
  3-persona search panel costs a 24-card fetch but only 3 LLM walks.
- Reaction engine: `src/pathfinder/local_reaction_runner.py` — per persona per touch,
  one Anthropic call (persona response, temp 0.5, ≤500 tokens, `:114-140`) + one OpenAI
  gpt-4o-mini Doubt-Gap scoring call (`reaction_scorer.py:212-224`). **The walk is
  strictly sequential** — list comprehension over cards, loop over touches
  (`local_reaction_runner.py:163-183`).
- Snapshot pin: `PATHFINDER_PERSONA_CARDS_SNAPSHOT` serves cards from disk so multi-day
  runs can't straddle persona regenerations (`persona_response.py:640-660`,
  `persona_card_snapshot.py` via `scripts/snapshot_persona_cards.py`).
- Calibration guard: cards path refuses to score if the frozen reaction→churn
  calibration's sign isn't negative (`persona_response.py:541-556`).

**(b) sequence-react (legacy, our Railway fork of Riley's repo).** Full walk runs
server-side:
- Client: `src/pathfinder/persona_client.py:71-97` — `httpx.post
  {base}/api/sequence-react`; 429 handling: up to `PATHFINDER_PERSONA_SERVICE_429_RETRIES`
  (default **8**) retries sleeping `Retry-After` or
  `PATHFINDER_PERSONA_SERVICE_429_RETRY_DELAY_S` (default **60 s**) each
  (`persona_response.py:696-702`; `.env.example:45-46`) — worst case **8 minutes of
  sleep per scoring call**. Timeout default 300 s
  (`persona_response.py:690-695`).

**Auth & env vars.** Cards: `PATHFINDER_PERSONA_CARDS_URL` / `_API_KEY` (X-API-Key) /
`_TOKEN` (Bearer) (`persona_cards_client.py:26-31`; `persona_response.py:630-638`).
Sequence-react: `PATHFINDER_PERSONA_SERVICE_URL` / `_API_KEY` / `_TOKEN`
(`persona_response.py:677-687`), plus legacy rollback pair
`PATHFINDER_LEGACY_RAILWAY_PERSONA_URL/_API_KEY` (`.env.example:31-32`) and the inert
`PATHFINDER_PERSONA_SERVICE_MODE` (explicitly does not select the provider,
`.env.example:15-16`, `persona_provider.py:23` docstring).

**Call volume & budget (cost levers in code).** Search panel 3 / final 10 / confirm 12
(`models.py:491-497`); per-run persona call budget default 80 (`models.py:489`);
scoring fan-out `persona_concurrency` default 10 threads
(`models.py:490`; `persona_response.py:797,877`). Each candidate score = 2 seeded
evaluations (prefix vs prefix+candidate — `reaction_provider.py:26-29`), so cards-path
LLM cost per candidate ≈ 2 × panel_size × touches × 2 calls (Claude + 4o-mini).
Batch-level fan-out is unthrottled by default (§C6 note below).

**Failure/retry.** Everything fail-closed to *abstention* (idea marked unscoreable)
rather than fabricated zeros: `PersonaCardsError`/`PersonaServiceError` →
`estimate_persona_response` abstains. No retries on the cards path at all; only the
429-sleep loop on sequence-react.

**Vendor risk.** High — this is the deepest dependency. Riley's service is a
teammate-run fork with its own Supabase, per-key rate caps and panel bounds enforced
server-side (the 24-floor is hard-learned in code, `local_reaction_client.py:23`), and
persona regenerations that break pins (mitigated by snapshotting). The pinned
calibration ties scoring validity to one artifact
(`data/v1l/frozen/…` via `CARDS_CALIBRATION_PATH`, `persona_response.py:648`).

**Required for MVP?** The *capability* (an evaluation signal for themes) is required to
prove the north-star loop works; this particular service is the current implementation.
The cards-only split is actually favorable for a rebuild: cards are a static asset
(snapshot file already supported), and the reaction engine is already local code — the
Railway dependency could be reduced to "a JSON file of personas" without touching the
loop. Verdict: keep the seam (`ReactionProvider`), keep the local engine + snapshot,
treat the live cards service as optional refresh infrastructure.

---

## 5. Google Sheets + LCM/Iterable POST

**Purpose.** The delivery leg of the north star: batch winners → reviewer Sheet and/or
POST to Allison's LCM copy tool (which feeds Iterable).

**Where called.**
- CLI entry: `scripts/export_batch_winners.py:1-5` → `src/pathfinder/export/cli.py:62-185`.
  **No UI path** — export is CLI-only (no export route in `viewer/app.py`).
- Winner selection from Supabase: `export/winner_select.py:103-104`.
- Contacts join (org→email): CSV supplied by operator, `export/contacts.py:37-55`;
  winners without a contact row become `missing_contact` (`:67-71`).
- Sheets client: `export/sheets.py:47-81` — stdlib urllib against Sheets v4;
  append-only tab discipline (`TabExistsError`, `:23-25,75-81`).
- LCM POST: `export/post_api.py:169-225` — one POST per batch
  `{"batch": label, "rows": [...]}` to `POST /api/pathfinder/intake`; per-row receipt
  summarized (`:119-142`); batch status poll via GET (`:222-225`).

**Auth & env vars.** Sheets: service-account via `GOOGLE_APPLICATION_CREDENTIALS`
(google-auth, optional dep — `export/sheets.py:2-6,27-44`); sheet id via
`--sheet-id` / `PATHFINDER_WINNERS_SHEET_ID` (`export/cli.py:74-76`). LCM: Bearer
`PATHFINDER_LCM_POST_TOKEN` (alias `LCM_PATHFINDER_API_KEY`) + Vercel bypass header
`PATHFINDER_LCM_VERCEL_BYPASS` (alias `x-vercel-protection-bypass`), resolved from
process env then `.env.local` (`export/cli.py:39-52,88-97`); the **URL is deliberately
never read from `.env.local`** so posting stays opt-in (`export/cli.py:163-166`,
`.env.example:50-57`).

**Cost/limits.** LCM hard cap 500 rows/batch → client refuses bigger payloads
(`post_api.py:28,209-215`); 4xx are mapped to actionable hints (`:37-44`). Sheets: no
rate handling (30 s timeout per request, `sheets.py:41`).

**Failure/retry.** LCM: retries only on 5xx/network, `retries=2`, `retry_wait=2.0` s,
idempotent by `(batch, row_id=run_id)` (`post_api.py:177-207`). Sheets: no retry; any
failure falls back to writing `winners_fallback.csv` and exiting 1
(`export/cli.py:148-161,183-184`). Receipts persisted next to the CSV because rejected
rows are silently dropped on the LCM side (`export/cli.py:176-182`).

**Vendor risk.** Sheets is a commodity. The LCM intake is a person-dependency
(Allison's Vercel app; token was "pending" at doc time, `post_api.py:13-15`) and the
CTA-category mapping funnels ~60% of ideas to `other` via the keyword table
(`post_api.py:58-85` — a real quality risk for the SMS-click metric, per the keyword
list's coarseness; evidence for C17-adjacent copy fidelity questions, not owned here).

**Required for MVP?** **Yes — this is the north star's terminal edge** ("→ LCM tool"),
and it is built and small (≈700 LOC total). Keep the POST leg; the Sheet is the human
review surface and cheap to keep. The gap is upstream: it reads winners from the
Supabase mirror, so it inherits §1's fail-open risk.

---

## 6. OpenAI / Anthropic LLM APIs — call-site inventory (feeds C7)

**Anthropic (Claude) call sites:**

| # | Site | Model | Knobs | Retries/timeout | Usage recorded? |
|---|------|-------|-------|-----------------|-----------------|
| 1 | Idea generator, `action_console/generator.py:644-671` | `PATHFINDER_ACTION_CONSOLE_LLM_MODEL` else `DEFAULT_MODEL` (`claude-sonnet-4-6`, `llm_tooling.py:19`) | temp 0.8, tool-forced | `max_retries=0`, timeout `PATHFINDER_ACTION_CONSOLE_LLM_TIMEOUT_S` (90 s) | yes (`:671`) |
| 2 | Grounding critic, `action_console/grounding_critic.py:145-165` | same env/default | tool-forced | `max_retries=0` | yes (`:165`) |
| 3 | Breadth critic, `action_console/breadth_critic.py:150-170` | same env/default | tool-forced | `max_retries=0` | yes (`:170`) |
| 4 | **Persona reactions**, `local_reaction_runner.py:114-140` | `DEFAULT_MODEL` hardcoded at construction (`persona_response.py:646` builds `LocalReactionRunner()` with no model arg) | temp 0.5, ≤500 tok | SDK defaults (no explicit retry config) | yes (`:139`) |
| 5 | Phase-1 proposer, `llm_proposer.py:219-221` | `DEFAULT_MODEL` | tool-forced | SDK defaults | **no** |
| 6 | Planner proposer, `viewer/llm_planner_proposer.py:223-225` | `DEFAULT_MODEL` | tool-forced | SDK defaults | **no** |
| 7 | Grounded variants, `viewer/grounded_llm_variants.py:182-190` | `DEFAULT_MODEL` | — | `max_retries=0`, timeout `PATHFINDER_LLM_TIMEOUT_SECONDS` default **6 s** | **no** |

**OpenAI call site (exactly one):**

| # | Site | Model | Retries/timeout | Usage recorded? |
|---|------|-------|-----------------|-----------------|
| 8 | Doubt-Gap scorer, `reaction_scorer.py:212-224` | `SCORING_MODEL = "gpt-4o-mini"` hardcoded (`:28`) | `timeout=30.0, max_retries=0`, temp 0 | yes (`:223`) |

**Auth:** `ANTHROPIC_API_KEY` (checked explicitly at `generator.py:638-640`; implicit
SDK env elsewhere), `OPENAI_API_KEY` (implicit, `reaction_scorer.py:200,216`).

**Cost accounting** (`action_console/llm_usage.py`): per-run `ContextVar` ledger opened
at `action_console/runner.py:946-947`; price table `PRICES_USD_PER_MTOK`
(`llm_usage.py:32-41`) contains **Anthropic models only**.

**C7 root causes found (diagnose):**
1. **Cost reporting is structurally `None` on the default path.** Every cards-path run
   calls gpt-4o-mini (site 8) and records it; `gpt-4o-mini` is not in the price table,
   and `totals()` nulls `llm_cost_usd` whenever *any* model is unpriced
   (`llm_usage.py:186-191`). So the UI cost column shows unknown for exactly the runs
   that cost the most. One-line fix: add the 4o-mini price row.
2. **Sequence-react cost is invisible by construction** — the LLM spend happens inside
   the Railway fork; this repo only sees HTTP (persona_client.py) and records nothing.
3. **Model swap is only half-wired**: generator/critics honor
   `PATHFINDER_ACTION_CONSOLE_LLM_MODEL` (sites 1-3) but the **persona-reaction model —
   the volume driver — is hardcoded** (`LocalReactionRunner()` built with defaults,
   `persona_response.py:646`; scoring model hardcoded `reaction_scorer.py:28`). "No
   ability to modify/swap the model" is accurate for the expensive calls, false for the
   generator.
4. **Where the money goes:** per candidate ≈ 2 evaluations × panel(3) × touches ×
   (1 Claude + 1 4o-mini) calls, budget-capped at 80 persona calls/run
   (`models.py:489`), plus confirm panels at n=12 (`models.py:497`). Batch of 50 orgs
   multiplies all of it with no cross-run cost ceiling anywhere in code.
5. `max_retries=0` everywhere in the console (sites 1-3, 7, 8): a transient 429/529
   fails the idea/critic outright — cheap, but it converts provider blips into
   run-quality noise; only sequence-react has a (crude, 60 s-sleep) retry loop.

**Required for MVP?** Yes (both vendors as currently written: Anthropic for
generation+reactions, OpenAI for the cross-family scorer — the cross-family property is
load-bearing for the calibration, `reaction_scorer.py:5-13`). A rebuild should
single-source the model registry + price table and thread one `model` config through
all eight sites.

---

## 7. C15 verdict (owned): remaining Supabase pulls that were meant to move

The complete current-code Supabase surface is enumerated in §1. Classified against the
"fully transitioned to n8n/Snowflake pulls" intent:

**Data-plane reads still on Supabase (the C15 substance):**
- Audience resolution for every run: `action_console/audience.py:153-155` (factor
  groups), `:303-336` (filtered cohort). Source default in the resolver is `"local"`
  (`audience.py:117`) but every dashboard call passes `source="supabase"`
  (`viewer/app.py:430,643,943,1005`) and `.env.example:39` ships
  `PATHFINDER_ACTION_CONSOLE_AUDIENCE_SOURCE=supabase`.
- Org-UUID lookup (the north star's entry point!): `viewer/app.py:853`.
- Export winner selection: `export/winner_select.py:103-104`; export CLI refuses to run
  without Supabase (`export/cli.py:128-131`).

**Why they haven't moved (root cause, with evidence):** production (Railway) cannot
authenticate to Snowflake at all — Okta browser SSO only
(`integrations/n8n/README.md:12-17`); the only production Snowflake path is the n8n
webhook, which is (deliberately) limited to 29 banded fields × ≤5 orgs and is
default-off (`models.py:499`). So Supabase is not legacy laziness; it is the **cache
tier standing in for the missing production Snowflake path**. The refresh that fills it
(`audience_refresh.py:132-159`) is itself a local-laptop Snowflake pull.

**Genuinely-telemetry Supabase uses** (fine to keep or fold into files): Phase-1 run
mirror (`runner.py:633,1174,1316`), action-run/idea/branch-signal mirrors
(§1 write list), run-history merge (`viewer/reader.py:495-511`), console run list
(`live_view.py:408-556`).

**Verdict:** C15 is **confirmed but misdiagnosed as drift** — the code cannot be
"fully n8n/Snowflake" until something replaces the audience index for production reads.
New-repo decision: land Snowflake extracts via n8n (bucket/CSV pattern the README
already cites) or a service account, and make the runtime read that landed artifact;
keep at most one store for run history.

---

## 8. Other concerns touched (evidence, not owned)

**C6 (parallelization).**
- Batch fan-out is *unthrottled by default*: worker pool sized to the batch cap (100)
  unless `PATHFINDER_BATCH_CONCURRENCY` set (`viewer/batch_queue.py:10-17,82-88`), so
  "starting 50" genuinely starts 50 threads.
- The choke points are per-run and per-call, not the pool: (a) candidate scoring
  bounded at `persona_concurrency=10` (`models.py:490`, `persona_response.py:877`);
  (b) the cards-path persona walk is fully serial per candidate
  (`local_reaction_runner.py:163-183`) — parallelizing across the 3 panel cards is an
  untouched 3x; (c) sequence-react 429s convert to sleeps of up to 8×60 s per call
  (`persona_client.py:76-84`, defaults `persona_response.py:699-702`); (d) all
  Anthropic/OpenAI clients share one API key with no client-side rate budget, so 50
  concurrent runs × 10 scorer threads slam the provider and the `max_retries=0` sites
  then fail ideas. Beyond parallelism, the cheapest speedups visible in code: prompt
  caching (explicitly unused — `llm_usage.py:44-47`), panel-walk parallelism, and
  batching the 24-card fetch (already memoized per sequence,
  `local_reaction_client.py:40-45`).

**C14 (env vars / long names).** 78 distinct config names matched across
`src/`+`scripts/`+`api/` (grep over `PATHFINDER_|SUPABASE_|SNOWFLAKE_|ANTHROPIC_|OPENAI_|GOOGLE_|LCM_`).
Redundant pairs in live code: `PATHFINDER_ACTION_CONSOLE_FINAL_PANEL_SIZE` vs
`_FINAL_CHOICE_PANEL_SIZE` and `_SEARCH_PANEL_SIZE` vs `_PERSONA_PANEL_SIZE` (both
aliased at `models.py:511-522`); `PATHFINDER_PERSONA_SERVICE_TIMEOUT` vs
`PATHFINDER_ACTION_CONSOLE_PERSONA_TIMEOUT_S` (`persona_response.py:690-695`); four
Supabase key names with a reader mismatch (§1/NOVEL-3); three persona credential sets
(cards / service / legacy-rollback, `.env.example:19-38`); an inert
`PATHFINDER_PERSONA_SERVICE_MODE` kept for compatibility (`.env.example:35`).

**C3 (persona economics, light).** The search signal is 3 personas/idea
(`models.py:491`), confirm 12 (`:497`), while every cards fetch pays Riley's 24-card
floor and discards 21 (`local_reaction_client.py:23,46-57`). Individual-pro personas
have no code support today — cards are keyed by segment (`persona_cards_client.py:33-36`
takes `segment`), not org/pro; the org-specific signal enters only via the n8n context
pack into prompts, not into who reacts.

**C7 fed** via §6.

---

## 9. Novel findings

1. **NOVEL-1 (Supabase run-list self-DoS):** full `audience` blob selected per run row
   under a 4 s timeout → `list_action_runs` returns `None` at realistic history sizes
   (`store/supabase_sink.py:66-80,82,172-176`; blob written at
   `action_console/runner.py:236`). Console history silently falls back/blanks.
2. **NOVEL-2 (personal identity as infrastructure default):**
   `JAKE.FASSORA@HOUSECALLPRO.COM` + account id hardcoded as connection defaults in
   `action_console/audience_refresh.py:18-19`, `scripts/extract.py:28-29`,
   `scripts/pull_group_baseline_churn.py:30-35`. Any operator who doesn't set
   `SNOWFLAKE_USER` attempts SSO as Jake; bus-factor and audit-trail hazard.
3. **NOVEL-3 (Supabase key-name mismatch):** status endpoint accepts
   `SUPABASE_SERVICE_ROLE_KEY` (`action_console/live_view.py:125-128`) but
   `SupabaseSink` never reads it (`store/supabase_sink.py:104-110`) — a deployment can
   report Supabase "available" while every read/write is disabled.
4. **NOVEL-4 (cost blind spots):** three Phase-1 LLM call sites never record usage
   (§6 sites 5-7), and the price table would null any run they participate in anyway if
   they used an unlisted model. Compounds C7.
5. **NOVEL-5 (audience source default drift):** resolver default `"local"`
   (`audience.py:117`) vs runtime-status reporting default `"supabase"`
   (`live_view.py:162`) vs `.env.example:39` — three different answers to "what happens
   when the env var is unset" depending on which code path asks.

---

## 10. Unverified

Stated as unverified because no in-repo code evidence exists; do not treat as findings:

1. **Riley's service-side limits** — the 24≤panel≤50 bound (client only encodes the
   24 floor, `local_reaction_client.py:23`; the 50 ceiling and per-key 429 caps are
   server-side on her Railway deployment and not observable in this repo), the raised
   600/hr sequence-react cap, and her Supabase migration behavior.
2. **The measured 74 MB/13 s `list_action_runs` payload** — the failure *mechanism*
   (full-blob select + 4 s timeout) is verified in code; the specific measurements come
   from operational memory, not from anything committed here.
3. **n8n workflow live behavior** — the 2026-07-31 live verification, 48 GB scan cost,
   and Header-Auth 403 shapes are doc claims (`integrations/n8n/README.md:45-47,104-111`)
   I could not re-execute read-only.
4. **LCM intake behavior** — the 202-receipt contract, 500-row 413, and rejected-row
   dropping are encoded from Allison's doc into comments/constants
   (`export/post_api.py:1-44`); the live endpoint was not called.
5. **Anthropic price-table accuracy** — `llm_usage.py:31` claims verification against
   published pricing on 2026-07-31; I did not independently verify current list prices.
6. **Whether any Vercel/Railway deployment currently sets**
   `PATHFINDER_ACTION_CONSOLE_ORG_CONTEXT_SOURCE=snowflake` (i.e., whether the n8n path
   is live in production today) — deployment env state is outside the repo.
7. **The live Supabase table schemas** — `docs/run-storage-supabase.sql` and
   `sql/create_pathfinder_tables.sql` exist in-repo, but past incidents (comment at
   `action_console/runner.py:244-259`) show the live DB has drifted from these files
   before; current live schema unverifiable from here.
