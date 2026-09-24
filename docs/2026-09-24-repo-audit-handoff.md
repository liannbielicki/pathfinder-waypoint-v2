# Waypoint — repo audit & cleanup handoff

**Audience:** an AI agent starting cold on this repo. Read this whole file before touching anything.
**Written:** 2026-09-24, from branch `channel-selection-consent` (cut from `V4-Improvements` @ d8df7d5).
**Owner:** Jake Fassora. Ask him before any step marked **ASK**.

Every claim below is tagged:
- **[verified]** — checked against code, git, or live Snowflake on 2026-09-24, with the evidence named.
- **[owner]** — stated by the owner; not provable from the repo.
- **[unknown]** — nobody has checked. Treat as an open task, not a fact.

Do not upgrade an [unknown] to a fact without checking it. The previous agent did exactly that
about which n8n flow serves real runs, and was wrong — which is why this document exists.

---

## 0. Your job, in order

**Status 2026-09-24:** Phase A is mostly done (§6 lists what's still open). **Phase B is executed** — see
§4.7. The orientation docs `CLAUDE.md` and `docs/REPO-MAP.md` now exist; the rest of Phase C (archiving
superseded docs, `docs/ARCHITECTURE.md`) is still to do.

1. **Phase A — Freeze the facts.** Resolve the [unknown]s in §6 that the cleanup depends on.
2. **Phase B — Get off V2.** ~~One final docs-only push to `V2-Improvements`, then move the working base (§4).~~ Done.
3. **Phase C — Clean docs.** Inventory, archive, correct, and write the missing knowledge base (§5).
4. **Phase D — Channel-selection work** resumes only after A–C (§7). Do not start it early.

Ground rules for all phases:
- Never push to `main`. Never force-push anything. Never skip hooks.
- Never delete a branch, worktree, or file without first showing Jake what's in it (`git log`, `git diff`, `ls`).
- Backend checks: `cd services/api && .venv/bin/python -m pytest -q`, `ruff check src tests`, `mypy src`. **Do not run `ruff format`.**
- Comment style in code: explain the *why*; a `ponytail:` prefix marks a deliberate simplification with a named ceiling.
- This repo is on **GitHub** (`liannbielicki/pathfinder-waypoint-v2`), use `gh`. Jake's global notes say GitLab — that is for his other work, not this repo. **[verified]** `git remote -v`

---

## 1. Vocabulary — the word "staging" means two things

This is the single biggest source of confusion. Always disambiguate.

| Term | Means | Where |
|---|---|---|
| **Staging environment** | The Railway deployment `pathfinder-waypoint-v2-staging.up.railway.app` | infra |
| **Staging context** / `context_source="staging"` | A *per-run* choice to build the Pro's brief from the **Context Workbench** pipeline instead of the legacy Standard flow | `models.py:43`, run form |
| **Standard context** / `context_source="standard"` | The legacy synchronous org-context flow | default |

Also: the **repo** is named `pathfinder-waypoint-v2`, and the primary checkout directory is
`~/projects/pathfinder-waypoint-v2`. Neither has anything to do with the branch `V2-Improvements`.
The `V2/V3/V4/v5` branches are successive improvement rounds on the same repo.

---

## 2. How a run gets its context — ground truth

### 2.1 Which path real runs use

**[owner]** Real runs use **Staging context** (the Context Workbench path). Standard is kept only as a
fallback in case the workbench path fails; the plan is to migrate fully to Staging.

**[verified]** Code supports both, chosen per run and persisted:
- `ContextSource = Literal["standard", "staging"]` — `services/api/src/waypoint/models.py:43`
- Pipeline branches on `run.context_source` — `services/api/src/waypoint/pipeline.py:1992`
- **[verified] Mismatch:** the run form defaults to `"standard"` — `apps/web/src/components/RunStart.tsx:57`.
  If Staging is the real path, every run where the operator forgets to flip the toggle silently uses
  the fallback. **ASK** whether to flip the default.

### 2.2 Staging context path (the real one)

**[verified]** end to end, `staging_context.py` + `pipeline.py:1992–2050` + `worker.py:413–427`:

```
run (context_source=staging, audience_query="workbench:<promotion_id>")
  → pipeline reserves a staging slot, run status "waiting / staging_context_pending"
  → WorkbenchStagingContextClient.start(org_id, job_id, promotion_id)
      POST N8N_CONTEXT_URL_WORKBENCH   (n8n answers 202 immediately)
  → n8n fans out 9 Snowflake queries, appends rows, POSTs them back to
      /api/context/staging/callback            (runs)
      /api/context-workbench/source-callback   (workbench authoring)
  → Waypoint also calls the Context Layer API directly (firmographics: segment, industry)
  → compile_staging_brief(): applies the ACTIVE PROMOTION BUNDLE from Postgres
      rule = source_key → canonical_key, lineage-checked on source_table
      only scalar values survive; anything without a rule is DROPPED SILENTLY
  → OrgBrief: typed fields for canonical keys that match OrgBrief fields,
      everything else in curated_context["v"]
```

Consequences an agent must internalize:
- **In Staging, a Snowflake column reaches the model only if a promotion rule exists for it.** Adding a
  column to the n8n SQL is necessary but not sufficient. Missing rules show up only in the
  `missing=` count of the `staging context compiled` log line.
- Three things bypass promotion on purpose: `firmographics.segment/industry` (Context Layer),
  `core_saas_plan`, and the contact `pro_uuid` from the `waypoint_contact_pro` row
  (`staging_context.py:_contact_pro`). That "authoritative bypass" pattern is the precedent for any
  field that must never depend on a human remembering to promote it — e.g. consent/DNC (§7).
- `N8N_CONTEXT_URL_STAGING` is **dead**. Staging uses `N8N_CONTEXT_URL_WORKBENCH`
  (`worker.py:415`, commit `10cd4b5 fix: share async context flow across workbench and waypoint`).
  The setting still exists in `settings.py:18`, `.env.example`, and three tests.
  `docs/HUMAN-TASKS.md:32` already calls it deprecated.

### 2.3 Standard context path (the fallback)

**[verified]** `N8NContextClient.fetch` → synchronous POST to `N8N_CONTEXT_URL`, expects a 200 JSON
array; a 202 raises `ContextConfigurationError` (`n8n.py:378–382`). Rows are projected through the
closed `ALLOWED_FIELDS` allowlist (`n8n.py:34`). Repo copy of that flow:
`n8n/waypoint-context-snowflake-v1.json`, webhook path `pathfinder-org-context`. It contains an
Iterable `Get a user` node keyed on `pro_uuid` whose response Waypoint discards.

### 2.4 The n8n flows — live vs repo **[verified 2026-09-24]**

Pulled from the live n8n instance via its public API (`GET /api/v1/workflows`, key `N8N` + `N8N_URL`
from Jake's vault — run with `vault run <cmd>`, never print the values). The instance holds ~300
workflows from many teams; these six are Waypoint's.

| Live workflow (id) | Active | Webhook path | Role | Repo copy | Drift |
|---|---|---|---|---|---|
| Waypoint Context_URL_Workbench (`uagiNmDAitRAHHRv`) | yes | `waypoint/context-v1` | **Staging context (real runs) + Workbench** — async 202 + callback | `n8n/waypoint-variable-audit-context-async-v1.json` | Only the webhook node differs: repo says path `waypoint/context-async-v1`. All queries identical. |
| Waypoint Context_URL (`64Cuwv6v20ANg5Yo`) | yes | `pathfinder-org-context` | **Standard context (fallback)** — synchronous | `n8n/waypoint-context-snowflake-v1.json` | Only the `Merge` node's parameters differ. |
| Waypoint - Snowflake pull (`hen9aPWGv5lC2FqE`) | yes | `pathfinder-audience-boundary` | audience pull; last updated 2026-08-10 | **none** | Nothing in the repo references this path. **ASK** whether anything still calls it. |
| waypoint-morning-batch (`ibHXuZUPOA4zZxnh`) | no | schedules | daily test/control batch: Salesforce DNC SQL + Iterable per-user SMS eligibility check | **none** | Contains the best consent logic anywhere — see §3 and §7. Its code cites `scripts/pick_ios_batch.py` and `scripts/n8n/pick_batches.js`, which are **not in this repo**. |
| Pathfinder Waypoint — outcome ingestion (`OWgWr0nsAza0iFPo`) | no | schedules | Iterable/Amplitude outcome polling | `docs/n8n/outcome-ingestion.workflow.json` | 9 of 16 nodes differ. Inactive — outcome polling now lives in the app (`amplitude_source.py`, `iterable_source.py`, `outcomes.py`). **[likely superseded]** |
| Waypoint_Context_URL_Staging (`sZUPpk7F69pRits9`) | no | random uuid | the old `N8N_CONTEXT_URL_STAGING` flow | **none** | Dead, matching the dead setting. |

Repo files with **no live counterpart at all** — dead copies:
`n8n/waypoint-variable-audit-context-v1.json` (the old synchronous audit flow; also claims path
`waypoint/context-v1`) and `n8n/waypoint-variable-audit-context-by-org-uuid-v1.json`.

Stranded file: `n8n/pathfinder-daily-audience-dnc.json` exists only in unpushed local commit `1be55b0`
on `V2-Improvements`. It's a template (audience SQL is a `WHERE 1 = 0` placeholder) with an Iterable DNC
check. The live `waypoint-morning-batch` is the more complete version of the same idea.

---

## 3. Snowflake facts established 2026-09-24 **[verified]** (`snow sql -c hcp`)

Keep these; they took a while to find.

- `production.reco.channel_recommendations` — **pro grain**, 406,565 rows / 65,746 orgs, refreshed daily.
  `recommended_action` values: `sms` 221,762 · `email` 103,174 · `call` 81,628 · `suppress_all` 1.
  Lowercase — already what `OrgBrief.suggested_outreach_channel()` (`n8n.py:164`) accepts.
  Also has `p_sms/p_email/p_call`, fatigue tiers, and boolean `email_optout_suppressed`.
- `production.reco.channel_recommendations_org` — **org grain**, exactly 1 row/org, daily
  (72 distinct days in `_ORG_HIST` over 72 calendar days). `org_best_channel`, `union_p_*`,
  per-channel contact pro. Distribution differs sharply from the pro table
  (org: email 47 / call 36 / sms 17 %; pro: sms 55 / email 25 / call 20 %).
  **Trap:** its `email_contact_optout` is a continuous rate, not a flag.
- The live async flow's RECO query uses the **pro** table via the founding admin and emits only
  `RECOMMENDED_ACTION` (no probabilities, no opt-out flag).
- RECO respects email opt-out only: `call` recommended into DNC orgs 5,645 rows; `sms` to SMS
  opted-out Pros 1,185 rows; `email` to email-suppressed Pros 0.
- DNC/opt-out, best source found: **Salesforce account**, keyed by org —
  `hcp_integrations.housecallpro_salesforce.account` joined on `org_id__c` (= `organization_id`),
  344,042 orgs. `sms_opt_out__c` (boolean) = TRUE for 24,645 orgs. `dnc_phone__c` / `dnc_email__c`
  are **free-text** fields (values like `Do Not Call`, `DO NOT CALL`, `DNC`, and stray phone numbers):
  ~176 phone-DNC and 92 email-DNC orgs. The live `waypoint-morning-batch` flow matches with
  `ilike '%do%'`, which misses the `DNC` spelling — normalize properly. It also reads
  `opportunity.phone__c`, contact-level `hasoptedoutofemail`, and `analytics.main.global_email_unsubscribes`.
- The **send-time SMS rule** the LCM team uses, copied into `waypoint-morning-batch`'s `Pick batches`
  node and labeled "LCM parity (featureProjection.js gate:dnc_suppressed)": an Iterable user is
  SMS-eligible only if `dataFields.dnc_flag` is empty, `phoneNumber` is present,
  `receivedSMSDisclaimer === true`, and none of `unsubscribedChannelIds` / `unsubscribedMessageTypeIds`
  are SMS channels or message types (ids from Iterable `GET /api/channels` and `/api/messageTypes`).
  The source of truth is `featureProjection.js` in the LCM codebase (Allison), not this repo.
- Warming-program DNC flags, broader but not a contract:
  `marts.staging.int__warming_exclusion_flags.dnc_orgs_flag` (transient staging table, 5,053 / 72,222 orgs) and
  `marts.sales.snap_daily_warming_eligibility.dnc_orgs_flag` (daily snapshot). They disagree on 197 orgs.
- Pro-level SMS opt-out: `analytics.main.fact_iterable_sms_transaction.has_opted_out` (by `pro_uuid`;
  covers only Pros Iterable has texted).
- **Neither context flow emits any consent or DNC field.** `sms_consent_state` was removed 2026-08-19
  (it measured the Pro's *customers'* consent); `email_consent_state` never had a source. So the
  consent gate `feasibility.gate_pro` has never blocked anything in production.

---

## 4. Branches, worktrees, stashes — full inventory and cleanup plan

Everything in this section was **[verified]** on 2026-09-24 with `git fetch`, `git branch --merged`,
`git cherry`, `git worktree list --porcelain`, `git stash list`, and `git -C <worktree> status` for
every worktree.

### 4.1 Branches that stay

| Branch | Role |
|---|---|
| `origin/main` @ fb92bba | Default branch on GitHub. V3 learning loop merged (PR #6); tip is Liann's `fb92bba feat: open channel selection`. |
| `V4-Improvements` @ d8df7d5 | **The working base. Railway staging deploys it [owner].** 1 behind / 47 ahead of main. Every push redeploys staging. |
| `origin/feat/feature-catalog-v2` | **Liann's**, 6 catalog commits not on main or V4. Not ours — leave it. |
| `V2-Improvements` | Frozen archive after one final docs-only push (§4.5). Never merges to main. |
| `channel-selection-consent` | This handoff + Phase D work. Cut from V4. |

Local `main` is 2 behind `origin/main`: fast-forward it.

**Divergence hazard:** channel selection was implemented **twice**: on main by Liann (`fb92bba`) and on
V4 by Jake (`ebb2b86`, then `3ae840c`, `d77656d`, `cde7de5`, `8087581`, `ad1ed88`). `git cherry` finds no
patch-equivalent of `fb92bba` on V4. Both touch `RunStart.tsx`, `feasibility.py`, `handoff.py`,
`models.py`, `prompts.py`, so merging main into V4 **will conflict**. Keep V4's behavior unless Jake says
otherwise; note `fb92bba` makes LCM handoff skip call winners, which V4 already handles with the calls panel.
**ASK** before this merge — it redeploys staging.

### 4.2 Branches to remove

Fully merged into `origin/main` (`git branch --merged`) — plain delete:
`V3-Improvements-pre-rebase`, `backup/V2-local-pre-reset`, `claude/closed-loop-learning-sync-da17ff`,
`claude/setup-page-ui-changes-34dcc5`, `fable/production-build`, `feature/compounding-evolve-loop`,
`fix/lcm-intake-contract`, `worktree-hotfix-panel-handoff-sms`.

Fully contained elsewhere — plain delete:
- `V3-Improvements` (= `origin/V3-Improvements` @ 41af898): merged into main except one commit, whose
  patch-equivalent is on V4 (`git cherry` shows `-`). Deleting the **remote** branch is a change to
  Liann's GitHub repo — **ASK**.

Not merged — **tag first** (`archive/<name>`), then delete, so nothing is lost:
| Branch(es) | Unique work | Tag |
|---|---|---|
| `v5` | `499833b spike: deterministic concept-quality gate` (+ the V2 docs commit) | `archive/v5-concept-quality-spike` |
| `claude/ecstatic-roentgen-ebf15d`, `claude/laughing-hugle-41a57d`, `claude/quizzical-driscoll-f977d9` (all @ 162e05b) | `fix: scope the Iterable export by campaign` — docs for the n8n outcome-ingestion flow, which is now inactive and superseded by in-app pollers | `archive/outcome-ingestion-campaign-scope` |

Existing tag `v3-learning-loop` is merged into main; keep it.

### 4.3 Worktrees

| Worktree | Branch | Uncommitted | Action |
|---|---|---|---|
| `~/projects/pathfinder-waypoint-v2` (primary) | `V2-Improvements` | `TODOS.md` (+2 backlog items **not** on V4 or main), `RunStart.tsx` (1 line, already on V4 via `5399293`), untracked `docs/context-workbench-attempt-project-brief.md` | Move the 2 TODOs to V4's `TODOS.md`; discard `RunStart.tsx`; archive the brief (§4.5). Then switch the primary to `main` (see note). |
| `.claude/worktrees/pathfinder-waypoint-v4` | `V4-Improvements` | untracked `n8n/waypoint-variable-audit-context-by-org-uuid-v1.json` (no live counterpart) | **Jake's active worktree — do not touch.** Archive that file (§4.5) only with his OK. |
| `.claude/worktrees/channel-selection-consent` | `channel-selection-consent` | this doc | keep |
| `.claude/worktrees/pathfinder-waypoint-v5` | `v5` | clean | remove after tagging |
| `.claude/worktrees/ecstatic-roentgen-ebf15d` | `claude/ecstatic-roentgen-ebf15d` | untracked `docs/specs/waypoint-learning-loop.md` (Aug 26 "Building a loop that can learn" build spec, 506 lines) | archive the spec (§4.5), then remove |
| `.claude/worktrees/laughing-hugle-41a57d` | `claude/laughing-hugle-41a57d` | clean | remove |
| `.claude/worktrees/closed-loop-learning-sync-da17ff` | `claude/closed-loop-...` | `TODOS.md` — already on V4 and main | remove |
| `.claude/worktrees/hotfix-panel-handoff-sms` | `worktree-hotfix-panel-handoff-sms` | clean | remove |
| `.claude/worktrees/setup-page-ui-changes-34dcc5` | detached @ 162e05b | clean | remove |
| `.claude/worktrees/v3-learning-loop` | detached @ 6c58e0b (merged) | clean | remove |
| `~/projects/pathfinder-waypoint-v3-outcome-pollers` | `V3-Improvements` | `AuthoringCatalog.tsx` download fix — **obsolete**: V4 rewrote that component and the JSON download no longer exists | remove |
| 4 × `/private/tmp/pathfinder-waypoint-*` | — | directories already gone | `git worktree prune` |

Note on the primary checkout: git won't check out one branch in two worktrees, and V4 is checked out
in Jake's v4 worktree. So the primary goes to **`main`** (stable, matches GitHub) and V4 work stays in
worktrees. Never commit to `V4-Improvements` from another worktree while Jake's v4 worktree has it
checked out — moving the ref under him desyncs his working tree. Put V4-bound changes on a branch and
let Jake merge.

### 4.4 Stash

`stash@{0}` "codex-v4-workbench-before-sync-2026-09-16" — based on `ebb2b86` (early V4), 16 files of
workbench changes. V4 has since rebuilt the workbench, so it's almost certainly superseded, but not
provably. Tag it (`archive/stash-codex-v4-workbench-2026-09-16`) and drop it. The stash stack is shared
across worktrees and sessions — re-find it by message before dropping.

### 4.5 The one docs-only push to V2 **[owner-approved]**

`V2-Improvements` becomes the archive of loose, historical docs. It never merges to main. Contents:
- `1be55b0` (already committed locally): `docs/knowledge/*`, `docs/options/lifecycle-stage-options.md`,
  an idea-ranking warm-starts plan, `n8n/pathfinder-daily-audience-dnc.json` — the only copies anywhere.
- `docs/context-workbench-attempt-project-brief.md` (Sep 15). **Read it before trusting the workbench:**
  it's a failure review ("did not produce a reliable or understandable end-to-end tool"). It predates
  the V4 fixes that made Staging the real path, but its critique may still apply.
- `docs/specs/waypoint-learning-loop.md` from the ecstatic-roentgen worktree.
- `n8n/waypoint-variable-audit-context-by-org-uuid-v1.json` from Jake's v4 worktree, with his OK.

Push once. After that, V2 is read-only.

### 4.6 Execution order

1. Tags: `archive/v5-concept-quality-spike`, `archive/outcome-ingestion-campaign-scope`, `archive/stash-codex-v4-workbench-2026-09-16`. Push tags **[ASK]**.
2. Primary checkout: move the 2 TODOs to a V4-bound branch; discard `RunStart.tsx`; copy archive files in; commit to `V2-Improvements`; push V2 once.
3. Remove the worktrees in §4.3, then `git worktree prune`.
4. Delete the local branches in §4.2. Remote `origin/V3-Improvements` only with an explicit yes.
5. Drop the stash.
6. Fast-forward local `main`; switch the primary checkout to `main`.
7. Land the knowledge base (§5.3) on both `main` (PR) and V4 (branch → Jake merges).

### 4.7 Executed 2026-09-24 (Jake approved everything except deleting `origin/V3-Improvements` and touching his v4 worktree)

- Tags created and pushed: `archive/v5-concept-quality-spike`, `archive/outcome-ingestion-campaign-scope`,
  `archive/stash-codex-v4-workbench-2026-09-16`.
- `V2-Improvements` final push `8a7db57`: `ARCHIVED.md`, the workbench failure review, the learning-loop
  spec (plus `1be55b0`). Frozen.
- The primary checkout's 2 unlanded TODOs moved to `TODOS.md` on `channel-selection-consent` (V4-bound);
  its `RunStart.tsx` change discarded (already on V4).
- Worktrees removed: v5, laughing-hugle, hotfix-panel-handoff-sms, setup-page-ui-changes, v3-learning-loop,
  ecstatic-roentgen (spec archived first), closed-loop-learning-sync, `~/projects/pathfinder-waypoint-v3-outcome-pollers`;
  4 `/tmp` entries pruned. Remaining: primary, `pathfinder-waypoint-v4` (Jake's), `channel-selection-consent`.
- Local branches deleted (13): `V3-Improvements-pre-rebase`, `backup/V2-local-pre-reset`,
  `claude/closed-loop-learning-sync-da17ff`, `claude/setup-page-ui-changes-34dcc5`, `fable/production-build`,
  `feature/compounding-evolve-loop`, `fix/lcm-intake-contract`, `worktree-hotfix-panel-handoff-sms`,
  `V3-Improvements`, `v5`, `claude/ecstatic-roentgen-ebf15d`, `claude/laughing-hugle-41a57d`,
  `claude/quizzical-driscoll-f977d9`. Remaining local: `main`, `V4-Improvements`, `V2-Improvements`,
  `channel-selection-consent`.
- Stash dropped (tagged first).
- Local `main` fast-forwarded to `origin/main`; the primary checkout is now on `main`.
- Not done, by choice: `origin/V3-Improvements` stays; the untracked
  `n8n/waypoint-variable-audit-context-by-org-uuid-v1.json` in Jake's v4 worktree left alone.

---

## 5. Docs — inventory and cleanup

### 5.1 What exists (on V4)

- Root: `README.md`, `FRONTEND.md`, `TODOS.md`. **No `CLAUDE.md`, no `ARCHITECTURE.md`.**
- `docs/`: `HUMAN-TASKS.md`, `environment.md`, `context-workbench-partner-handoff.md`,
  `2026-09-24-next-session-handoff.md`, `2026-09-24-channel-selection-analysis.md`, this file.
- `docs/superpowers/plans/` (18) and `docs/superpowers/specs/` (15) — dated design/plan pairs from 2026-08-07 to 2026-09-23.
- `docs/context-layer/`, `docs/knowledge/`, `docs/n8n/`, `docs/plans/`, `docs/research/`,
  `docs/specs/`, `docs/verification/`.
- `.planning/` — three task_plan/progress/findings sets (workbench rebuild, promotion, v4 idea quality).
- V2-only (commit `1be55b0`): `docs/knowledge/hcp_feature_catalog.csv`, `org_lifecycle_segments.sql`,
  `pro_variable_audit_mountain_hvac.md`, `docs/options/lifecycle-stage-options.md`,
  an idea-ranking warm-starts plan, `n8n/pathfinder-daily-audience-dnc.json`.

### 5.2 Known-stale or contradictory **[verified]**

| Doc | Problem |
|---|---|
| `docs/superpowers/specs/2026-09-17-per-run-context-source-design.md` | Describes three URLs with Staging on `N8N_CONTEXT_URL_STAGING`. Superseded by `10cd4b5`: Staging uses the Workbench URL. |
| `docs/superpowers/specs/2026-09-17-railway-context-workbench-design.md:12` | Same three-URL claim. |
| `docs/HUMAN-TASKS.md` | Env section is current; "Launch status" is from `fable/production-build` (88 tests; the suite is now ~800). |
| `README.md` | "Local Context Layer Workbench" section appears **twice** (≈L91 and ≈L194); says "The V3 branch includes…". |
| `docs/superpowers/plans/2026-09-23-loop-reliability-and-scoring.md` §2A–2D | Premises wrong: assumes RECO value format is the problem (it isn't), that audience SQL is the DNC filter (it isn't), and that fixing `NEGATIVE_CONSENT` matters (no field feeds it). |
| `docs/2026-09-24-channel-selection-analysis.md` | Written before §2 was known. Its §4 recommends mapping Iterable consent in the **Standard** flow — the fallback. Corrected banner added; §7 here supersedes it. |
| `n8n/waypoint-variable-audit-context-async-v1.json` | Webhook path drifted from live (§2.4). |

### 5.3 Target structure (proposal — **ASK** before moving files)

```
CLAUDE.md                 ← NEW. Short. Points at ARCHITECTURE.md; lists commands, rules, vocabulary (§1).
docs/ARCHITECTURE.md      ← NEW. The living "how it works inside and out". Built from §2–§3 of this file.
docs/runbooks/            ← env/Railway/n8n operations (HUMAN-TASKS env section, environment.md)
docs/n8n/                 ← one README mapping every flow → path → env var → role → live status
docs/decisions/           ← specs/plans that describe CURRENT behavior
docs/archive/YYYY-MM/     ← superseded specs/plans/handoffs, moved not deleted, each with a one-line
                            "superseded by …" header
```
Rules: move, don't delete. Every archived doc gets a header naming what superseded it.
`ARCHITECTURE.md` is the source of truth; when code changes, it changes in the same commit.

### 5.4 Acceptance for Phase C
- A fresh agent reading only `CLAUDE.md` → `docs/ARCHITECTURE.md` can correctly answer: which flow
  serves real runs, how a Snowflake column reaches the model in each path, what "staging" means, which
  branch to work on, and how to run checks.
- No doc in `docs/decisions/` contradicts the code. Everything else is in `docs/archive/` with a header.
- `n8n/` repo copies match live exports, or each drift is written down.

---

## 6. Open questions — resolve in Phase A

| # | Question | Blocks | How to answer |
|---|---|---|---|
Answered 2026-09-24: Railway staging deploys **V4-Improvements**. Repo-vs-live n8n diff is done (§2.4).
The old synchronous `waypoint/context-v1` flow no longer exists live. The uncommitted V2 `RunStart.tsx`
is already on V4 — discard it.

| # | Question | Blocks | How to answer |
|---|---|---|---|
| 4 | Should the run form default to Staging? | §2.1 | **ASK** |
| 6 | Does the active promotion bundle map `RECOMMENDED_ACTION → suggested_channel`? | §7 | Query the promotion table in the staging Postgres (`RAILWAY_WAYPOINT_STAGING` is in the vault), or the workbench UI. If the rule is missing, that alone explains "RECO unavailable" on every staging run. |
| 7 | Is Salesforce account DNC plus LCM's Iterable rule (§3) the right consent definition, or is there something more canonical? | §7 | **ASK** Jake; get `featureProjection.js` from the LCM team |
| 8 | Delete remote `origin/V3-Improvements` (fully contained in main + V4)? | §4.2 | **ASK** — it is Liann's repo |
| 9 | Does anything still call the active `pathfinder-audience-boundary` flow? | §2.4 | **ASK** |
| 10 | Should the repo n8n copies be refreshed from live and the dead ones archived? | §5 | **ASK**; the drift is small (one webhook path, one Merge node) |

---

## 7. Phase D — channel selection (do not start before A–C)

Owner's intent **[owner]**:
- DNC, SMS opt-out, and email opt-out are applied **first** and are hard: never send a Pro something on
  a channel they can't or won't receive.
- RECO is the **basis** for the choice among what remains.
- If several channels remain open, give the model the open set **plus** RECO's preferred channel, and
  let the model decide.

Refinement recommended by the previous agent, **ASK** before adopting: let the model decide **once per
Pro, before the loop**, then pin that channel for every round. Reason [verified]: loop identity is
`mechanism_key()` = slug of the label text only (`loop.py:183`); channel isn't part of it, but
`channel_directive()` (`prompts.py:32`) reshapes the idea heavily per channel. Re-picking per round makes
rounds incomparable. Cost: the loop can't discover "this theme only works by email".

Constraints that follow from §2:
- Real runs are Staging, so the data has to come through the **async workbench flow** and survive
  `compile_staging_brief`. Consent/DNC must **not** depend on a promotion rule — follow the authoritative
  bypass precedent (`_contact_pro`, firmographics) so a missed promotion can't silently disable a
  safety gate.
- RECO should come from the org-grain table with probabilities, not the bare pro-grain string.
- Consent should match what the sender enforces. The best prior art is the live (inactive)
  `waypoint-morning-batch` flow (§2.4): Salesforce account DNC / SMS opt-out in SQL, plus LCM's
  per-user Iterable SMS rule (§3). The stranded `pathfinder-daily-audience-dnc.json` guesses different
  Iterable field names (`emailSendDnc`, `smsSendDnc`) and says to confirm them against a real response
  — prefer the morning-batch/LCM definitions.
- The async workbench flow has no Iterable node, so an Iterable check on the Staging path means adding
  one to that flow, or calling Iterable from Waypoint.
- `call` has no consent key in `CONSENT_FIELD` (`feasibility.py:28`) and always fails open.
- Downstream gate ordering is already right: `infeasible_channel` precedes the RECO-override check
  (`pipeline.py:884`), and the RECO rule is guarded by `suggested_channel in channels`.

Independent bug, fix regardless **[verified]**: a shipped handoff said "SMS showed strong returns FOR
THIS PRO (82/121…)" — population numbers from `pattern_summaries()` (no `pro_id`) narrated as one Pro's
history. Causes: the GROUNDING rule (`prompts.py:277`) bans only *invented* values, not misattributed
ones; the evidence block is labeled population-wide only at its top; and block kind `"per_pro_data"`
is listed in `SUPPRESSING_BLOCK_KINDS` (`pipeline.py:615`) but defined nowhere else in the repo — the
critic is never told it exists. Fix all three and add a test that fails if the kind goes unwired.

---

## 8. What the previous session left behind

- Branch `channel-selection-consent` + worktree `.claude/worktrees/channel-selection-consent`, cut from
  V4 @ d8df7d5. Uncommitted: this file and `docs/2026-09-24-channel-selection-analysis.md`.
- No code changed. No commits. Nothing pushed.
- `.claude/worktrees/pathfinder-waypoint-v4` is Jake's active worktree for other work — **do not touch it.**
