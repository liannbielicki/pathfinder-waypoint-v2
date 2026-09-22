# V4 Loop and Idea Quality Implementation Plan

> **Execution:** Use Superpowers test-driven implementation and review each completed behavior against the approved design before proceeding. Do not commit or push without explicit user approval.

**Goal:** Make V4's model choice, idea loop, channel decision, persona validation, learning evidence, and winner explanation reliable and auditable.

**Architecture:** Extend the immutable run snapshot for model choice; tighten existing Pydantic/pipeline contracts; reuse the existing feature catalog, round ledger, winner evidence, and winner card. Prefer deterministic validation and metadata over new services.

**Tech stack:** Python/FastAPI/Pydantic/SQLAlchemy/Postgres, React/TypeScript/Next.js, pytest/Vitest.

---

### Task 1: Persist and route the run model

**Files:** `models.py`, `tables.py`, `api.py`, `worker.py`, `pipeline.py`, migration `0018`, RunStart/API types, model/API tests.

- [x] Write failing tests for deep default, fast override, persisted run view, actual-model evidence, and cache separation.
- [x] Add `model_tier: fast|deep`; migrate historical rows as `fast`, default new runs to `deep`.
- [x] Route generation, critic, rank, and screen calls through the run tier; retain deep final with its existing fallback.
- [x] Expose configured fast/deep model names in fleet settings and the selected tier in the run UI.

### Task 2: Make loop semantics and candidate comparison real

**Files:** `loop.py`, `prompts.py`, `pipeline.py`, ranking/loop/pipeline/concurrency tests.

- [x] Write failing tests for cold round one, refine-after-loser, canonical mechanism dedupe, shift novelty, and all-candidate screening.
- [x] Track the current selected candidate/mechanism during ledger replay and normalize mechanism equality.
- [x] Generate cold/shift batches as distinct mechanisms and refine batches as variants of the current mechanism.
- [x] Screen every feasible candidate under the same strict reaction schema; select by comparable persona score and persist the complete decision.
- [x] Reserve the actual worst-case screen count and preserve bounded concurrency.

### Task 3: Make channel and execution contracts safe

**Files:** `models.py`, `prompts.py`, `n8n.py`, `catalog.py`, `pipeline.py`, `handoff.py`, related tests.

- [x] Write failing tests proving generated `none` is invalid and legacy/invalid channels never reach LCM.
- [x] Carry an allowlisted suggested channel into standard and staging briefs, require an explicit override reason, and persist agreement evidence.
- [x] Resolve CTA/feature claims through the existing catalog and suppress unknown or unreachable execution claims before persona evaluation.
- [x] Keep call recommendations as operator to-dos and no-action as a Winner state.

### Task 4: Replace exact persona matching with a labeled fallback ladder

**Files:** `personas.py`, `n8n.py`, `pipeline.py`, persona/pipeline tests.

- [x] Write failing tests for weighted field coverage, no-exact-match fallback, low-coverage labels, held-out final preference, and labeled reuse.
- [x] Rank strong weighted matches first, then same-segment and broad fallbacks; never fail merely because the threshold is unmet.
- [x] Prefer unseen final validators and reuse only when the pool is too small.
- [x] Persist match quality, coverage, fallback reason, and overlap IDs.

### Task 5: Prevent false learning

**Files:** `evidence.py`, `warmstart.py`, outcome/warm-start tests.

- [x] Write failing tests for per-touch deduplication and minimum fingerprint coverage.
- [x] Merge multiple source rows for one logical touch before pattern counts.
- [x] Require sufficient shared allowlisted fields before similarity can qualify a warm start; record coverage telemetry.
- [x] Preserve channel-specific evidence and document unavailable external channel sources honestly.

### Task 6: Add the compact winner explanation

**Files:** `api.py`, `apps/web/src/lib/api.ts`, `WinnerReview.tsx`, fixtures/tests.

- [x] Write failing API/UI tests for round ranking, channel comparison, candidate counts/scores, warm-start state, and actual models.
- [x] Return the existing round `ranking` object and additive model/channel evidence.
- [x] Render the compact summary and a collapsed technical disclosure inside the existing winner card.
- [x] Do not build a separate dashboard or duplicate backend evidence.

### Task 7: Integration and adversarial review

- [x] Re-read this plan and the design, inspect the complete diff, and run focused regression tests.
- [x] Run backend `ruff`, `mypy`, all `pytest`, frontend unit tests, lint, and production build.
- [x] Verify migration heads, OpenAPI/generated contract consistency, clean diff checks, and preservation of the untracked n8n file.
- [x] Run independent spec and code-quality review; fix all critical/important findings and rerun affected verification.
