# Task plan

Git reality: GREEN — `V4-Improvements` equals upstream and contains `origin/main`; one pre-existing untracked n8n file is out of scope.

Implementation contract:

- Target behavior: the approved V4 loop and idea-quality design.
- Existing patterns to reuse: immutable run snapshots, Pydantic contracts, feature catalog, evolve-round ranking JSON, winner evidence, current winner card.
- Ponytail decision: reuse existing code and stored evidence; smallest custom validation only.
- Acceptance check: focused red-green tests plus complete backend/frontend verification.
- Do not build: new dashboard, new outcome pollers, speculative LCM fields, exact persona requirement, or automatic threshold tuning.

Roadmap:

- [x] P1 model routing and durable run selection
- [x] P2 loop semantics and comparable candidate scoring
- [x] P3 channel/no-action and execution safety
- [x] P4 persona fallback and held-out validation
- [x] P5 learning dedupe and warm-start coverage
- [x] P6 compact decision explanation
- [x] P7 full verification and independent review
