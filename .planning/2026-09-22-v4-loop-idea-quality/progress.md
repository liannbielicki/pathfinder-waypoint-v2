# Progress

- 2026-09-22: fetched remote; V4 matches upstream and already includes main.
- 2026-09-22: baseline verified: backend 703 passed (2 deselected); frontend 116 passed, lint and build passed.
- 2026-09-22: completed bounded audits for loop/model, persona fallback, and channel/learning/UI paths.
- 2026-09-22: implemented per-run deep/fast selection (deep default), explicit cold/refine/shift semantics, canonical mechanism rejection, and all-candidate screening.
- 2026-09-22: implemented strict persona-score validation, persona fallback/held-out reuse evidence, RECO-channel override gating, deterministic catalog CTAs, and compact selection evidence.
- 2026-09-22: hardened learning evidence dedupe and warm starts against duplicate source rows and sparse fingerprints.
- 2026-09-22: independent adversarial review found and repaired replay-state, refine-prompt, critic-enum, CTA-delivery, fallback-ranking, and model-default inconsistencies.
- 2026-09-22: regenerated OpenAPI/types and verified 733 backend tests, Ruff, mypy, 118 frontend tests, ESLint, Next production build, Alembic single head, and diff whitespace.
- 2026-09-22: diagnosed the restart incident: an async n8n workflow collided with the Standard webhook path and outer worker-loop exceptions could terminate the process.
- 2026-09-22: separated the async webhook path, made Standard `202` a fail-once configuration error, supervised every long-lived service loop with capped backoff, and suppressed noisy successful httpx request logs.
- 2026-09-22: final review added bounded persona-judgment retries, catalog-only CTA authority, a two-stack screening cap, and accurate fallback-panel wording.
- 2026-09-22: final verification passed with 741 backend tests (2 deselected), Ruff, mypy, 119 frontend tests, ESLint, Next production build, Alembic single head, and diff whitespace. Alembic's broader metadata check still reports the pre-existing unrelated drift recorded in findings.
- 2026-09-22: pre-existing untracked n8n audit file remains preserved and excluded from the planned commit.
