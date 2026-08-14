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
  - **Status:** Waypoint now emits `recommendation_id` (the winner ID) in the
    LCM handoff payload and accepts it back on `POST /api/outcomes` to key
    inbound touch outcomes. The remaining gap is entirely external: LCM must
    preserve `recommendation_id` through drafting and Iterable delivery so it
    round-trips on the outcome event.

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
