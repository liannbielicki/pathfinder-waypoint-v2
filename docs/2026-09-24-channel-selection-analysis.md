# Channel selection & consent — deep analysis

> **CORRECTION (2026-09-24, later the same day):** this was written assuming real runs use the
> Standard flow (`waypoint-context-snowflake-v1.json`). They don't — real runs use **Staging context**
> via the async workbench flow, and Standard is the fallback. §1–§3 and §5–§7 (Snowflake findings,
> RECO grain, DNC sources, loop identity, the §2D bug) still hold. **§4 and §8 are superseded**:
> the Iterable node lives only in the fallback flow, and in the Staging path any new field also needs
> a promotion rule or a promotion bypass. See `docs/2026-09-24-repo-audit-handoff.md` §2 and §7.

Branch `channel-selection-consent`, off `V4-Improvements` (d8df7d5).
Supersedes the framing in `docs/superpowers/plans/2026-09-23-loop-reliability-and-scoring.md` §2A–2D.

All Snowflake figures below were run live on 2026-09-24 via `snow sql -c hcp`.

---

## 1. The two blocking queries are answered

### RECO vocabulary is clean

```
production.reco.channel_recommendations
recommended_action | n
sms                | 221,762
email              | 103,174
call               |  81,628
suppress_all       |       1
```

Lowercase `sms`/`email`/`call` — exactly the literals `suggested_outreach_channel()`
(`services/api/src/waypoint/n8n.py:164`) already accepts. **The value format was never the
problem.** `suppress_all` is a single row and normalizes to `None`, which is correct by
accident, not by design.

### There is no consent source to query, because none is wired

The second query has no target. Both consent fields are structurally absent in production,
and the main flow's own SQL comments say so:

> `'email_consent_state' never had one, and 'sms_consent_state' was REMOVED 2026-08-19 — its
> source, communications_sms_consents, records the Pro's CUSTOMERS' marketing-SMS consent,
> not the Pro's own`

So `_consent_blocks()` (`feasibility.py:45`) reads `None` for every channel on every Pro and
returns `False`. **`NEGATIVE_CONSENT` has never matched anything in production.** Correcting
its vocabulary — the §2A task — changes nothing.

`waypoint-context-snowflake-v1.json` also contains no DNC, opt-out or suppression filter of
any kind. The plan's premise that "the audience SQL upstream is the authoritative DNC filter"
is **false for this flow**. Iterable at send time is the only real gate today.

---

## 2. RECO does not respect suppression — this validates consent-before-RECO

Row counts in `channel_recommendations` (one row per pro, 406,565 rows / 65,746 orgs):

| Conflict | Rows |
|---|---|
| `call` recommended into a DNC org | 5,645 |
| `sms` recommended to a Pro who affirmatively opted out of SMS | 1,185 |
| `email` recommended to an email-suppressed Pro | **0** |

RECO internalizes email opt-out and nothing else. It is a response-propensity model, not a
deliverability model. Treating it as the base layer and consent as advisory inverts the only
ordering that is legally and operationally safe.

The ordering in `pipeline.py:884` is already correct in *shape* — `infeasible_channel` is
evaluated before the RECO-override check, and the RECO rule is guarded by
`suggested_channel in channels` — it is simply starved of data on both sides.

---

## 3. `channel_recommendations_org` is the right table, and nobody is using it

Both the plan and the three variable-audit flows join the **pro-grain** table via the
founding admin. There is an org-grain table:

```
production.reco.channel_recommendations_org
65,746 rows / 65,746 orgs        -- exactly one row per org
scoring_date = 2026-09-24        -- current
_ORG_HIST: 72 distinct days over 72 calendar days (2026-07-15 .. 2026-09-24)
```

It refreshes every single day, is keyed on `organization_id` — which the Router Query already
emits — and carries `org_best_channel`, `union_p_email/sms/call`, and a **per-channel contact
pro** (`email_contact_pro`, `sms_contact_pro`, `call_contact_pro`, `call_contact_is_founder`,
`used_fallback_no_dm`).

The two grains disagree materially:

| channel | org-grain `org_best_channel` | pro-grain `recommended_action` |
|---|---|---|
| email | 47% | 25% |
| call  | 36% | 20% |
| sms   | 17% | 55% |

Waypoint keys runs by org. Picking the founding admin's row out of the pro table and calling it
the org's channel is a silent grain error — the org table has already done decision-maker
selection, and tells you when it fell back (`used_fallback_no_dm`, 0.4–0.9%).

**Trap:** `channel_recommendations_org.email_contact_optout` is a FLOAT *rate* (mean 0.12,
continuous), not a suppression flag. The boolean is `channel_recommendations.email_optout_suppressed`
on the pro table. Do not treat the org column as a gate.

---

## 4. The consent source is already in the flow — it is Iterable, not Snowflake

`waypoint-context-snowflake-v1.json` already contains an Iterable node:

```
Get a user | n8n-nodes-base.iterable | userId = {{ $json.pro_uuid }}
```

Its result is merged, PII-stripped, and returned. Iterable is the system that actually enforces
suppression at send, and its user record carries the channel/message-type unsubscribe state.
Waypoint then throws all of it away: `ALLOWED_FIELDS` (`n8n.py:34`) is a closed allowlist, and
the only consent names on it — `sms_consent_state`, `email_consent_state` — have no producer on
either side.

So the shortest correct path is not a new Snowflake join. It is to map Iterable's existing
response onto the two allowlisted names that already exist, in the `Table Structure for Merge`
code node. The gate, the block kind, the prompt text and the field names are all already built
for data that is one mapping away.

Snowflake remains the source for the one thing Iterable cannot tell us — **DNC for calls**.

---

## 5. DNC has no canonical registry, and that is a real risk

`call` has no consent field at all (`CONSENT_FIELD`, `feasibility.py:28`, has no `"call"` key),
so it always fails open. The candidate sources are both warming-program artifacts, not a
company DNC registry:

| Source | Grain | DNC orgs | Concern |
|---|---|---|---|
| `marts.staging.int__warming_exclusion_flags` | 1 row/org, 72,222 | 5,053 (7.0%) | TRANSIENT table in a dbt **staging** schema — not a contract, renameable without notice |
| `marts.sales.snap_daily_warming_eligibility` | daily snapshot, 69,633 orgs | 2,493 latest-per-org | published mart, but orgs leave the snapshot when they leave the warming audience |

Agreement on the overlap: 2,328 both-DNC, **197 orgs disagree**, and ~6.4k orgs appear in only
one. `analytics.main.fact_accounts_transaction.do_not_call` is Salesforce-account-keyed with no
`organization_id`, so reaching it means owning an extra mapping hop.

`marts.staging.int__warming_product_selection.dnc_suppressed_pro_flag` is pro-grain but covers
only 1,232 rows (the current warming cohort) — not a population source. Discard.

**Recommendation:** union the two org-level flags (suppress `call` if *either* says DNC — fail
closed on suppression), emit it as a band, and write the caveat down. Do not present this as a
canonical DNC gate; it is belt-and-braces over Iterable/Twilio, same as the rest of this layer.

---

## 6. Channel really is an uncontrolled lever in the loop

`mechanism_key()` (`loop.py:183`) is `re.sub(r"[^a-z0-9]+", "-", value.casefold())` on the
model-authored mechanism label alone. `LoopState.current_mechanism` / `tried_mechanisms` key on
it. Channel is nowhere in the identity.

Meanwhile `channel_directive()` (`prompts.py:32`) reframes the stimulus substantially per
channel — the SMS branch forces "ONE short SMS ... ~160 characters, no sequences", which is a
different artifact from an email. So round *N* can score "billing transparency via email" and
round *N+1* "billing transparency via SMS", and the loop calls that a **refine** of the same
mechanism while the persona panel is scoring a different thing. Pinning channel per-Pro before
the loop is a scoring-comparability fix, not just a targeting one.

Note `channel_directive` also instructs the model to weigh "consent states" — currently always
absent. Another dead instruction spending tokens.

---

## 7. §2D: the grounding rule has a hole exactly the shape of the shipped bug

The shipped Pro 842846 rationale read *"SMS showed strong returns FOR THIS PRO (82/121 at 1d,
116/121 at 7d)"*. Those numbers are real; they are `pattern_summaries()` output, which takes no
`pro_id` and is population-wide.

Three independent contributing causes, all confirmed:

1. **The grounding rule does not cover this.** `prompts.py:277` forbids only *invented* values —
   "no invented AR balances, job counts, revenue figures, or dates". 82/121 was not invented.
   Misattributing a real population statistic to one Pro is not prohibited anywhere.
2. **The framing invites it.** `evidence_block` says "for similar pros" once, at the top of the
   block; the evolve and ranker prompts then re-label the same text "Historical outcome evidence
   (observed behavior — the strongest signal we have)" and place it adjacent to "This Pro's
   context". The qualifier is out-argued by its own surroundings.
3. **The gate that names this failure is dead code.** `"per_pro_data"` is listed in
   `SUPPRESSING_BLOCK_KINDS` (`pipeline.py:615`) but appears **nowhere else in the repo** — not in
   `critic_prompt`'s block_kind enumeration, not in any test. The critic is never told it exists,
   so it can never emit it.

Fixing this is cheap and independent of everything above: define `per_pro_data` in
`critic_prompt`, extend the GROUNDING rule to cover attribution (not just invention), label the
evidence block at its point of use, and add the one test that fails if the block kind goes
unwired again.

---

## 8. Revised plan

Ordered by dependency. Steps 1–3 are inert without the flow change; step 5 is independent.

1. **Flow — consent from Iterable.** Map the already-fetched Iterable user's channel
   unsubscribe state onto `sms_consent_state` / `email_consent_state` in `Table Structure for
   Merge`. No new query, no new allowlist entry.
2. **Flow — DNC and RECO from Snowflake.** Add to the org-context SQL, both joined on
   `organization_id`, which the Router Query already has:
   - `call_consent_state` from the union of the two warming DNC flags;
   - `suggested_channel` from `channel_recommendations_org.org_best_channel`, plus the three
     `union_p_*` probabilities.
   Add `call_consent_state` and the probability fields to `ALLOWED_FIELDS`.
3. **`feasibility.py`.** Add the `"call"` key to `CONSENT_FIELD` and the emitted literal to
   `NEGATIVE_CONSENT`. No logic change — the gate works, it has just never had input.
4. **Picker.** Pin per-Pro before the loop: filter by the consent gate, then `argmax union_p_*`
   over what survives. The RECO table's own probabilities make the proposed re-pick escape hatch
   and the engagement-band tiebreaker unnecessary — drop both from §2C.
5. **§2D.** Wire `per_pro_data`, close the attribution hole in GROUNDING, label the evidence
   block at point of use. Independent; ship it either way.

### Stated tradeoff, unchanged from §2C
Pinning means the loop cannot discover "this theme only lands by email". Pin per-Pro, never
per-run.

### Open questions for the user
- Is the n8n flow Waypoint's to change, or another team's?
- Is there a canonical DNC registry outside the warming program? If so it beats both sources in §5.
- Should `suppress_all` become an abstain, rather than silently normalizing to `None`?
