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
