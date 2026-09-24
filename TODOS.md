# TODOS

**The one backlog for this repo.** Handoffs and plans link here instead of keeping their own lists.
Ordered by priority within each section. Every item names its evidence; re-check before acting.
Last reviewed 2026-09-24 against `V4-Improvements` @ d8df7d5.

Detail lives in: `docs/2026-09-24-next-session-handoff.md` (loop work, "§" refs below),
`docs/2026-09-24-repo-audit-handoff.md` (repo audit), and the brief on `feature/channel-selection`.

---

## Now — correctness and cost

- [ ] **Stop population evidence being narrated as one Pro's history.** A shipped LCM handoff said
  "SMS showed strong returns FOR THIS PRO (82/121…)"; those numbers come from `pattern_summaries()`,
  which has no `pro_id`. Three causes: the GROUNDING rule (`prompts.py:277`) bans only *invented*
  values, not misattributed ones; the evidence block says "similar pros" only at its top; and block kind
  `per_pro_data` sits in `SUPPRESSING_BLOCK_KINDS` (`pipeline.py:615`) but no prompt defines it, so the
  critic can never emit it. Fix all three; add a test that fails if the kind goes unwired.
- [ ] **Find out why the critic suppresses so many rounds.** Run `acc26e53`: Pro `475567` lost 6 of 10
  rounds to the critic, each paid for (generation + critic) and never panel-scored. Count
  `CandidateRow.critics["block_kind"]` for that run; one dominant kind means a prompt fix or an
  over-strict gate. Biggest visible cost lever. (§1.5 B, §4.3)
- [ ] **Next run: set `MAX_NO_IMPROVE=3`.** Operator setting on the start form, no code. At 5 it never
  trips inside the round cap; replay shows 3 stops `553067` at round 13 instead of 15. Confirm at least
  one Pro ends on `no_improve_exhausted`. (§1.5 A, §4.3)
- [ ] **Decide the `PATIENCE` label.** The UI says "Refine attempts per mechanism" (`RunStart.tsx`,
  `RunStatus.tsx`), implying a hard cap; the code counts *consecutive losses* and resets on a win.
  Recommended: fix the label text only. Alternative: add a real per-mechanism cap. **Needs Jake.** (§1.5 D)
- [ ] **Fix `MissingGreenlet` under load.** 6 of 200 jobs fail at evolve under 4-worker concurrency and
  only succeed on retry. Also fix the false assertion message "a live lease was double-claimed"
  (`tests/test_load.py:123`) — it sent the last session down the wrong path. Repro:
  `pytest tests/test_load.py -m load -q`. (§4.2)
- [ ] **Close the CI gap.** Add a separate, visible `pytest -m load` job and extend `mypy` to `tests/`
  (non-strict). (§4.1)

## Channel selection — branch `feature/channel-selection`

One feature; the brief on that branch has the evidence. Headline facts:
- [ ] **The consent gate has never blocked anything.** Neither context flow emits `sms_consent_state` or
  `email_consent_state`, and `call` has no consent key at all (`feasibility.py:28`). Source DNC/opt-out
  from Salesforce `account` (`sms_opt_out__c`, `dnc_phone__c`, `dnc_email__c`) and mirror LCM's Iterable
  SMS rule. Consent must bypass promotion, like the contact `pro_uuid` does.
- [ ] **RECO never reaches Staging runs.** Check whether the active promotion bundle maps
  `RECOMMENDED_ACTION → suggested_channel`; if not, that alone explains "RECO unavailable". Then switch to
  the org-grain table `channel_recommendations_org` with probabilities.
- [ ] **Pick the channel once per Pro, before the loop** (from the consent-filtered set, RECO as the
  basis), so rounds stay comparable. **Needs Jake's sign-off on pinning.**

## Context, repo, and docs hygiene

- [ ] **Run form defaults to Standard context** (`RunStart.tsx:57`) though real runs use Staging.
  Flip the default? **Needs Jake.**
- [ ] **Merge `main` into V4** and resolve the double channel-selection implementation (`fb92bba` on
  main vs `ebb2b86`… on V4; conflicts in `RunStart.tsx`, `feasibility.py`, `handoff.py`, `models.py`,
  `prompts.py`). Keep V4's behavior by default. Redeploys staging — **ask first.**
- [ ] **Refresh `n8n/` from live**: fix the Workbench flow's webhook path (`context-async-v1` →
  `context-v1`), sync the Standard flow's `Merge` node, delete `waypoint-variable-audit-context-v1.json`
  (no live counterpart), decide the untracked `…-by-org-uuid-v1.json`.
- [ ] **Remove the dead `N8N_CONTEXT_URL_STAGING` setting** (`settings.py:18`, `.env.example`, three tests).
- [ ] **Ask whether anything still calls the active `pathfinder-audience-boundary` flow**; retire it if not.
- [ ] **Docs Phase C**: archive superseded specs (the 2026-09-17 per-run-context-source and
  railway-context-workbench designs still describe `N8N_CONTEXT_URL_STAGING`), write
  `docs/ARCHITECTURE.md`, remove the duplicated README workbench section, refresh `HUMAN-TASKS.md`'s
  stale launch status. (Audit handoff §5)

## Measurement quality

- [ ] **Screen panel looks optimistic.** Final 5-panel scores ~half the 3-panel screen for 3 of 4 Pros
  (`553067` 4.9 → 3.0 pp). Check whether the panels draw from different pools or backfill drags the mean. (§1.5 C)
- [ ] **Persona fit is `1.00` for everyone, and per-Pro churn baselines are off.** Likely one root cause:
  the flow's tenure vocabulary (`under_1y`…) never matches the cards (`0-3m`…), so fit collapses to
  `{segment}` and `calibration_cell()` stays disabled. Fix at the n8n flow, then re-tune `KEEP_DELTA_PP`
  and `MIN_REDUCTION_FLOOR_PP`. Verify against a live `persona-cards` payload first. (§4.4, §7)
- [ ] **V3 outcome gaps**: `unsubscribed` has no writer (STOP arrives via `smsReceived`); the Iterable
  cursor runs ~24h behind because winner-less control batches get deferred; the return-event definition
  (`Loaded a Screen` / `Loaded a Page`) still lacks formal data-owner sign-off.

## Later — blocked on outcome volume

- [ ] **Fit the loop's own knobs from observed outcomes.** `WARM_START_THRESHOLD` (0.75),
  `DEFAULT_SIMILARITY_WEIGHTS`, `TIE_MARGIN`, and the ranker rubric are launch guesses nothing can move.
  Compare warm-start return rates by similarity bucket, reweight fields by match-vs-return, and confirm
  warm starts beat cold starts at all. Semi-automatic: propose values with counts; a human approves.
  If bounded Postgres/Python retrieval later misses latency at production size, revisit retrieval then.
  Needs enough attributable volume first — a fit on thin data is worse than the default.

## Small

- [ ] `_panel_for`'s `size` is typed `Any`, not `Literal[3, 5]`.
- [ ] Enforce `N8N_TIMEOUT_SECONDS` (settings, default 900) < `LEASE_SECONDS` (`worker.py:49`, 1800); only an operator override can break it today.
- [ ] `_screen_finalists` raises `failures.exceptions[0]` arbitrarily; a `LeaseLost` can get buried.
