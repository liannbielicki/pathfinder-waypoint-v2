# Contact plan — pick the Pro and channel before ideation

**Status:** design, reviewed 2026-09-25 by three independent agents (code paths, data and
sender parity, rollout and tests). Decisions §2 settled by Jake. Supersedes
`docs/2026-09-24-channel-selection-analysis.md` §4 and §8; that doc's §1–3, §5–7 and §A1 stay
valid as research notes (except where §1 below corrects them).
Visual version: https://claude.ai/artifact/TQ2QGUHGKE9AGQ25z6vH7o
Candidate SQL (tested read-only on 3 orgs): `2026-09-25-contact-candidate.sql`.
**Build on `V4-Improvements`** (what staging deploys); `feature/channel-selection` is rebased onto it.

## 1. Problem

A run is keyed by `organization_id`, but Waypoint contacts a **person**: a `pro_uuid`. Orgs
average 1.7 active admins, and 39% have more than one. Today, three parts of the system each
look at a different person, or at nobody:

| Step | Where (V4) | Which Pro |
|---|---|---|
| Contact Pro | workbench SQL "Part 1": founding admin, else earliest admin | Pro X |
| RECO channel | workbench SQL "Addendum": a separate subquery with different NULL/tie rules; pro-grain `recommended_action` only; reaches the model as `suggested_channel` | usually X, can differ |
| Consent | `feasibility.gate_pro`: reads `sms/email_consent_state`, which nothing fills; `call` has no key | nobody (never blocks) |
| Channel | `channel_directive`: the model picks a channel per idea, per round; the war-game follow-up re-gates on all run channels | n/a |
| Send | `handoff.ready_rows`: `pro_uuid` from winner evidence; LCM enforces Iterable consent at send | X, dropped silently if blocked |

The contact Pro is fixed at the staging callback, before ideation, but nothing uses it before
the winner is written (`pipeline.py:1794`).

### Evidence (Snowflake aggregates, 2026-09-25)

- In **18.7%** of RECO orgs (12,329 / 65,801), RECO's best channel would reach a different
  admin from the one Waypoint contacts: email 16.7%, call 18.9%, sms 24.8%.
- In **65%** of multi-admin orgs (19,662 / 30,286), the admins' own RECO recommendations disagree.
- **51%** of orgs with an SMS opt-out (1,574 / 3,082) still have another admin who hasn't opted
  out. Email: 382 / 630.
- **Correction:** RECO scores archived Pros. The org default pair points at an inactive Pro in
  3.0% of orgs, and per-channel contact Pros are archived in 2–10% of orgs. Candidates must come
  from `dim_service_pro`, not from RECO.
- Worked example, org 294916: 4 admins, with RECO recommendations email, email, sms and call.

### How RECO chooses (verified on all 65,801 org rows)

- `<ch>_contact_pro` = the admin with the highest `p_response_raw_<ch>`.
- `org_best_channel` = the channel whose contact Pro has the highest
  `resp_pct_in_channel_<ch>`: 65,801 / 65,801. Neither `p_<ch>` nor `union_p_*` reproduces it.
  `resp_pct_*` averages 0.5 on every channel, so it is comparable across channels; `p_*` is not
  (sms mean 0.22, email 0.08).
- Pro-grain `recommended_action` is a cost-weighted rule (`call_uplift`, `best_cheap`, fatigue).
  **Don't rank on it.**

## 2. Decisions (settled)

1. **RECO picks by default.** Take the top-ranked surviving pair. No per-round model choice.
2. **Pin one (Pro, channel) per org per run.** Every round, and the follow-up's primary touch,
   is written and scored in that channel.
3. **Contact one admin**, the plan's Pro.
4. **DNC means "do not contact", but Waypoint calls aren't marketing calls.** DNC does **not**
   block `call`. It is carried as a flag and shown on the call to-do. It **does** block `sms`,
   because LCM drops those sends anyway.
5. **Email consent** follows the channel LCM's Waypoint emails use, found from a real send (org
   294916, 2026-09-24 18:41 UTC, `lcmRun`-stamped): **channel 47360** "Marketing Channel -
   Updates", **message type 183150** "Retention".
6. **RECO `suppress_all`** removes that Pro from the candidates. The org's other admins stay
   eligible.

### Defaults taken in review (flagged for Jake, easy to flip)

- A. `global_email_unsubscribes`: only **non-Iterable** sources block email. `opt_out_from_iterable`
  (16k admins) is superseded by the exact Iterable 47360/183150 check; blocking on it would
  remove 14.6% of RECO's email defaults, which is stricter than LCM.
- B. A Pro with **no Iterable profile** loses sms and email (fail closed, same as LCM). They can
  still be picked for call.
- C. `phone_shared` (16% of admins share a number, e.g. an office line) is a **flag**, not a
  block.
- D. Morning-batch's retention-case and failed-billing exclusions **do not** apply. Waypoint
  targets at-risk Pros on purpose.
- E. Recently-failed-mechanism history (`failed_mechanisms`) is keyed by **org**, so it survives
  switching admins between runs. Today it is keyed inconsistently and never matches.

## 3. Design

### 3.1 The contact plan

One immutable value is computed once per job in a new pipeline stage `plan`, right after
`context`, and checkpointed. On resume it is **loaded, never recomputed**: a retry must not
switch Pro or channel after rounds exist.

```
ContactPlan(
  pro_uuid: str | None,            # None => abstain
  channel: "sms"|"email"|"call"|None,
  open_channels: tuple[str, ...],  # channels that passed consent for pro_uuid (follow-up)
  source: "reco_default" | "reco_fallback" | "no_reco" | "legacy",
  rank_score: float | None,        # resp_pct_in_channel of the chosen pair
  flags: tuple[str, ...],          # "dnc_call", "phone_shared" - informational, never block
  runner_up: (pro_uuid, channel) | None,
  blocked: tuple[(pro_uuid, channel, reason_code), ...],  # audit; codes only
  reco_scoring_date: date | None,
  abstain_reason: str | None,
)
```

It is **not** an `OrgBrief` field: the Standard path serializes the brief into the prompt
(`catalog.py:208`). It lives in `job.checkpoint["plan"]`, in `PipelineState.plan`, and in
`WinnerRow.evidence["contact_plan"]` on every row, including abstained and no_action rows.

### 3.2 Algorithm (`contact_plan.build_plan`)

Inputs: the run's allowed `channels`, candidate rows (§3.3), Iterable profiles (§3.4), and
today's date.

1. **Candidates** are active admins from `dim_service_pro`. Drop Pros whose
   `recommended_action = 'suppress_all'`.
2. **Pairs** are candidates × allowed channels.
3. **Hard consent**: a pair is removed on any hit.
   - **sms:** blocked by any of:
     - no profile, or Iterable errored after retries;
     - no `phoneNumber`;
     - `receivedSMSDisclaimer !== true`;
     - `dnc_flag` present;
     - an unsubscribed SMS channel (62580, 141340, 141339, 62566, 116525);
     - an unsubscribed SMS message type (77064, 86860, 149942);
     - Salesforce org `sf_sms_opt_out`;
     - contact `sf_sms_opted_out` (SMS-Magic);
     - `sf_dnc_phone` (account text or opportunity `phone__c`);
     - contact `sf_donotcall`.
   - **email:** blocked by any of:
     - no profile, or Iterable errored after retries;
     - unsubscribed from channel 47360 or message type 183150;
     - `email_optout_suppressed`;
     - contact `sf_email_opted_out`;
     - `global_unsub_other`.
   - **call:** removed only when `has_phone` is false. `sf_dnc_phone`, `sf_donotcall` or Iterable
     `dnc_flag` adds `dnc_call`; `phone_shared` adds `phone_shared`.
   - `dnc_email__c` is **ignored**: it holds email addresses, not a flag.
4. **Rank** the surviving pairs:
   1. `resp_pct_in_channel_<ch>`, descending;
   2. `p_<ch>` (the candidate SQL carries `p_*`, not `p_response_raw_*`; it only breaks rare ties);
   3. Salesforce point of contact;
   4. founding admin;
   5. `pro_uuid`.

   Pairs with no RECO row rank last. With nothing blocked, rank 1 *is* RECO's org default, so
   `source = reco_default` when the pick equals `org_best_channel`/`<ch>_contact_pro`, and
   `reco_fallback` otherwise. If no candidate has RECO data, `source = no_reco`, using the same
   tiebreaks.
5. **Stale RECO:** if `reco_scoring_date` is more than 2 days old, drop the RECO keys from
   ranking (`source = no_reco`). The plan still works.
6. **Nothing survives:** abstain with a reason code, before any LLM call.
7. **Legacy:** when there are no candidate rows at all (Standard context, a flow still in
   transition, test fixtures), use `pro_uuid = brief.pro_uuid`, or the run key if it starts
   with `pro_`, and `channel` = the first allowed channel. Set `source = legacy`, with no consent
   checks. This keeps today's behaviour exactly.
8. **Operator-chosen Pro:** for runs keyed by a `pro_<hex>`, candidates are limited to that Pro.

Iterable lookups are **lazy**: walk pairs in rank order and fetch a Pro's profile only when an
sms or email pair needs it. That's about 1–2 calls per org.

### 3.3 Snowflake input (workbench n8n flow)

`2026-09-25-contact-candidate.sql` emits one row per active admin, as
`query_name='waypoint_contact_candidate'`, `variable_name='contact_candidate'`, with `value`
an object of about 30 boolean and ID keys (no phone or email values).

- **Additive:** it is appended to the Part 1 node's `union all`, so no new Merge input is needed
  and an org with 0 admins returns 0 rows, which means abstain. The old `waypoint_contact_pro`
  and RECO-addendum rows stay until enforce mode has run on staging.
- `staging_context.compile_staging_brief` extracts them with `_contact_candidates(rows)`, next
  to `_contact_pro`, and bypasses promotion. The callback persists them in
  `checkpoint["staging_context"]` next to `brief` (the raw rows are discarded after the
  callback). The Workbench variable inventory ignores `waypoint_contact_candidate` rows (the pre-existing `waypoint_contact_pro` row is left as it is today).
- Joins that matter:
  - RECO is joined on **both** `pro_uuid` and `organization_id`.
  - Salesforce `org_id__c` is compared as text, with `not isdeleted`.
  - Flags are `boolor_agg`'d across the 2–3 accounts some orgs have.
- **Before editing the live flow:** confirm the n8n Snowflake role can read
  `production.reco.*`, `hcp_integrations.housecallpro_salesforce.*` and
  `analytics.main.global_email_unsubscribes`, and export the live flow. A failing Snowflake node
  stalls the callback and holds the only staging slot (`STAGING_MAX_PENDING=1`).

### 3.4 Iterable input (direct, no n8n)

- **Call:** `GET /api/users/byUserId/{pro_uuid}` on an httpx client built like
  `iterable_source.make_client`, with a short timeout (10s).
- **Fields read:** only `phoneNumber` presence, `receivedSMSDisclaimer`, `dnc_flag`,
  `unsubscribedChannelIds` and `unsubscribedMessageTypeIds`. Nothing from the profile enters
  the prompt, the logs or the checkpoint beyond reason codes.
- **Error handling:**
  - A 404 means "no profile".
  - A 5xx or timeout raises, and the pipeline's unhandled-error backstop requeues the job.
    If attempts run out, the job fails with the reason recorded; it never guesses consent.
  - If `ITERABLE_API_KEY` is unset, the plan runs call-only and logs it once.
- **Constants:** all Iterable IDs live in one constants module (`contact_plan.py`).

### 3.5 Consumers

- **Single producer:** `pipeline.py:1794` writes `evidence["pro_uuid"] = plan.pro_uuid` and
  `evidence["contact_plan"]`. Handoff, exposures and outcomes already read `evidence["pro_uuid"]`,
  so they are unchanged. `ready_rows` also holds back any row whose channel differs from
  `contact_plan.channel`.
- **Gate / prompt:** `gate_pro` and `channel_directive` get `[plan.channel]`.
- **Follow-up:** `_attach_follow_up` gets `plan.open_channels`, not `run.channels`.
- **Remove RECO from the LLM layers** (otherwise a `reco_fallback` pin gets critic-blocked into
  `no_action`):
  - delete the `unsupported_channel_override` critic rule and the `channel_override_reason`
    schema field, prompt text and evidence;
  - strip `suggested_channel` from the model's context;
  - bump `PROMPT_VERSION`.
- **Off-pin ideas** are coerced to `plan.channel` with a log line, not suppressed, so they
  don't burn `blocked_rounds`.
- **Pinned `call` directive:** drop "recommend it only when a text/email would be ignored". The
  SMS no-consent-ask sentence stays.
- **Call to-do:** `CallItem` gains `pro_uuid` and `flags`. The `/calls` page shows them.
  `contracts/openapi.json` and `api-types.ts` are regenerated.
- **UI:**
  - `RunStart` label becomes "Allowed channels".
  - `WinnerReview` replaces "Selected · RECO · override" with a contact-plan block: Pro, channel,
    source, runner-up, flags, and a count of blocked pairs.
  - The abstained card shows `abstain_reason`.
- **Workbench preview:** its default channels follow the pin semantics (one channel).

### 3.6 Rollout

`CONTACT_PLAN_MODE = off | shadow | enforce` (setting, default `off`; Railway staging sets `shadow`, then `enforce`). A shadow-mode error never fails a job.

- **shadow:** compute and store `evidence["contact_plan_shadow"]`; behaviour is unchanged.
  Compare against today's picks for about 1 day.
- **enforce:** everything in §3.5 applies.
- **After enforce is verified on staging:** delete `CONSENT_FIELD`/`NEGATIVE_CONSENT`/
  `_consent_blocks`, `sms/email_consent_state` in `ALLOWED_FIELDS`, and the old flow rows, then
  update the tests.

**Verification orgs on staging:**
- 294916 (4 admins, mixed RECO), 842846;
- one Salesforce `sms_opt_out` org, one opportunity-DNC org, one single-admin org, one no-RECO
  org, one all-suppressed org.

**Checks:**
- the plan is in the checkpoint;
- every candidate's channel equals the pin;
- the handoff `pro_uuid` equals `plan.pro_uuid`;
- abstained jobs have 0 LLM calls;
- LCM rejects nothing;
- the call to-do shows flags;
- wall time doesn't regress noticeably.

## 4. Known limits and out of scope

- **The learning loop only learns SMS.** The Iterable poller exports `smsSend` only, and call
  winners have no outcome path. RECO's org default is 47% email and 36% call, so pinning moves
  most runs to channels the loop can't learn from yet. Follow-up: add an `emailSend` export.
- **The Standard fallback drops `pro_uuid`** (`n8n.py:234`). It stays `legacy` and is fixed
  separately.
- **The GROUNDING / `per_pro_data` hole** (analysis §7) is independent.
- **Scoring thresholds** (`win_threshold_pp`, patience) were tuned on mixed-channel runs. Watch
  the win rate per channel after enforce.
- **Merging V4 into main** adds to the existing channel conflict with main's `fb92bba`
  (RunStart, handoff, feasibility, models, prompts). Reconcile it at merge time.
- **Timing gaps:** Pros created today aren't in `dim_service_pro` until tomorrow, and RECO
  refreshes around 06:10 PT.
