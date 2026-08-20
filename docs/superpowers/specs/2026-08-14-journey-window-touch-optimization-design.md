# Pathfinder Waypoint: Journey-Window Touch Optimization

## Purpose

This document reframes Pathfinder Waypoint around the problem we actually need to
solve: helping high-churn-risk pros return to and use the app again.

The goal is not to build an open-ended idea-generation loop, produce a generic
campaign, or treat synthetic persona enthusiasm as proof that a touch will work.
The goal is to make increasingly good touch decisions for a pro in a meaningful
journey window, learn from the pro's observed behavior, and reuse that learning
for similar pros in the future.

This is a design brief for Claude to critique and refine before implementation.
It is intentionally focused on the first three Waypoint capabilities:

1. ingesting context and data;
2. generating feasible touch ideas; and
3. evaluating and selecting among those ideas.

Waypoint is recommendation-only. It reads Iterable data, produces a theme and
idea for review, and hands that recommendation to the LCM tool. LCM drafts and
sends the resulting message. Waypoint must never write to, edit, schedule, or
control Iterable. Iterable outcomes are still required as feedback data because
the system cannot learn without observing what happened after a touch.

## Core objective

Pathfinder Waypoint exists to identify the best feasible touch, or bounded
sequence of conditional touches, for bringing a high-churn-risk pro back into
the app and sustaining app usage.

The retention logic is deliberately simple:

> If a pro returns to and uses the app, that is the primary observable signal
> that we are improving their likelihood of retention.

The optimization target is therefore app usage over multiple horizons:

- return or usage within 7 days;
- continued usage within 14 days;
- continued usage within 30 days; and
- sustained usage within 90 days.

The system should not optimize primarily for an open, a click, a reply, a
synthetic persona score, or message novelty. Those can be useful intermediate
signals, but app usage is the outcome that matters.

## Terminology

### Touch

A touch is one concrete, executable action directed at a pro. A touch is not a
campaign concept or an abstract engagement strategy.

Examples:

- one SMS message;
- one email;
- one in-app prompt;
- one approved product-specific follow-up action.

Every generated recommendation must be independently actionable within the
selected channel. If the channel is SMS, the output must be one clear SMS
theme/idea for LCM to draft, not message copy and not an unspecified
multi-touch campaign.

### Journey window

A journey window is a high-leverage customer state in which a touch may have
unusually high expected value. Initial windows should be intentionally narrow,
for example:

- onboarding;
- an upsell or expansion opportunity; and
- high-churn-risk pros who are not actively using the app.

### War gaming

War gaming means anticipating a small number of plausible responses and deciding
what to do next. It is not a rigid sequence that assumes the pro will behave as
predicted.

A war game should answer:

1. What is the best next touch given the pro's current state?
2. What meaningful outcomes might follow?
3. If the pro returns and uses the app, what should happen next?
4. If the pro does not interact, what alternate touch is justified?
5. If the pro clicks or responds but does not return to meaningful app usage,
   what should change?

Waypoint only recommends the next touch and possible follow-ups. LCM and the
operator-owned workflow decide whether to draft, approve, and send anything.
Later recommendations are conditional on observed behavior.

## The proposed system

The system should be a closed learning loop:

```text
journey data and churn state
        -> feasible touch candidates
        -> evidence-based evaluation
        -> selected recommendation for review
        -> LCM drafts and sends
        -> observed messaging and app behavior
        -> updated evidence and future touch selection
```

The loop is the learning mechanism, not the product definition. The system does
not need to generate 25 new ideas for every pro. It needs to spend enough effort
to make the next decision reliable, then use the observed outcome to improve
future decisions.

## Evaluation strategy

Evaluation should be staged from cheapest and strongest available evidence to
more expensive and uncertain evidence.

### 1. Feasibility and policy gate

Reject candidates that cannot be handed to LCM or violate known constraints before
using LLM or synthetic-persona budget.

The gate should check at least:

- channel capabilities;
- channel and LCM handoff feasibility;
- approved content and personalization boundaries;
- DNC and contactability constraints;
- journey-window relevance;
- whether the touch is materially different from recent failed touches; and
- whether the requested follow-up behavior can be represented as a conditional
  next action.

### 2. Historical outcome evidence

Iterable messaging data and Amplitude app-engagement data should be the primary
evidence sources whenever similar historical examples exist. Iterable is the
authority for delivery and messaging events; Amplitude is the authority for
whether the treated identity returned to and used Housecall Pro.

`org_uuid` is the organization scope when the run is organization-targeted.
`pro_uuid` is the person-level treatment and Iterable/Amplitude join key when
the run is person-targeted. The n8n contract is conditional: it returns the
identity that was supplied to it, so a pro-targeted run may have `pro_uuid`
without `org_uuid`, and an organization-targeted run may have `org_uuid`
without `pro_uuid`. Waypoint must preserve the supplied identity type and must
not infer the missing identity from downstream activity.

The system should compare the candidate with prior touches for pros with similar
states, journey windows, risk levels, channels, and touch history.

Useful outcomes include:

- delivery;
- open or click where applicable;
- reply or other direct response;
- return to the app;
- app usage after return;
- continued usage at 7, 14, 30, and 90 days;
- unsubscribe, opt-out, or other negative outcomes.

Clicks and replies are diagnostic signals. They are not substitutes for the
return-to-app objective.

### Amplitude event interpretation

The Amplitude inventory is broad, but volume is not the selection rule. Events
such as `Click`, `Loaded a Screen`, `Loaded a Page`, experiment exposure, and
message delivery are useful for diagnostics or attribution context, but they
are weak standalone definitions of meaningful app use.

The first measurement contract should use a small, versioned event set with two
layers:

1. **Return signal:** a trusted app-entry event such as `Loaded a Screen` or
   `Loaded a Page`, filtered to Housecall Pro app surfaces and keyed by the
   supplied treatment identity.
2. **Use signal:** one or more meaningful in-app actions such as job creation,
   scheduling, completion, estimate, invoice, payment, customer, or onboarding
   actions. This confirms that the pro did more than open the app.

The primary binary outcome can remain simple: did the treated identity return
and produce qualifying app activity within 7, 14, 30, or 90 days? The event set,
filters, identity type, timezone, and horizon closure must be versioned so the
definition does not drift. Do not optimize against all 100 events, and do not
choose the highest-volume event by default.

The first implementation should establish whether an Iterable touch can be
reliably joined to the supplied treatment identity's subsequent Amplitude
activity at each target horizon. If it cannot, the system must label the
evidence limitation rather than pretend that messaging engagement proves
retention impact.

### 3. Constrained idea generation

The LLM should generate novel touch candidates only after it receives:

- the pro's relevant context;
- the journey window;
- the churn-risk state;
- recent touches and outcomes;
- the channel and execution constraints;
- known successful and failed touch patterns; and
- the desired return-to-app outcome.

Generation should be bounded by the action surface. Novelty is useful when the
historical data is sparse, but the generator must not be rewarded for inventing
themes or ideas that LCM cannot turn into a governed touch or that Waypoint
cannot measure.

### 4. Cheap candidate ranking

Before invoking an expensive evaluator, rank candidates using the available
evidence and metadata:

- expected return-to-app lift;
- evidence strength;
- relevance to the journey window;
- feasibility;
- estimated cost;
- downside risk;
- similarity to previously successful or failed touches; and
- uncertainty.

Most candidates should be eliminated here. Deep evaluation should be reserved
for a small top set, especially when the expected value of additional evidence
justifies the cost.

### 5. Selective synthetic-persona evaluation

Synthetic personas are useful as a pre-send comparison signal for novel or
uncertain candidates. They can help answer whether a touch is plausible,
relevant, understandable, and likely to resonate with the modeled pro.

They are not factual responses from the pro and are not proof that a touch will
improve retention. Their output should be represented as data-backed simulation
or model-based evidence, not observed customer evidence.

Persona evaluation should be used selectively rather than automatically for every
candidate and every pro. Results should be cached or reused where the persona,
journey state, and touch pattern are materially equivalent.

## Why Monte Carlo simulation is not the starting point

Monte Carlo simulation is not the primary evaluator for the first version.
Simulation becomes useful only when there is a credible transition model for how
pros move between states after different touches.

If the transition model is invented or mainly based on synthetic persona
outputs, Monte Carlo will produce precise-looking estimates from uncertain
assumptions. It would multiply uncertainty rather than create evidence.

The correct order is:

1. collect and join real touch and app-usage outcomes;
2. estimate which touch patterns work for which states;
3. validate the measurement and selection policy; and only then
4. consider simulation for comparing longer-horizon policies.

## Decision policy

The system should choose the best touch conditional on the pro's current state,
not search for one universal winner.

The relevant state includes:

- churn-risk level;
- journey window;
- recent app usage;
- prior touch history;
- prior messaging response;
- channel availability;
- organization-specific context; and
- the 7-, 14-, 30-, and 90-day objective.

The system should prefer the candidate with the strongest expected return-to-app
value after accounting for evidence, feasibility, cost, and uncertainty. It
should be allowed to abstain when no candidate has adequate support.

A stable system does not require the generator to be deterministic. It requires
the decision policy to be stable: repeated runs with the same evidence should
usually produce similar rankings, and small generation differences should not
change the selected touch when one candidate is clearly better.

## Multi-touch behavior

The recommended design is a bounded conditional decision tree, not a fixed
multi-touch campaign.

For example:

```text
Touch 1
  -> pro returns and uses app: stop or move to success follow-up
  -> pro clicks but does not use app: change the next touch objective
  -> pro does not interact: consider one alternate touch
  -> negative response or opt-out: stop
```

This gives Waypoint the benefits of war gaming without assuming that the pro's
future behavior is known. It also keeps execution and measurement clear: one
touch is sent, the outcome is observed, and the next decision is made from the
new state.

The first version should not attempt to generate unlimited branches or optimize
an entire campaign upfront. A small number of meaningful branches is enough to
test whether conditional planning improves outcomes.

## Scope for the first version

The first version should focus on journey-window optimization for a small,
high-leverage population rather than all 65,000 future pros.

Recommended initial scope:

- high-churn-risk pros;
- a limited set of journey windows;
- a limited set of supported channels;
- one next touch at a time;
- a small bounded set of conditional follow-ups;
- Iterable read access for messaging outcomes;
- Iterable and Amplitude read access keyed by the supplied treatment identity:
  `pro_uuid` for person-targeted runs or `org_uuid` for organization-targeted
  runs;
- LCM recommendation handoff;
- app-usage outcome measurement at 7, 14, 30, and 90 days;
- historical evidence as the primary evaluator;
- selective synthetic-persona evaluation for novel or uncertain candidates; and
- explicit cost and confidence reporting.

## Cost and scale principles

The system should optimize evaluation spend, not maximize the number of loops.

At future scale, it is not acceptable to run an expensive generation and persona
evaluation process repeatedly for every pro without regard to expected value.

The design should include:

- reusable touch patterns and evaluation results;
- caching for equivalent states and candidates;
- early stopping when another candidate is unlikely to change the decision;
- deeper evaluation only for high-value or high-uncertainty cases;
- batch evaluation where it is safe and useful;
- a small candidate beam rather than unbounded generation; and
- cost-aware prioritization by journey window and expected retention upside.

The system should make the cost tradeoff visible. For example, if one loop costs
30 cents per pro and 25 loops are run, the theoretical cost is $7.50 per pro
before considering downstream execution. At 65,000 pros, that is a material
scale concern. The exact cost model must be verified, but the design should not
depend on repeated deep evaluation as the default path.

## Non-goals

This design does not attempt to:

- write to, edit, schedule, or control Iterable in any way;
- send messages or draft message copy itself;
- replace LCM or the sending workflow;
- design a complete marketing campaign platform;
- prove retention impact using synthetic personas alone;
- generate unlimited multi-touch sequences upfront;
- optimize every pro before the high-leverage windows are understood;
- treat clicks or opens as the primary success metric;
- build a Monte Carlo simulator before real transition data exists; or
- add unrelated data-ingestion and recommendation infrastructure before the
  journey-window use case is validated.

## Success criteria

The first version is successful if it can demonstrate that:

1. A high-churn-risk pro receives a concrete, executable next-touch
   recommendation for operator/LCM review.
2. The touch is selected using historical evidence where available.
3. Novel candidates are constrained and ranked before expensive evaluation.
4. Synthetic personas are used as supporting evidence, not treated as truth.
5. The system can conditionally recommend a follow-up after observing behavior.
6. Iterable outcomes can be joined to subsequent app usage.
7. Outcomes are measured at 7, 14, 30, and 90 days.
8. Successful and failed touch patterns become reusable evidence.
9. The decision can abstain when the evidence is insufficient.
10. Evaluation cost is bounded and visible.

## Questions for Claude to resolve

Claude should critique this design and answer these questions before proposing
implementation:

1. What is the minimum reliable definition of “returned to and used the app” for
   each measurement window?
2. Can Iterable touch records be joined to app activity at the pro level with
   reliable timestamps and identity keys?
3. Which journey windows have enough population and outcome volume to support the
   first experiment?
4. What historical evidence is sufficient to rank a candidate without synthetic
   persona evaluation?
5. When should persona evaluation be invoked, and how should its uncertainty be
   represented?
6. What is the smallest useful conditional decision tree for the first version?
7. How should the system distinguish a promising touch from a promising channel,
   message pattern, or journey state?
8. What policy prevents repeated failed touches from being regenerated under a
   slightly different description?
9. What is the appropriate cost budget per pro and per journey window?
10. What experiment or holdout design would let us learn whether the selected
    touches actually improve return-to-app behavior?

## Recommendation-only boundary

Waypoint has no Iterable write credentials and no authority to alter an
Iterable journey. Its permitted flow is:

```text
Waypoint reads Iterable and app data
        -> Waypoint generates and evaluates a theme/idea
        -> Waypoint recommends it for review
        -> LCM drafts and sends if the owner approves
        -> Waypoint reads the resulting outcomes
```

The runtime modes are therefore:

- **observe:** Waypoint records what it would recommend, with no LCM handoff;
- **recommend/review:** Waypoint hands a theme and idea to the owner/LCM for
  review; and
- **disabled:** the kill switch stops recommendation and handoff output.

There is no Waypoint live-send mode. Waypoint cannot autonomously influence the
selected touch path or mutate Iterable.

## Recommended conclusion

Build Waypoint first as a focused, evidence-backed touch optimizer for
high-churn-risk journey windows.

Use a closed learning loop, but do not confuse learning with repeated blind idea
generation. Use real Iterable and app-usage outcomes as the foundation. Generate
novel themes and ideas only where they are needed. Use synthetic personas
selectively to compare uncertain candidates. Recommend one touch at a time, with
a small conditional war game behind it; LCM drafts and sends only after review.
Then use what the pro actually does to improve the next decision.

The fastest path to the best answer is not necessarily more loops. It is better
state definition, stronger historical evidence, cheaper filtering, selective
simulation, and disciplined learning from real outcomes.

## CEO review decision record

Review posture: **HOLD SCOPE**. The product definition is accepted. The review
held the existing Waypoint product steady and clarified the learning boundary.

Accepted decisions:

- Keep the current compounding loop for candidate discovery and pre-send
  comparison.
- Add a real-outcome policy layer in the same service, using separate modules
  and tables rather than a new deployed service.
- Execute one recommendation per run for now. Hold the bounded conditional
  policy tree as a future extension.
- Define and store cross-run touch history so later runs do not start cold.
- Use an evidence-aware bounded search budget instead of a fixed deep loop for
  every organization.
- Use controlled holdouts and rank mature patterns by incremental lift.
- Allow synthetic-persona evidence to open controlled exploration when historical
  evidence is sparse, but never treat it as proof.
- Store canonical touch patterns alongside the exact theme/idea and observed
  outcomes.
- Keep raw evidence organization-scoped. Share only sufficiently aggregated,
  de-identified pattern statistics across organizations.
- Use Iterable for messaging outcomes and Amplitude for app-engagement outcomes.
- Use binary return-to-app at 7, 14, 30, and 90 days as the primary outcome;
  keep messaging and feature events as diagnostic evidence.
- Preserve the supplied n8n treatment identity: `pro_uuid` for person-targeted
  runs and `org_uuid` for organization-targeted runs. Treat the other identity
  as optional context, never as an inferred join key.
- Pin one versioned evidence snapshot at the start of each run.
- Retry transient outcome-ingestion failures, deduplicate valid repeats, and
  quarantine malformed or unmatched events for replay.
- Treat all external context and touch text as untrusted LLM input and validate
  generated themes/ideas deterministically.
- Start in shadow mode, then move to controlled exposure through an explicit
  kill switch.
- Keep the first observability surface to structured logs and the existing
  winner-review evidence panel, rather than adding a new dashboard.
- Keep the existing proposal-specific measurement plan as secondary diagnostic
  evidence, not as the selection objective.
- Require human review before an LCM handoff can proceed.
- Waypoint never writes to, edits, schedules, or controls Iterable and never
  drafts or sends message copy. LCM owns drafting and sending.

Open decision:

- Exact recommendation-level attribution through LCM and Iterable is not yet
  settled. The first workable fallback uses the supplied treatment identity
  (`pro_uuid` or `org_uuid`), plus channel, theme/idea, and handoff/send
  timestamps. A stable Waypoint recommendation ID remains the preferred future
  contract if LCM can preserve it.

## CEO review architecture

```text
Iterable read API/events ───────┐
                                v
Amplitude read events ─────> Outcome records
                                |
                                v
                     Versioned evidence snapshots
                                |
                                v
Org-scoped Waypoint run ─> candidate discovery loop
                                |
                                v
                     synthetic + historical comparison
                                |
                                v
                     recommendation for human review
                                |
                                v
                         LCM drafts and sends
                                |
                                v
                       Iterable/Amplitude outcomes
```

The kill switch stops recommendation and LCM handoff output. It does not modify
Iterable and there is no Waypoint live-send mode.

## CEO review implementation tasks

- [ ] Define the read-only Iterable and Amplitude outcome contracts, including
  the supplied treatment identity (`pro_uuid` or `org_uuid`), identity type,
  timestamps, horizon closure, and unmatched event behavior.
- [ ] Add an append-only touch-outcome record separate from candidate and round
  records.
- [ ] Add idempotent ingestion, retries, quarantine, replay, and pending/
  positive/negative/unavailable outcome states.
- [ ] Add versioned cross-run touch-pattern evidence and pin its snapshot per run.
- [ ] Add an evidence-aware search budget while preserving the current loop’s
  durable replay and cost controls.
- [ ] Add shadow mode and a kill switch that prevent recommendation/handoff
  output without ever granting Iterable write access.
- [ ] Extend the existing winner review with evidence source, lift, sample size,
  horizon state, exploration status, and evidence snapshot version.
- [ ] Add replayable fixtures for outcome contracts, duplicate/late/malformed
  events, cross-org boundaries, snapshot pinning, and policy selection.
- [ ] Keep the existing measurement-plan flow as secondary diagnostic evidence.

## CEO review status

**READY FOR ENGINEERING REVIEW**, with one open integration dependency: the exact
LCM/Iterable attribution metadata path. The product boundary, outcome source,
treatment unit, rollout posture, and cost-control direction are settled.

## Engineering review decision record

- Scope reduced to a minimum vertical slice: preserve the current compounding
  loop and handoff, then add only the outcome contract, append-only outcome
  history, pinned evidence identity, and recommendation evidence needed to
  validate return-to-app learning.
- n8n returns the identity supplied to it. A pro-targeted run uses `pro_uuid`;
  an organization-targeted run uses `org_uuid`. New outcome records must carry
  the supplied identity and its type as typed fields; do not infer a missing
  identity from JSON or downstream events.
- The existing authenticated handoff action is the approval boundary. Add
  structured approval logging, but do not add a separate approval workflow in
  the first slice.
- Pin the evidence snapshot identity on the run record so retries and resumed
  workers use the same evidence.
- Keep primary outcome ingestion separate from `measurement.py`; that module
  remains responsible for proposal-specific diagnostic indicators.

## What already exists

- `pipeline.py` already provides the durable per-pro worker state machine,
  resumable checkpoints, candidate/round ledger, scoring, abstention, and cost
  accounting. The first slice reuses it.
- `tables.py` already provides durable runs, candidates, winners, measurements,
  handoffs, and fleet kill state. Outcome history and evidence snapshot identity
  are additive records, not replacements for these tables.
- `n8n.py` already provides the typed, allowlisted context boundary and supports
  matching submitted identifiers against echoed provider identifiers. The flow
  is conditional: it returns `pro_uuid` when a pro UUID is supplied and
  `org_uuid` when an org UUID is supplied. The adapter must preserve that input
  identity type rather than require both fields.
- `handoff.py` already provides a durable-before-POST idempotent LCM boundary.
  The first slice treats the authenticated handoff action as approval and adds
  structured approval logging.
- `measurement.py` already provides the finite proposal-specific diagnostic
  metric catalog. It remains secondary and is not rebuilt as the outcome layer.

## NOT in scope

- Bounded conditional policy trees or automatic multi-touch planning. One
  recommendation is evaluated at a time until real outcomes justify expansion.
- A new deployed service. The outcome/evidence layer stays in the existing API
  service and worker process for the first slice.
- A broad evidence-aware search-budget framework, Monte Carlo simulation, or
  fleet-wide optimization. These wait for validated outcome volume.
- Waypoint message drafting, Iterable mutation, autonomous sending, or approval
  bypass. LCM remains the drafting/sending owner and Iterable remains read-only.
- A new dashboard or broad metric taxonomy. Structured logs and the existing
  winner review evidence surface are sufficient for this slice.

## Failure modes and required handling

| Path | Production failure | Test | Handling | User impact |
|---|---|---|---|---|
| Provider ingest | Missing identity, invalid timestamp, or unknown event shape | Yes, contract fixtures | Reject and quarantine for replay | Clear unavailable/degraded evidence state |
| Deduplication | Provider retries the same event | Yes, unique-key replay | Idempotent no-op | No visible corruption |
| Attribution | `pro_uuid` is unmatched or scoped to another `org_uuid` | Yes, cross-org fixtures | Quarantine; never promote to evidence | Evidence marked unavailable |
| Horizon closure | 7/14/30/90-day event arrives late | Yes, late-event fixtures | Keep pending until closure, then update once | Horizon status is visible in review |
| Snapshot pinning | Worker resumes after evidence changes | Yes, crash/resume fixture | Reuse RunRow snapshot id/version | Same run remains reproducible |
| LCM handoff | Network timeout after LCM accepts | Existing plus new approval/idempotency test | Reuse durable handoff key and retry safely | Receipt remains pending/retryable |
| Kill switch | Kill activates before handoff | Yes, kill-switch integration test | Block recommendation/handoff output | No LCM request is made |

No critical silent gap remains after the accepted contract, replay, snapshot,
and handoff tests are added.

## Parallelization

| Step | Modules touched | Depends on |
|---|---|---|
| Outcome contracts and provider adapters | `services/api/src/waypoint/` | Updated n8n/Iterable/Amplitude contracts |
| Outcome/evidence persistence and migrations | `services/api/src/waypoint/`, `services/api/alembic/` | Outcome contract shape |
| Pipeline snapshot pinning and evidence read | `services/api/src/waypoint/pipeline.py` and evidence modules | Persistence schema |
| Handoff approval logging and API evidence view | `services/api/src/waypoint/api.py`, `handoff.py`, `apps/web/` | Identity and persistence schema |
| Replay and integration fixtures | `services/api/tests/` | All preceding contracts |

Lane A: outcome contracts → persistence (sequential, shared data model).

Lane B: handoff approval logging (independent after the identity contract).

Lane C: replay fixtures (waits for persistence and adapter contracts).

Launch Lane A and Lane B in parallel after the external identity contract is
confirmed; then run Lane C and the pipeline integration work against the merged
schema. Lane A and Lane C both touch the data-model/test surface, so they should
not edit the same migration or fixture files concurrently.

## Implementation Tasks

Synthesized from the engineering review. These are the minimum build tasks.

- [ ] **T1 (P1, human: ~1 day / CC: ~10 min)** — Outcome contract — define strict
  Iterable/Amplitude models with the supplied treatment identity (`pro_uuid` or
  `org_uuid`), an explicit identity type, event time, horizon, provider identity,
  and quarantine behavior.
  - Surfaced by: D5, D7; conditional identity and untrusted-event boundary.
  - Files: `services/api/src/waypoint/n8n.py`, new outcome contract module,
    `services/api/tests/test_n8n.py` and outcome fixtures.
  - Verify: contract tests reject malformed/unmatched events and preserve both IDs.
- [ ] **T2 (P1, human: ~1–2 days / CC: ~15 min)** — Outcome persistence — add
  append-only typed outcome rows, deterministic dedup keys, indexes, identity
  type, horizon states, and quarantine/replay records.
  - Surfaced by: D5, D8, D11; database-enforced attribution and replay.
  - Files: `services/api/src/waypoint/tables.py`, `services/api/alembic/versions/`,
    outcome/evidence modules, persistence tests.
  - Verify: migration tests cover duplicates, late events, cross-org isolation,
    and indexed evidence lookups.
- [ ] **T3 (P1, human: ~1 day / CC: ~10 min)** — Run snapshot — resolve and
  persist one evidence snapshot id/version at run start and reuse it on resume.
  - Surfaced by: D4; reproducible worker retries.
  - Files: `tables.py`, `pipeline.py`, run creation/worker tests.
  - Verify: crash/resume fixture proves identical snapshot identity.
- [ ] **T4 (P2, human: ~1 day / CC: ~10 min)** — Evidence review — expose source,
  snapshot, sample, horizon state, and uncertainty in existing winner evidence.
  - Surfaced by: scope decision and existing winner-review boundary.
  - Files: `pipeline.py`, `api.py`, `apps/web/src/components/WinnerReview.tsx`,
    API/UI tests.
  - Verify: review shows evidence without exposing another organization’s rows.
- [ ] **T5 (P2, human: ~1 day / CC: ~10 min)** — Handoff boundary — retain the
  authenticated handoff as approval, add structured approval logging, batch
  existing winner/measurement reads, and preserve idempotent LCM delivery.
  - Surfaced by: D3 and D10; auditable review and no database N+1 path.
  - Files: `api.py`, `handoff.py`, `tables.py`, handoff/API tests.
  - Verify: one authenticated approval produces one LCM request; retries do not duplicate it.
- [ ] **T6 (P1, human: ~2–3 days / CC: ~20 min)** — Replay fixtures — cover both
  conditional identity paths plus all outcome, snapshot, isolation, approval,
  and kill-switch branches.
  - Surfaced by: D9; complete test coverage for the learning boundary.
  - Files: `services/api/tests/`, `tests/fixtures/`.
  - Verify: `pytest` with real Alembic migrations and targeted integration tests.

## Engineering review status

**DONE_WITH_CONCERNS.** The reduced plan is ready for implementation after the
canonical Amplitude active-use event set and filters are confirmed. The n8n
identity behavior is now understood: preserve whichever treatment identity was
supplied. The stable LCM recommendation ID remains a tracked follow-up, not a
first-slice blocker.

## Current open questions

These are the only questions that still need product/data/integration answers:

1. Which Amplitude events and filters define “returned to the app” and
   “meaningful app use” at 7, 14, 30, and 90 days?
2. Which Housecall Pro surfaces qualify for `Loaded a Screen` or `Loaded a
   Page`, and which background or non-product activity must be excluded?
3. For an `org_uuid`-targeted run, does success mean any connected pro uses the
   app, the admin uses the app, or a defined share of connected pros use it?
4. Should the normalized outcome contract use `treatment_id` plus
   `treatment_type` (`pro_uuid` or `org_uuid`), with the other identity fields
   optional?
5. Which Iterable fields reliably provide delivery, open, click, reply,
   unsubscribe, and send timestamps for the supplied treatment identity?
6. Can LCM preserve a stable Waypoint recommendation ID through drafting and
   Iterable delivery? This is a follow-up, not a first-slice blocker.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | CLEAR | HOLD SCOPE; recommendation-only boundary preserved |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | Skipped |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | DONE_WITH_CONCERNS | 10 issues, 0 critical gaps; all first-slice decisions resolved |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | Not needed for backend-first slice |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | Not run |

**VERDICT:** CEO + ENG CLEARED for implementation with one documented measurement
dependency: canonical Amplitude active-use event set and filters. The n8n
identity behavior is documented, and the LCM recommendation ID remains a
follow-up.

**UNRESOLVED DECISIONS:**

- Confirm the exact Amplitude event/event-set, filters, identity handling, and horizon rules before finalizing the outcome adapter.
