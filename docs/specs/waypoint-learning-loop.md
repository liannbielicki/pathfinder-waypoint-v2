# Building a loop that can learn

> Pathfinder Waypoint V2 · Build spec v1.0 · 26 Aug 2026 · V2-Improvements

Waypoint picks a message per Pro and has never measured whether it worked. Spec to fix
that: five breaks, one closed vocabulary, one metric, exact build order. *Six probes under
two days gate every date here.*

| Signal | Value |
|---|---|
| Outcomes recorded | 0 |
| Distinct mechanisms | 108/109 |
| Touches attributable | 101/105 |
| Themes from real data | 6 |
| Amplitude events seen | 633/828 |
| To top-line answer | 16d |

Live Iterable + staging Waypoint + Amplitude catalog (633 of 828 events seen), 25–26 Aug 2026.

## Contents

1. [Goal](#01-goal)
2. [Verify first](#02-verify-first--2-days-gates-everything)
3. [State](#03-state)
4. [Themes](#04-themes)
5. [Signals](#05-signals)
6. [Proving it learns](#06-proving-it-learns)
7. [Metric](#07-primary-metric)
8. [Schema](#08-schema)
9. [Contracts](#09-contracts)
10. [Feedback](#10-feedback)
11. [Build order](#11-build-order)
12. [Keep vs undo](#12-keep-vs-undo)
13. [Traps](#13-traps)

---

## 01 Goal

Learn which recommendation makes a Pro return, and feed it back. Four questions: reached a
real Pro · engaged · returned, how fast · did the specific thing asked.

**Constraints.** No Waypoint id in Iterable — attribution rides `(run_id, pro_id)`, which
both systems already carry. All external calls GET, read-scoped credentials that cannot send.

**Out of scope v1:** fitting warm-start weights · theme × segment · anything keyed on
clicks · a 14-indicator catalog.

---

## 02 Verify first — <2 days, gates everything

| | Question | Method | Cost | If wrong |
|---|---|---|---|---|
| V1 | Real 7d return base rate, σ, ρ? | `useractivity` × 200 Pros, distinct active days over 28d | 1d | p≈0.90 → metric at ceiling, dead. p≈0.15 → binary unusable. Dates move 3×. |
| V2 | LCM randomizes before or after intake? | 20 arm-B `userId`s vs `runs.pro_ids` | 1h | If before, arm B has no winner rows → per-theme lift not estimable. |
| V3 | `usersearch(pro_uuid)` resolves? | 100 real uuids, count hits | 1h | <95% → biased population. Blocking. |
| V4 | Corpus already dirty? | `count(*)` vs `count(distinct pro_id)`, 90d handoffs | 1 query | Retroactive cleaning impossible. |
| V5 | Arm B on a different campaign? | List campaigns vs `19156972` | 10m | Our 25 Aug campaign filter is silently dropping the control arm. |
| V6 | Iterable SMS events in Amplitude? | `/api/2/events/list \| grep -i iterable` | 10m | If yes, most of the Iterable export retires. |

---

## 03 State

### Works — verified, do not rebuild

Attribution without stamping: `lcmRun` = `run_id`, Iterable `userId` = `pro_uuid`,
`uq_winners_run_pro` makes the pair one winner. **101/105 matched live.** Remaining 4 = one
run handed off, never sent (real QA drop). Plus: evidence gate (`routing='route-to-pro'`),
idempotent ingestion, funnel, campaign-scoped export (117k rows → ~100).

### Broken

| | Break | Evidence | Consequence |
|---|---|---|---|
| A | No aggregation unit | 108 distinct mechanisms / 109 winners; 107 seen once | Theme learning impossible at any volume |
| B | Measurement plan never executed | Read only at `handoff.py:221` (gate) and `api.py:255` (display) | Question 4 unanswerable; a paid call producing paperwork |
| C | Control arm discarded | Parser filters `lcmVariant === 'A'` | Lift not computable |
| D | Four Iterable defects | `smsClick` has no `transactionalData` → every click dropped · 33/38 clicks were bots · device tests counted · `smsBounce` unread (13/187) | Delivery + engagement numbers false |
| E | Two signals are lies | `handoff.py:43` coerces unparseable → `"rejected"` · `failed_mechanisms` exact-matches free text, so `pipeline.py:983` can never fire | QA rate false; suppression dead |

> **Break A is not a labelling defect**
>
> The prompt says: *"Every idea in the batch must use a mechanism distinct from the others —
> duplicated mechanisms are discarded."* SHIFT adds a forbidden list. The model is complying;
> `payment_` + `friction_removal` is the cheapest way to comply.
>
> `mechanism` carries five jobs — batch dedupe (`pipeline.py:549`), SHIFT exhaustion
> (`loop.py:113`), warm-start payload, per-Pro suppression, LCM export — four needing *high*
> cardinality. An aggregation reader was pointed at a novelty key.
>
> **Do not close `mechanism`.** Add a second field: `theme`, closed, aggregation-only.

---

## 04 Themes

All 109 winners classified on one rule: **what product surface is the Pro asked to touch**,
read from `pro_facing_concept` — not `mechanism`.

*Why the concept:* mechanism tokens are adjectives (`friction` ×58, `workflow` ×34); concept
tokens are surfaces (estimate 51, invoice 42, payment 29). One live winner pairs mechanism
*"…employee GPS tracking"* with concept *"You have an estimate sitting in your queue…"*.

| Theme | n | Ask | Bound event | Sends/day | Days to n=175 |
|---|---|---|---|---|---|
| `estimate_approval` | 41 | Send estimate/proposal for digital approval | `Estimate sent` | 9.40 | 22 |
| `payment_collection` | 29 | Set up/use a payment rail | `Payment submitted` | 6.65 | 31 |
| `online_booking` | 12 | Let customers self-book | `Online booking enabled` | 2.75 | 53 |
| `price_book` | 9 | Build reusable service/price catalog | `Pricelist updated` | 2.06 | 65 |
| `invoice_send` | 8 | Create/convert/send an invoice | `Invoice sent` | 1.83 | 70 |
| `service_agreements` | 4 | Package recurring work | `Service agreement created` | 0.92 | 102 |
| `other` | 6 | None of the above — stays legal | — | — | — |

94.5% coverage; no seventh cluster reaches n≥3. Days column assumes the 10–15% exploration
reserve is **inverse-filled** at the smallest cells — costs the head ~3 days, pulls
`service_agreements` from 190 to 102.

### Boundaries

- `invoice_send` vs `payment_collection` — strip the benefit clause, keep the ask.
- `price_book` vs `estimate_approval` — configure once vs send one document.
- `online_booking` vs `estimate_approval` — customer-initiated, no existing job, vs
  Pro-initiated on a live job.

### Implementation

Generator emits `theme` as a `Literal` in the same JSON — no classifier (a classifier costs a
call, rejects only after the winner exists, and adds a second version axis). `other` stays
legal with a **<10% per run** alarm.

> **Two traps**
>
> Prompt must say **"theme is NOT a diversity key; ideas MAY share a theme"** — otherwise the
> model reads the list as a second novelty constraint and sprays themes, making the scoreboard
> a lie.
>
> `handoff.py:234` already sends a wire field `theme` carrying `pro_facing_concept` — the only
> field Allison's copywriter sees. Add `theme_slug`; never overload it.

---

## 05 Signals

**O** may drive learning · **D** explains, never decides · **G** can only stop · **A** defines
the unit.

| Signal | Source | Latency | Ev/day | Note | |
|---|---|---|---|---|---|
| `smsSend / arm` | Iterable | ≤1h | 50 | Exposure + every clock's origin. Arm currently discarded. | A |
| `first_return_at` | Amplitude | ≤24h | — | The primary. | O |
| `pre_active_days_28` | Amplitude | minutes | 50 | CUPED covariate + arm-balance test. **Snapshot at send.** | A |
| `post_active_days_7/30` | Amplitude | 7/30d | — | Construct check: session vs habit. | O |
| `critic_block_kind` | Waypoint | minutes | 250–600 | Highest-volume labelled signal by 2 orders of magnitude. Already in `candidates.critics`; needs a GROUP BY. | D |
| `intake_status` | Waypoint | hours | ~50 | Copy quality, days before any send — after fixing `handoff.py:43`. | O |
| `qa_pass_rate_by_theme` | Waypoint | hours | ~50 | Survivorship denominator. Must print beside any lift. | G |
| `qa_edited` | LCM | — | absent | Rewritten copy is not our treatment. | G blocking |
| `feasibility_block` | Waypoint | minutes | ~50 | Audience signal, upstream of message quality. Free. | O |
| `clicked_human` | Iterable | ≤1h | 1.3 | ~10 months to power. 87% of raw clicks are bots; control out-clicked us 3:1. | D |
| `t_mechanism_action` | Amplitude | ≤24h | ~4/theme | Question 4. Different scales per theme — cannot rank themes. | D |
| `bounce / skip / device-test` | Iterable | ≤1h | ~55 | Corrects the denominator. `delivered:true` is currently asserted unconditionally. | G |
| `unsubscribed` | Iterable | ≤1h | 0.5 | Asymmetric — 1 unsub is actionable where 1 click is not. | G |
| `warm_start_outcome` | Waypoint | minutes | ~50 | Hit rate + score distribution. Log line only today. | D |
| `touches_per_pro_30d` | Waypoint | — | must be 0 | `uq_winners_run_pro` is per-*run*; nothing prevents overlap. | G |

### ACTIVE_V1

Rule: **qualifies only if it could not fire without the Pro deciding to act.** Excludes passive
delivery, impressions, generic nav, vendor telemetry, `[Iterable] *` echo, customer-side events.

```
Logged in · App opened from deeplink · Invoice sent · Invoice saved · Estimate sent
Estimate created v2 · Job created server · Job scheduled server · Job started server
Job completed · Payment submitted · Customer added · Message sent · Review requested
Line item added v2 · Proposal sent · Clocked in · Card on File Requested
```

`Application Opened` **excluded pending V1** — if it fires on session restore, 7d return ≈100%
and the metric is dead. Add back if p95 ≤5/day and 7d incidence <0.70.

**Built from 633 of 828 events — see §06.** Re-run the rule over the full catalog before
ratifying.

**Dropped as unmeasurable:** `feature_activations` (no event means "feature attached") ·
`online_booking_usage` (`Online booking enabled` is a one-time settings write; no event
represents a booking arriving).

---

## 06 Proving it learns

Every number here depends on calls returning what we think they return, against events that
actually fire. None of that is verified today.

### The catalog is 76% seen

> **633 of 828**
>
> **195 live Amplitude events have never been looked at.** `ACTIVE_V1` and the deny-list were
> built from a partial alphabetical listing. An omitted active-use event depresses the return
> rate; an omitted passive event inflates it.
>
> Pull the full taxonomy (`GET /api/2/events/list`) and re-run the membership rule over all 828
> **before** ratifying `ACTIVE_V1`. Blocking.

### Every binding proven live

A bound event that never fires is silently dead — the theme is unmeasurable and nothing errors.
Run once per bound event (18 in `ACTIVE_V1` + 6 theme-bound), record weekly uniques:

```bash
curl -u "$AMP" -G '.../api/2/events/segmentation' \
  --data-urlencode 'e={"event_type":"Estimate sent"}' \
  --data-urlencode 'start=20260701' --data-urlencode 'end=20260825' \
  --data-urlencode 'm=uniques' --data-urlencode 'i=7'
```

Reject any binding under ~50 weekly uniques project-wide — it cannot support a per-theme rate.
The recorded number is the evidence the catalog is real.

### Failure must be loud

The failure that cost two days: every node green, nothing produced, no error to read.
**"Nothing to do" and "silently broken" must not look alike.**

| Condition | Required behaviour |
|---|---|
| Work list empty | Named error, not an empty result |
| Work list non-empty, zero matched | **Named error.** The case that bit us — identical to success |
| Export returns zero rows for a window with known sends | Named error |
| Amplitude lookup fails | Row stays NULL, counted, alerted. *Never* scored `returned: false` |
| `poll_truncated` / `pre_window_truncated` | Alert; drop from depth metrics, keep for the primary |
| Any §07 guardrail breached | Alert; pull the theme or arm |

### The closed-loop test

The only real answer to "is it learning". A CI test, not a manual check — inject a synthetic
outcome, assert it changes the next prompt:

1. Insert a `touch_outcome`: known `theme`, `arm='A'`, `routing='route-to-pro'`, a return timestamp.
2. Run the rollup — assert `theme_stats` has that theme at n=1.
3. Assert `pattern_summaries` groups it by `theme`, not `mechanism`.
4. Assert `evidence_block()` output contains it.
5. Assert a generation run receives that text in `evolve_prompt` **and** `ranker_prompt`.
6. Insert a losing theme (`ci_high < 0`, n ≥ MIN_N); assert it reaches the `forbidden` slot and
   is dropped from `warmstart.retrieve`.
7. Assert a winner whose theme has not cleared MIN_N is *not* promoted.

**If this test cannot run, the loop is not closed** — whatever a dashboard shows. It is also the
regression guard: every one of the five breaks in §03 fails at least one step.

---

## 07 Primary metric

```
Covariate-adjusted Cox PH on time-to-first-return, arm A vs POOLED arm B, tau=30d.
```

- **Population.** `routing='route-to-pro'` · `evidence_limitation IS NULL` · not bounced · not
  skipped · not device test · `amplitude_user_id` present · `arm IN ('A','B')`.
- **Origin.** `sent_at` = Iterable `smsSend.createdAt`.
- **Event.** First `ACTIVE_V1` at `≥ sent_at + 60s`. Events under 60s neither end the wait nor
  remove the subject (different clocks).
- **Censor.** `min(sent_at+30d, data_asof, next_touch_at, subscription_cancelled_at)`;
  `data_asof = min(sources) − 6h`, one value per analysis. **Unsubscribe does not censor** —
  that would make the arm with more unsubs look faster.
- **Model.** `arm_A + log1p(pre_active_days_28) + pre_missing`, strata `journey_window`, Efron ties.
- **Report.** KM curves, median days-to-return, RMST at 7/30. Never an HR on a dashboard.
- **Stop.** One analysis at 342 events; optional interim at 171, O'Brien–Fleming. Monthly cadence.

| Design | Effect | n / events | Calendar |
|---|---|---|---|
| binary `returned_7d` | +10pp | 396/arm | 23d |
| binary `returned_7d` | +3pp realistic | 4400/arm | 183d |
| `active_days_7` + CUPED | 0.4d | 310/arm | 19d |
| **Cox τ=30** | HR 1.3 | 342 events | 16d |

**Why Cox wins:** not per-subject efficiency — partial follow-up counts immediately. A touch sent
yesterday adds a person-day today; a binary adds nothing until its horizon closes. It also detects
pure timing effects that `returned_30d` cannot see, and is unbiased on a cohort still accruing.

**Cost:** schema + polling, not analysis — `t_first_return` is a superset of `returned_7d` (write
the timestamp, derive the booleans). One dep (`lifelines`). ~750 `useractivity` calls/day. Real
cost is discipline: a daily-moving target invites peeking.

**Guardrails** (breach pulls a theme, no statistics): unsub ≥3 or ≥2× pooled · bounce >10% ·
skip >15% · `evidence_limitation` >5% · any `routing_conflict` · Amplitude unresolved >5% ·
truncation >1% · any `touches_per_pro_30d` >1 · theme QA pass <60%.

> **QA pass rate is never a promotion input**
>
> Humans reject non-randomly by theme, so lift is conditional on surviving a human. A low-QA theme
> is **unevaluated, not negative**. Lift without its QA rate on the same row is uninterpretable —
> the failure most likely to produce a confident wrong answer.

---

## 08 Schema

`touch_outcomes` has zero rows — all free now.

> **Time-critical**
>
> `useractivity` returns ≤1000 most-recent events, **no server-side time filter**. For a Pro at
> 200 events/day that reaches back 5 days, not 28. The pre-period for active Pros is gone in three
> months. Every send before `pre_active_days_28` exists is unmeasurable — no baseline to compare
> against.

```sql
-- exposure
arm text not null default '' check (arm in ('A','B',''))
message_id text · sent_at timestamptz not null · sent_hour_local smallint
routing text not null -- REQUIRED at boundary · theme text not null
mechanism text not null default '' -- unchanged · is_device_test boolean

-- pre-period: NOT reconstructable later
amplitude_user_id text · pre_active_days_28 smallint
pre_window_events jsonb · pre_window_truncated boolean · pre_snapshot_at timestamptz

-- clocks
first_return_at timestamptz · first_return_event text · t_first_return_s bigint
return_censored_at timestamptz not null · return_censor_reason text not null
next_touch_at timestamptz · post_active_days_7/_30 smallint
last_polled_at · poll_cursor_event_time · poll_truncated boolean

-- delivery
bounced boolean · send_skip_reason text · first_human_click_at timestamptz
click_bot_count smallint · unsubscribed_at timestamptz

-- provenance
indicators jsonb + GIN · qa_edited boolean -- NULL = unknown
intake_status text · theme_vocab_version text
```

On `winners`: `theme`, `warm_start_retrieval` jsonb, `shipped_block_kind`. Per analysis run:
`data_asof`, source asofs, `event_set_version`, `theme_vocab_version`. Keep `returned_*` booleans,
**derived from the timestamp**.

---

## 09 Contracts

### Amplitude

Project API Key + Secret, Basic auth, **Dashboard REST API** (`amplitude.com/api/2/*`),
read/export scopes. Explicitly *not* the ingestion key (`api2.amplitude.com`) — that separation is
the security answer. Confirm region (EU = `analytics.eu.amplitude.com`; wrong host looks like a bad key).

```bash
curl -u "$AMP" -G '.../api/2/usersearch' --data-urlencode 'user=<pro_id>'   # V3
curl -u "$AMP" -G '.../api/2/useractivity' --data-urlencode 'user=<amplitude_id>' \
     --data-urlencode 'limit=1000'                                        # V1
curl -u "$AMP" '.../api/2/events/list' | grep -i iterable                 # V6
```

### Iterable

Server-side key, **data-export read only** — no `campaigns.send`, no `users.update`. Narrower than
the key in use today. Datetimes encode spaces as `%20`, not `+`.

```
GET /api/campaigns                                    # V5: is arm B elsewhere?
GET /api/export/data.json?dataTypeName=smsClick&campaignId=19156972
GET /api/export/data.json?dataTypeName=smsReceived    # does it carry campaignId?
```

### Allison / LCM

Preferred: one webhook POST per row to existing `/api/outcomes` when a row's fate is decided —
needs no Waypoint id in Iterable, collapses 1–5 into one ask.

| Field | Breaks without it | Effort |
|---|---|---|
| `arm` | Lift not computable | Low — already stamps `lcmVariant`; ask is a guarantee |
| `edited` | Rewritten copy silently credited to our theme | Medium — bool alone suffices |
| `rejection_code` | QA survivorship invisible; lift uninterpretable | Medium — a dropdown |
| `sent_at` | No clock → permanently unmeasurable | Trivial |
| `routing` | We infer it by inspecting recipients | Low |
| `theme_slug` | — | Her call — add alongside `theme_category` |

---

## 10 Feedback

`evidence_block()` feeds both `evolve_prompt` and `ranker_prompt` (`pipeline.py:930-931`) —
re-graining it reaches generation and selection with no new plumbing.

```
touch_outcomes -> nightly GROUP BY theme -> theme_stats
   +-> pattern_summaries  GROUP BY (channel, theme)   <- was mechanism
   +-> losing_themes()  [NEW]  ci_high < 0 AND n >= MIN_N -> existing forbidden slot
   +-> warmstart.retrieve: drop losing themes; promote only if theme clears MIN_N
```

1. **Re-key to theme.** ~20 lines, 2 files, 1 migration — not one line. Worthless until outcomes
   exist (GROUP BY over 0 rows = 0 groups).
2. **Add suppression.** `warmstart.py` only promotes today; the one negative path can never fire.
3. **Gate promotion on theme evidence.** Today one 7d return on one touch seeds every similar run
   cross-org. At p≈0.45 half of first touches promote noise. Until a theme clears MIN_N the system
   runs cold — correct.

**Reserve 10–15% of arm A as forced cold starts** (suppress warm-start + evidence injection),
inverse-filled at the smallest cells. Suppression without it is self-fulfilling — a suppressed theme
can never earn its way back.

---

## 11 Build order

| | What | Unblocks | Size |
|---|---|---|---|
| 0 | Run V1–V6 | Every date | <2d |
| 0b | Pull all 828 events; prove every binding fires (§06) | Honest `ACTIVE_V1` | 1d |
| 1 | Close `theme`; generator emits it | All aggregation | 2–3d |
| 2 | Amplitude → `touch_outcomes`: clocks, arm, pre-window; `routing` required | **Everything** | 2–3w |
| 3 | One touch/Pro/30d at audience selection | Clean corpus | 2–3d |
| 4 | Delete measurement call → theme lookup | Cost, one failure mode | 1d |
| 5 | Four Iterable defects + loud-failure rules (§06) | Honest delivery; silence stops looking like success | 3–4d |
| 6 | Re-key evidence readers + **closed-loop CI test** (§06) | Feedback, provably | 2d |
| 7 | Scoreboard, QA rate on every row | Interpretation | 3–4d |
| 8 | `losing_themes()` + cold-start reserve | Suppression | 2–3d |
| 9 | Promotion gated on theme evidence | Stops cross-org noise | 2d |

**Parallel, unblocked now:** critic `block_kind` GROUP BY · fix `handoff.py:43` · warm-start
telemetry from the existing log line.

### First three PRs

1. **`theme_vocab_v1`.** *Accept:* pydantic rejects unknown theme at parse · dedupe/SHIFT/warm-start
   still key on `mechanism` · 100% themed, `other` <10% · no batch-size regression.
2. **Record an outcome.** *Accept:* rows >0 · arm populated for A *and* B · `pre_snapshot_at` within
   24h of `sent_at` · guardrailed send labelled and invisible to `pattern_summaries` · missing
   `routing` rejected, not stored.
3. **Delete measurement call.** *Accept:* zero `stage="measure"` calls · every winner still has a
   measurement row · no-theme winner still blocked · net lines negative.

### Calendar

| Answer | When |
|---|---|
| Critic rates, abstention, honest QA rate | this month |
| Does personalization beat control? | ~5–7 weeks |
| `estimate_approval` / `payment_collection` | ~7–8 weeks |
| `online_booking` / `price_book` / `invoice_send` | ~11–13 weeks |
| `service_agreements` | ~18 weeks |

Assumes V1's numbers hold. They are assumptions.

---

## 12 Keep vs undo

Branch point `8b4fe7e`. **Nothing needs reverting** — no schema or behaviour is wrong, and zero rows
means nothing is contaminated.

| Commit | Work | Verdict |
|---|---|---|
| `42afc76` | `outcomes.py` resolver + evidence gate (+206) | Keep — 101/105 verified |
| `42afc76` | Migration `0007` (`routing`) | Keep — gate needs it |
| `42afc76` | `funnel.py` +257, `test_funnel.py` | Keep — survives the port |
| `42afc76` | `models.py` natural key, `auth.py` token, tests | Keep |
| `42afc76` · `162e05b` | `docs/n8n/*` (989 lines) | Retire, don't revert — measurements are documentation |
| `162e05b` | Campaign scoping | Keep, verify V5 |
| pre-existing | Measurement LLM call | Delete in PR3 |

Full rollback if ever needed: `git revert --no-commit 162e05b 42afc76 && git commit` ·
`alembic downgrade 0006` · disable both n8n triggers. `OUTCOMES_TOKEN` unset reverts endpoints to
cookie-only.

---

## 13 Traps

### Most likely wrong

- **Base rate.** `p₇=0.45` invented. V1 settles it and yields σ, ρ free.
- **QA rewriting.** Diff 50 sent bodies against `pro_facing_concept`. >20% rewritten → no per-theme
  number is interpretable until `qa_edited` exists.
- **Randomization integrity.** Compare `pre_active_days_28` across arms — the best falsification test
  here, free once §07 lands.
- **Daily users dilute.** Pre-specify one subgroup, `pre_active_days_28 ≤ 3`, and never expand it.
- **Effect shape.** Primary win with no `post_active_days_30` movement = "bought a session, not a
  habit". Pre-register that reading.

### Do not

- Close `mechanism` · build a control cell per theme · backfill the 109 winners · build the
  14-indicator pipeline.
- Put clicks, persona scores, critic block rate, or QA pass rate in any promotion/suppression predicate.
- Theme × segment (~0.5/day/cell) · a weekly scoreboard someone acts on · fit the 14 similarity weights.

### Two decisions no document can make

1. **Ratify the six themes.** Derived from real data, has a default, still needs a person. One meeting.
2. **Allison agrees the contract.** `arm` and `routing` near-free; `edited` and `rejection_code` need
   work in her app. Without `edited`, per-theme conclusions carry unquantified contamination.

---

Sources: live Iterable + staging Waypoint 25–26 Aug 2026 · Amplitude catalog, 633 of 828 events seen ·
all 109 staging winners · `measurement/outcomes/evidence/warmstart/pipeline/prompts/handoff/funnel.py` ·
LCM engagement audit + Omni metrics note, 18 Aug 2026.

Calibration figures are Allison's. σ, ρ and base rate are stated assumptions, unverified until V1.
