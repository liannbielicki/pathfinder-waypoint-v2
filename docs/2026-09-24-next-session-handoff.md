# Waypoint — Next Session Handoff (2026-09-24)

Written at the end of the session that shipped `580c668`. Assumes you have **no
prior context**. Read "Gotchas" before touching anything — several of them cost
the previous session real time.

**Branch:** `V4-Improvements` · **Worktree:** `.claude/worktrees/pathfinder-waypoint-v4`
**Code commit:** `580c668` · this doc is updated separately; see `git log` for the tip.

```bash
cd services/api && .venv/bin/python -m pytest -q        # 804 passed, 2 deselected
cd services/api && .venv/bin/ruff check src tests       # clean
cd services/api && .venv/bin/mypy src                   # clean, 39 files
cd apps/web && pnpm vitest run                          # 122 passed
```

Do **not** run `ruff format` — the repo is not format-clean at unmodified HEAD.

---

## 1. What just shipped

Waypoint runs an evolutionary loop per Pro: generate candidate outreach ideas
via LLM → gate → rank → score the best with an LLM persona panel → keep a
champion → repeat until a stop condition. Five defects were diagnosed from
production run `4e7cb9fd` and fixed in `580c668`.

| # | Defect | Status |
|---|---|---|
| 1 | The loop never changed approach — patience never advanced, every Pro burned to its round cap | **Fixed** — verified live, but see §1.5 |
| 2 | The win bar sat below the measurement's resolution — the smallest possible improvement always won | **Fixed** — verified live |
| 3 | Early-stop threshold unreachable | Out of scope (settable in the UI) |
| 4 | Every Pro scored on the global churn baseline | **Deliberately still disabled** — see §4.4 |
| 5 | One bad LLM response destroyed a Pro's finished work | **Fixed** — verified live |
| — | Jobs stalled and were reaped with no recorded reason | **Fixed** — not yet exercised at load |

Live verification (Task 7) has since run. **Read §1.5 before planning anything** —
it confirms three fixes and surfaces four new issues.

The full diagnosis for each lives in `580c668`'s commit message. The plan and
its evidence are at
`docs/superpowers/plans/2026-09-23-loop-reliability-and-scoring.md`.

### Key mechanisms now in place

- **`apply_round` takes the round's `mode`** (`cold`/`refine`/`shift`). It no
  longer tries to recover that decision by string-comparing the model's
  free-text `mechanism` label — the model rewords it every round, which is why
  `tries_on_current` pinned at 1 and `MAX_NO_IMPROVE` could never fire.
- **`is_win` gates on the panel's mean reaction**, not percentage points,
  floored at `max(KEEP_DELTA_REACTION, 2.0 / panel_size)`.
- **Generation failure ends the loop and ships the champion.** Rate-limit
  exhaustion still fails loudly — a 429 storm means `MAX_LLM_IN_FLIGHT` is too
  high for the tier, which is an operator signal, not a model failure.
- **The lease is renewed inside the round** — per paid attempt, per slot-queue
  poll, and between context-flow retries. `LLM_TIMEOUT_SECONDS` (300s) and
  `max_retries=0` bound each call.

---

## 1.5 Task 7 results — run `acc26e53895e463dac6e22e73a3da541` (2026-09-24)

Same audience lineage as the baseline (`workbench:promotion-d1a0d4e84f97318b`),
`MAX_ROUNDS=15`, `MAX_NO_IMPROVE=5`, `PATIENCE=2`, `KEEP_DELTA_REACTION=0.4`,
`KEEP_DELTA_PP=0.2`. Cost `$2.59`.

| Metric | Baseline `4e7cb9fd` | Predicted | **Actual** |
|---|---|---|---|
| Run status | `degraded` | complete | **`complete`** ✅ |
| Pros decided | 3 of 4 (1 failed, no result) | 4 of 4 | **4 of 4** ✅ |
| Verdicts | 2 winner / 1 no-action | more no-action | **4 winner / 0 no-action** ❌ prediction wrong |
| Rounds used | 12, 14 of 15 | materially fewer | **15, 13, 10, 9** ⚠️ barely moved |
| Candidates per round | 3 rewordings of one lever | distinct levers | **8 distinct mechanisms on `553067`** ✅ |
| `baseline confidence` | `global` | still `global` | **`global`** ✅ (issue 4 still off) |
| RECO | unavailable | unavailable | **unavailable on all 4** — §4.5 still blocked |
| Persona `fit` | 1.00 everywhere | — | **1.00 on all 20 seats** — §7 still true |

### Confirmed fixed

**Issue 5.** Four of four Pros decided, run `complete` rather than `degraded`.
Per *decided* Pro the run was also cheaper: `$0.65` vs `$0.72`.

**Issue 2 — with two concrete proofs.** Every observed pp value lands exactly on
the 3-panel lattice, and two rounds were rejected that the old bar would have
accepted:

- `553067` r7: `task_completion_coaching` scored **4.1 pp against a 3.3 pp
  champion**. That is a 0.8 pp delta — it would have **won** under the old
  `KEEP_DELTA_PP=0.6`. It is one lattice step (mean 5.67 vs 5.33), correctly
  rejected.
- `464057` r4: **2.4 pp against a 1.4 pp champion** — a 1.0 pp delta, also one
  step (5.00 vs 4.67), correctly rejected.

Both wins that *were* accepted (`553067` r8, `227626` r5) are exactly two steps.
The gate behaves as designed.

**Issue 1 — the mechanism churn is real.** `553067` cycled through eight
distinct mechanisms across 15 rounds (`visibility_into_usage_pattern` →
`billing_confidence_reinforcement` → `job_completion_velocity_benchmark` →
`task_completion_coaching` → `mobile-field-first-activation` →
`payment_completion_momentum` → `competitive_speed_positioning` →
`self_serve_customer_conversion_enablement`). The baseline produced three
rewordings of a single payment lever. SHIFT is working.

### Four new issues this run surfaced

**A. `MAX_NO_IMPROVE=5` is too loose to fire — the rounds barely dropped.**
Replaying `553067`'s exact 15 rounds through the shipped loop code: `dry`
reaches **4** and the run ends on `round_cap`. Before this work `dry` was
permanently 0, so the mechanism is now working — the setting just never trips
inside the cap, because each win resets `dry` to 0.

```
MAX_NO_IMPROVE=5  -> round_cap at 15 (dry=4)   <- what ran
MAX_NO_IMPROVE=4  -> no_improve_exhausted at 14
MAX_NO_IMPROVE=3  -> no_improve_exhausted at 13
```

This is a **settings change, not a code change**. Try `MAX_NO_IMPROVE=3` on the
next run. Rounds 9–15 on `553067` produced nothing better than round 8.

**B. Suppression is high and is being paid for.** `475567` had **6 of 10 rounds
suppressed** — never panel-evaluated. `553067` had 4 of 15. Suppressed rounds
still pay for generation and the critic before being binned. Nobody has looked
at *why* the critic is rejecting this much; the `critics.block_kind` column on
`CandidateRow` holds the reason and is the place to start. This is the single
biggest cost lever visible in the run.

**C. The held-out final panel scores consistently lower than the screen.**

| Pro | Screen (3-panel) | Final (5-panel) |
|---|---|---|
| `553067` | 4.9 pp | **3.0 pp** |
| `227626` | 4.1 pp | **1.8 pp** |
| `475567` | 2.4 pp | **1.2 pp** |
| `464057` | 1.4 pp | 2.4 pp |

Three of four drop by roughly half. If the screen panel is systematically
optimistic, the loop is optimising against a biased estimate and the shipped
number is the more pessimistic one. Worth checking whether the two panels draw
from different persona pools or whether the 5-panel's backfilled seats drag the
mean down. Not urgent, but it undermines confidence in the screen estimate.

**D. The `PATIENCE` label is wrong (open decision).**

A mechanism can visibly run 4–5 consecutive rounds even at `PATIENCE=2`, which
looks like the loop fix failing. **It is not.** `PATIENCE` counts *consecutive
non-improving* attempts, and a **win resets the counter to zero**. Replay of
`227626` through the shipped code:

```
r3  recurring_revenue  shift   win    tries 2 -> 0
r4  recurring_revenue  refine  lose   tries 0 -> 1
r5  recurring_revenue  refine  win    tries 1 -> 0   <- reset
r6  recurring_revenue  refine  lose   tries 0 -> 1
r7  recurring_revenue  refine  lose   tries 1 -> 2   <- shifts next
```

Five rounds on one mechanism, never more than two failures back to back. The
rule is: **keep refining while it keeps winning; two losses in a row and it is
abandoned.**

The win-reset (`tries_on_current=0` in `apply_round`'s win branch) exists at
`5443e39` and **predates this work** — the loop fix only changed how the counter
increments on a *loss*.

**The defect is the UI label.** Both `RunStart.tsx:24` and `RunStatus.tsx:25`
call it *"Refine attempts per mechanism"*, with help text *"Tries a mechanism
gets before the loop shifts to a new one."* Both promise a hard cap of 2 total.
An operator reading that will conclude the loop is broken when it is not.

**Undecided — the user was asked and has not chosen:**
- *Fix the label only* (recommended). Abandoning a mechanism that just improved
  is perverse, and a win requires a genuine two-step gain so it cannot churn
  indefinitely. Text-only, zero risk.
- *Add a hard total cap per mechanism* — what the label currently promises.
  Would have cut `recurring_revenue` from 5 rounds to 2 on `227626`. Tightens
  the round budget at the cost of dropping ideas mid-improvement.

Note this is the real lever on rounds-per-Pro — `recurring_revenue` took 5 of 13
rounds on `227626`, and `billing_confidence` + `task_completion` took 8 of 15 on
`553067`. `MAX_NO_IMPROVE` (issue A) is the coarser second dial: it counts how
many mechanisms have gone dry, and stops the Pro at 5.

### A prediction that was wrong

I told the user to expect **more `no_action`** from the tightened bar. The run
produced **zero**. The reason: the tightened bar only governs *replacing* a
champion. The **first** win is unchanged — it needs only to clear the
`MIN_REDUCTION_FLOOR_PP` (1.0 pp) support floor. Every Pro won early (rounds 2,
3, 2, 2), so every Pro shipped. Do not expect the reaction gate to change the
winner/no-action ratio; it changes how often the champion is replaced.

---

## 2. Gotchas

**You are on `V4-Improvements`, not `v5`.** These branches diverged; `v5` is
*behind*. The previous session read v5's source **three separate times** while
working here and drew wrong conclusions each time (`prompts.py` mode
vocabulary, `personas.py` `MIN_PANEL_SIZE` twice). Always confirm which file
you are reading.

**The screen panel seats 3 personas, not 5.** `_panel_for(state, deps, brief, SCREEN_PANEL_SIZE)`
drives every win/lose decision. The 5-persona panel is the held-out *final*
check and decides nothing. The previous session analysed the 5-panel by mistake
and shipped a no-op change (`KEEP_DELTA_PP` 0.5 → 0.6) before catching it. One
persona-step on the deciding panel is worth **0.82–1.33 pp**, not 0.556.

**`KEEP_DELTA_REACTION` is a floor, not a free knob.** The effective bar is
`max(KEEP_DELTA_REACTION, 2.0 / panel_size)`, so on a full 3-panel it is always
0.667 and lowering the setting below that does nothing. This is deliberate —
an operator setting 0.3 would otherwise re-admit the single-persona win the
change exists to prevent. Documented in `is_win`, `DEFAULT_LOOP_CONFIG`, and
the `RunStart.tsx` help text.

**The load suite is red, and it is not your fault.** `pytest -m load` fails on
this branch *and* on `5443e39`, before any of this work. Verified by running it
in a worktree at the pre-plan commit — identical failure. See §4.2.

**Two CI gates have a hole.** `pyproject.toml`'s
`addopts = "-m 'not live and not load'"` deselects two whole modules, and
`mypy` is scoped to `src` only, not `tests`. A protocol change broke an
unmigrated implementer in `test_load.py` with **both gates green**. See §4.3.

---

## 3. Decisions already made — do not re-litigate

The previous session made 11 recorded rulings. These four change what you
should build:

1. **Issue 4 was reverted, not fixed.** Composing a calibration cell key was
   shipped, then reverted, because the key can never match production data.
   Shipping code that looks fixed and does nothing was judged worse than an
   honest `return None`.
2. **Rate-limit failures propagate and fail the job.** They are explicitly
   excluded from the "ship the champion" path.
3. **No database migration** was permitted. `mean_reaction` therefore rides in
   the existing `ranking` JSON column rather than its own column. A migration
   is the correct long-term home and the JSON already holds the value, so the
   move is non-destructive whenever someone wants it.
4. **`KEEP_DELTA_REACTION` as a floor** (see Gotchas).

---

## 4. Open work

**Recommended order after Task 7** (the subsection numbers are stable labels,
not the order to work in):

1. **§4.3 first half** — set `MAX_NO_IMPROVE=3` and re-run. Free, and it
   finishes the unproven half of issue 1.
2. **§4.1** — close the CI gap. 20 minutes, and it protects everything after.
3. **§4.3 second half** — the suppression rate. Biggest visible cost lever.
4. **§4.5 `2D`** — a population-level claim shipped as per-Pro fact in a real
   handoff. Correctness, independent of the channel work.
5. **§4.5 `2A`/`2B`** — two Snowflake queries that unblock channel selection.
6. **§4.2** — the `MissingGreenlet` under load.
7. **§4.5 `2C`** and **§4.4** — real projects, both needing decisions first.

### 4.1 — Close the CI gap (≈20 min)

Smallest item, and it protects everything after it.

- Add a CI job running `pytest -m load` (it will be red until §4.2 — wire it
  as a separate, visible job rather than blocking the main suite).
- Extend `mypy` to `tests/` (non-strict is fine). This alone would have caught
  the `test_load.py` regression at the type gate.

Files: `pyproject.toml`, `.github/workflows/`.

### 4.2 — Fix the `MissingGreenlet` under load

A **live concurrency bug**, currently masked because the retry path rescues it.

```
ATTEMPTS  Counter({1: 194, 2: 6})
REASON    {'reason': 'unhandled at evolve: MissingGreenlet(...)'}   x6
```

Six of 200 jobs hit it at the evolve stage under 4-worker concurrency, get
checkpointed as failures, get requeued, and succeed on attempt 2. Pre-existing
— confirmed at `5443e39`.

**Fix the assertion message too.** It currently reads
`"a live lease was double-claimed"`, which is **false** — no lease was double
claimed — and sent the previous session hunting through lease code first.

Approach: run `-m load`, find which awaits in the evolve stage touch a
SQLAlchemy session across a task boundary, fix the one that does. Note that
`MissingGreenlet` typically means a lazy attribute load fired outside the async
context; `make_session_factory` sets `expire_on_commit=False` (`db.py`), so
look for a path that bypasses that or shares a session across tasks.

Reproduce: `cd services/api && .venv/bin/python -m pytest tests/test_load.py -m load -q`

### 4.3 — Task 7 is DONE. Act on what it found (§1.5)

Two follow-ups, in order:

**Tune `MAX_NO_IMPROVE` to 3 and re-run.** Free — a settings change on the start
form, no code. Rounds 9–15 on `553067` bought nothing; at `MAX_NO_IMPROVE=3` the
loop would have stopped at round 13 on `no_improve_exhausted`. Confirm on the
next run that at least one Pro stops for that reason rather than `round_cap`.
That is the last unproven half of issue 1.

**Investigate the suppression rate.** `475567` lost 6 of 10 rounds to the critic
without a single panel evaluation, and each one was paid for. Start from
`CandidateRow.critics["block_kind"]` for that run and count the reasons. If one
block kind dominates, it is either a prompt problem or a gate that is too
strict — both cheap to fix, and it is the largest visible waste in the run.

### 4.4 — Issue 4: per-Pro churn baselines

Blocked on a decision, not on code.

`OrgBrief.calibration_cell()` returns `None`, so every Pro scores against
`global_baseline = 0.0869` and all 166 per-cell baselines (range 0.040–0.362)
go unused.

**Why it cannot simply be switched on:** the live flow emits tenure as
`under_1y / 1_2y / 2_4y / over_4y` (the `CASE` on `oi.tenure_mo` in
`n8n/waypoint-context-snowflake-v1.json`); the cards key on
`0-3m / 4-12m / 13-36m / 37m+`. **Zero overlap.** Do not translate in Python —
`under_1y` spans both `0-3m` and `4-12m`, whose baselines differ materially, so
a mapping would silently pick a wrong baseline.

**The fix belongs at the n8n flow**, and it ripples:
- `tenure_band` also feeds persona matching (`_MATCH_FEATURE_MAP` maps it to
  `tenure_bucket`) and warm-start fingerprints (`warmstart.py`). The persona
  cards use the **card** vocabulary, so today tenure never matches a persona —
  a contributor to every panel member scoring `fit 1.00`.
- Changing it alters stored warm-start fingerprints.

**And the constants must be re-tuned with it.** `KEEP_DELTA_PP` (0.6) and
`MIN_REDUCTION_FLOOR_PP` (1.0) are fixed pp values tuned on the single global
baseline. Measured against the artifact at r=4.8: **23 of 166 cells** have two
reaction steps worth under 0.6 pp (minimum 0.044 pp, `1D|max|37m+`), and
**11 of 166 cells cannot reach the 1.0 pp support floor at all**, even at a
perfect 7/7/7 panel. Restoring cell baselines without re-tuning would make
those Pros structurally unwinnable — silently, as `no_action`.

This warning is already in the `calibration_cell()` docstring.

### 4.5 — Phase 2: RECO and channel selection

Full detail: `docs/superpowers/plans/2026-09-23-loop-reliability-and-scoring.md`
line 927 onward (`2A` line 931, `2B` 946, `2C` 957, `2D` 963).

**2D first — it is a live correctness bug, independent of the rest.**
`pattern_summaries(session, journey_window, channels)` takes no `pro_id`; it is
population-wide and `evidence_block` labels it correctly as *"Observed outcomes
for similar pros."* But a shipped handoff rationale for Pro `842846` read:

> *"SMS showed strong returns **for this Pro** (82/121 at 1d, 116/121 at 7d)"*

The model read population evidence and narrated it as this Pro's own history,
and that sentence went to the LCM team. Two parts: tighten the prompt so
population evidence cannot be restated as per-Pro fact, and add genuine
per-`(pro, channel)` return evidence (`failed_mechanisms(session, pro_id)`
shows the per-Pro query shape already exists).

**2A and 2B are one Snowflake query each, and block 2C.** Until you know what
these columns actually contain, channel-first selection is unbuildable:

```sql
select distinct recommended_action, count(*)
from production.reco.channel_recommendations
group by 1 order by 2 desc;
```

`suggested_outreach_channel()` accepts only the literal strings `sms`, `email`,
`call` after lower/strip. The column is named `recommended_action`, not
`recommended_channel` — if it holds `SMS_OUTREACH` or `NO_ACTION`, it silently
becomes `None` and the winner card prints `RECO unavailable`, which is what
production shows today. Note also that the **main** context flow
(`waypoint-context-snowflake-v1.json`) never fetches RECO at all — it only
exists in the three `waypoint-variable-audit-context-*` flows, so RECO reaches a
run only as a promoted workbench variable.

For 2B, the consent gate fails **open** against a hardcoded guess:

```python
NEGATIVE_CONSENT = frozenset({"opted_out", "opted-out", "unsubscribed",
                              "suppressed", "dnc", "blocked", "revoked", "no"})
```

If Snowflake emits `OPT_OUT` or `N`, an opted-out Pro stays contactable at this
layer. (The audience SQL upstream and Iterable's DNC failsafe are the real
filters, so this is belt-and-braces — but it is belt-and-braces built on an
unverified word list.) Same `select distinct` treatment.

---

## 5. Where the channel design actually lives

Two docs, and they describe different things:

| Doc | What it is |
|---|---|
| `docs/superpowers/specs/2026-09-22-v4-loop-idea-quality-design.md:22` ("Channel and no-action safety") | **What ships today.** RECO is *advisory*: generation follows it unless the idea carries an explicit `channel_override_reason`, and the gate only checks that string is non-empty. |
| `docs/superpowers/plans/2026-09-23-loop-reliability-and-scoring.md:957` (`2C`) | **The proposal.** Decide channel deterministically before the loop and pin it per-Pro. |

There is no doc describing a channel-*picking* algorithm, because today there
isn't one — channel is a by-product of idea generation, chosen per-idea by the
model. `2C` is the proposal to make it a decision, weighing (1) the consent
gate, (2) RECO, (3) observed per-channel returns, (4) engagement bands
(`email_engagement_state`, `outreach_count_28d_band`). All four inputs already
exist.

Worth knowing for `2C`: because `mechanism` identity ignores channel while the
persona screen reframes substantially by channel, channel is currently a large
**uncontrolled** lever swinging the score between rounds. Pinning it makes
round scores comparable, which is a scoring fix as much as a targeting one.

---

## 6. Smaller deferred items

- `_panel_for`'s `size` parameter is typed `Any`, not `Literal[3, 5]`.
- `N8N_TIMEOUT_SECONDS < LEASE_SECONDS` is unenforced. Shipped default is safe
  (900 vs 1800); only an operator override breaks it. A `field_validator` is a
  one-liner.
- The heartbeat is an `UPDATE`+`COMMIT` on every 0.25s slot poll. Judged fine
  to ship (~32 tiny HOT updates/sec fleet-wide, only while saturated, never two
  connections on one row). If it ever bites, throttle to once every N seconds —
  the hook signature does not need to change.
- `_screen_finalists`'s `raise failures.exceptions[0]` picks the first exception
  arbitrarily; a `LeaseLost` can be demoted to `__cause__` if a sibling stack
  raised something else.
- `n8n/waypoint-variable-audit-context-by-org-uuid-v1.json` is untracked and
  predates this work. Nobody has decided whether it should be committed.

---

## 7. Persona panel fit — raised, never scoped

Every panel member in production reports `fit 1.00`, **including the
counterweights**, which is self-contradictory — a counterweight is supposed to
be the near-miss. `_fit` scores over keys present in *both* the Pro and the
persona card. The pool is already fetched by segment and `_adapt_persona`
explicitly injects `segment`, so if the live card's other keys do not land under
the exact names `plan` / `tenure_bucket` / `org_size_bucket` / `trade_bucket` /
`lifecycle_stage` / `open_ar_band`, the intersection is `{segment}` alone and
fit is `1/1 = 1.00` for everyone. Panel selection then degenerates to
alphabetical-by-`persona_id` within a family-diversity rule — every Pro in a
segment gets effectively the same jury.

This was inferred from production output, **not verified against a live
`persona-cards` payload**. Confirm before acting. It shares a root cause with
§4.4 (tenure vocabulary), so the two are worth investigating together.
