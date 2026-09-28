# V4 Audit Repairs Implementation Plan

> **Execution:** Inline in the existing V4 worktree. Deliver as one local commit; no push requested.

**Goal:** Correct verified contact-plan and evidence-attribution defects without changing n8n SQL or pre-existing jobs.

**Architecture:** Keep contact selection and evidence gates at their existing boundaries. Treat a missing candidate block as unavailable for new enforced Staging jobs; keep the existing legacy path for Standard context and jobs with paid rounds. Reject model rationale that repeats population return counts as an individual claim before ranking or storage.

**Tech Stack:** Python 3.14/FastAPI/pytest, React/TypeScript/Vitest.

---

### Task 1: Candidate-specific RECO freshness

**Files:** `services/api/tests/test_contact_plan.py`, `services/api/src/waypoint/contact_plan.py`.

- [x] Add failing tests: mixed fresh/stale rows choose the fresh row; future-dated rows do not count as fresh.
- [x] Run `services/api/.venv/bin/python -m pytest -q services/api/tests/test_contact_plan.py` from repo root and confirm both fail for the expected selection.
- [x] Evaluate `0 <= today - scored <= RECO_MAX_AGE` for each candidate in `_score`/`_rank_key`; do not share one Boolean across the pool.
- [x] Rerun the focused tests and Ruff.

### Task 2: Missing candidate block under enforce

**Files:** `services/api/tests/test_pipeline.py`, `services/api/src/waypoint/pipeline.py`.

- [x] Add a failing test for a new enforced Staging job with no candidate rows: no LLM spend and an explicit unavailable result. Preserve tests for Standard context and jobs with prior rounds.
- [x] Run the specific test and confirm it fails for the expected legacy behavior.
- [x] Branch on context source and started work in `_stage_plan`: fail closed only for a new Staging job under enforce; preserve the legacy paths. Keep shadow behavior observational.
- [x] Run database-backed pipeline tests with local PostgreSQL after the required sandbox escalation.

### Task 3: Population-outcome attribution

**Files:** `services/api/tests/test_prompts.py`, `services/api/tests/test_evidence.py`, `services/api/tests/test_pipeline.py`, `services/api/src/waypoint/prompts.py`, `services/api/src/waypoint/evidence.py`, `services/api/src/waypoint/pipeline.py`.

- [x] Add failing tests for the documented `82/121` rationale, a correctly scoped population statement, and a critic prompt that names `per_pro_data`.
- [x] Run the pure tests red and green, then the full database-backed API suite.
- [x] Make each population evidence line self-labeling, define `per_pro_data` as a hard block, and deterministically suppress a rationale that repeats a population return ratio as a per-Pro fact even if the critic returns `none`.
- [x] Rerun the focused tests and verify the documented false claim is blocked before ranking.

### Task 4: PATIENCE copy

**Files:** `apps/web/src/components/RunStart.tsx`, `RunStatus.tsx`, and their tests.

- [x] Add failing UI assertions for “Consecutive losses before shifting” and help text that explains wins reset the counter.
- [x] Run affected Vitest tests red.
- [x] Change the labels and help text only; keep loop behavior unchanged.
- [x] Rerun affected tests, lint, TypeScript, and production build.

### Task 5: Keep the checkpointed contact-plan mode on replay

**Files:** `services/api/tests/test_pipeline.py`, `services/api/tests/test_resume.py`, `services/api/src/waypoint/pipeline.py`.

- [x] Reproduce an enforced plan losing its pin when `CONTACT_PLAN_MODE` changes to `off`.
- [x] Make the checkpointed mode authoritative after the plan stage; verify a shadow plan does not become enforced when the setting changes in the other direction.
- [x] Rerun the focused regression, Ruff, and mypy.

### Final verification

- [x] Inspect the diff and status; preserve the untracked n8n export.
- [x] Run backend focused tests, Ruff, mypy, web tests, lint, TypeScript, and build.
- [x] Run the full backend/database and browser tests with sandbox escalation.
- [x] Record all audit repairs in one local commit; leave it unpushed.
