# Design follow-ups

- [ ] **Responsive run-setup refinement**
  - **Why:** The first release keeps the existing single-column flow, but the
    loop controls, confirmation inputs, fleet safety setting, and run inputs may
    become dense on narrow screens.
  - **Pros:** Produces intentional tablet/mobile hierarchy from real control
    density and operator usage instead of guessing during backend delivery.
  - **Cons:** Requires a later viewport review and may change the form layout.
  - **Context:** Revisit after the loop controls ship and usage data shows which
    fields operators need most often. Preserve keyboard order and 44px targets.
  - **Depends on / blocked by:** The first loop-control UI must exist so density
    can be measured against the real content.

- [ ] **Product design system**
  - **Why:** The app has shared CSS variables and component patterns but no
    `DESIGN.md` defining typography, spacing, component vocabulary, or state
    rules across screens.
  - **Pros:** Gives future features a durable visual reference and reduces
    accidental drift between operator surfaces.
  - **Cons:** It is broader than this loop and creates no immediate value for
    the first implementation.
  - **Context:** Start when multiple Pathfinder screens need coordinated visual
    decisions; document the existing vocabulary before introducing new tokens.
  - **Depends on / blocked by:** Agreement on product-wide visual direction and
    enough screens to make the system representative.

- [ ] **Stable recommendation attribution across LCM and Iterable**
  - **Why:** The minimum slice can join outcomes with `pro_uuid`, `org_uuid`,
    channel, theme/idea, and timestamps, but a stable recommendation ID is the
    reliable way to learn which exact Waypoint recommendation caused an outcome.
  - **Pros:** Enables exact touch-level attribution, safer retries, and clearer
    learning from multiple nearby touches.
  - **Cons:** Requires an integration contract and may require LCM to preserve
    Waypoint metadata through drafting and Iterable delivery.
  - **Context:** The current handoff has a durable Waypoint idempotency key, but
    the external attribution path is not settled. Start by agreeing on the ID
    carried Waypoint -> LCM -> Iterable -> outcome ingestion and its retention.
  - **Depends on / blocked by:** LCM support for preserving the identifier and
    confirmation of the Iterable readback fields.
  - **Status:** the LCM handoff now sends the winner ID as `row_id` in each
    Pathfinder Intake batch row (was: `recommendation_id` in the old per-winner
    payload); `POST /api/outcomes` accepts it back under either spelling
    (`recommendation_id` or `row_id`) to key inbound touch outcomes. Note:
    `journey_window` and `follow_up` are no longer forwarded to LCM at all —
    the intake row shape is fixed to `pro_uuid`/`theme`/`theme_category`/
    `org_id`/`row_id`; both remain stored (`run.journey_window`,
    `winner.evidence["follow_up"]`) and readable via the API. The remaining
    gap is entirely external: LCM must echo `row_id` through drafting and
    Iterable delivery so it round-trips on the outcome event, and extending
    the intake contract to carry `journey_window`/`follow_up` is pending
    confirmation with Allison.

- [ ] **Canonical Amplitude active-use event contract**
  - **Why:** The primary outcome is binary return-to-app and continued use, but
    the exact Amplitude event or event set and horizon rules are not yet named.
  - **Pros:** Makes 7/14/30/90-day outcomes reproducible and prevents the
    implementation from choosing a weak feature-specific proxy.
  - **Cons:** Requires product/data-owner agreement before the outcome adapter
    can be finalized.
  - **Context:** Amplitude is authoritative for app engagement and `pro_uuid`
    is the person-level join key. Document the canonical event names, required
    identity fields, timezone handling, and what counts as positive usage at
    each horizon.
  - **Depends on / blocked by:** Access to the Amplitude event catalog and the
    owner of the retention measurement definition.
  - **Status:** The ingestion side is ready and waiting: `POST /api/outcomes`
    and the `returned_7d/14d/30d/90d` horizon fields on `TouchOutcomeRow`
    already exist. No outcome source posts to that endpoint yet, so the
    evidence store stays empty and generation runs with the honest "no
    evidence" block until the event contract lands.

- [ ] **V3: fit the loop's own parameters from observed outcomes**
  - **Why:** Today the loop *reads* history and never *fits* to it. Evidence
    enters as prompt text (`evidence.evidence_block`) and as a seeded mechanism
    (`warmstart.retrieve`), so a model decides what to do with the numbers. The
    machinery's own tuning surface stays whatever a human typed:
    `WARM_START_THRESHOLD` (0.75), `DEFAULT_SIMILARITY_WEIGHTS` (segment /
    lifecycle_stage / churn_risk_state at 2.0, every other field 1.0),
    `TIE_MARGIN`, and the ranker rubric. These are operating defaults, not
    proven values, and no observed outcome can currently move any of them.
  - **What fitting means here:** measure, then move the knob. Compare return
    rates of warm starts bucketed by similarity score to find where the
    threshold actually earns its keep; compare per-field match against return
    rates to reweight `DEFAULT_SIMILARITY_WEIGHTS` (a shared `vertical` may
    transfer better than `lifecycle_stage` — the current weights are a guess);
    compare cold-start versus warm-start outcomes to confirm warm starts help
    at all. Semi-automatic first: the system proposes new values with the
    supporting counts and a human approves. Keep the scorer behind
    `warmstart.retrieve`'s interface so a fitted version replaces its body
    without touching the pipeline.
  - **Pros:** Turns real 7/14/30/90-day return behavior into better selection
    quality and lower evaluation spend, instead of leaving the whole tuning
    surface frozen at launch guesses.
  - **Cons:** Needs meaningful attributable volume before any fit is honest — a
    fit on thin data is worse than the default it replaces. It also gives up a
    real V2 property: today bad evidence degrades output gracefully (worse
    ideas), whereas a wrongly fitted threshold mistunes selection silently.
  - **Context:** Deliberately out of V2 scope. V2 compounds through prompts and
    mechanism seeding only, and that is the honest claim to make about it.
    Before changing any default, record cold-start versus warm-start outcomes,
    ranker choices, persona calls, cost, and downstream return-to-app behavior.
  - **Depends on / blocked by:** Canonical Amplitude active-use contract, a
    settled outcome-attribution anchor (see above), and enough attributable
    volume for those comparisons to mean anything.
