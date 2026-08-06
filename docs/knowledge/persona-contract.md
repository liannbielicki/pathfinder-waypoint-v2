# U3 — personas: persona_*, synthetic_persona_evaluator, local_reaction_* (16 files, ~1779 loc)

Audit date: 2026-08-05. All claims carry repo-relative file:line evidence; anything I could not confirm is in §8 only.

## 1. What it actually does

This unit is the **persona reaction-scoring stack**: it turns a candidate outreach touch into a churn-reduction estimate by simulating synthetic home-service pros.

The live default path (provider `persona-cards`, `src/pathfinder/persona_provider.py:24`):

1. `PersonaCardsClient` POSTs to Riley's Railway service `/api/persona-cards` and gets a panel of frozen persona archetype cards for a segment (`src/pathfinder/persona_cards_client.py:57-72`), parsed by `persona_cards_contract.panel_from_dict` (`src/pathfinder/persona_cards_contract.py:29-44`). Optionally the panel is served from a disk snapshot pin instead (`src/pathfinder/persona_card_snapshot.py:178-226`), so a multi-day campaign can't straddle two of Riley's persona generations.
2. `LocalReactionRunner` runs the reaction LLM walk locally: for each card, for each touch in the sequence, one Anthropic call produces an in-character free-text reaction (`src/pathfinder/local_reaction_runner.py:125-140`), then a cross-family OpenAI `gpt-4o-mini` Doubt-Gap scorer converts it to a 3–7 reaction score Sdg = mean(C_spec, I_feas, V_trust) (`src/pathfinder/local_reaction_runner.py:142-155`, scorer itself lives in `src/pathfinder/reaction_scorer.py`, unit U-other). A compact per-persona experience summary is threaded between touches (`src/pathfinder/local_reaction_runner.py:157-161`). Any failure produces a `score_error` step, never a fabricated score (`src/pathfinder/local_reaction_runner.py:163-178`).
3. `LocalReactionClient` glues 1+2 behind the exact `.react(...)` surface of the legacy HTTP client, memoizing identical requests, fetching at Riley's 24-card floor and slicing to the requested panel size (`src/pathfinder/local_reaction_client.py:40-67`).
4. `SyntheticPersonaEvaluator.evaluate` calls `.react` twice — prefix-only and prefix+candidate, same seed — and hands the paired responses to `persona_reward.marginal_churn_reduction` (`src/pathfinder/synthetic_persona_evaluator.py:136-168`). The prefix-only response is cached under a lock (`src/pathfinder/synthetic_persona_evaluator.py:115-134`).
5. `marginal_churn_reduction` maps each persona's **final-step** reaction through a reaction→churn transform (group-aware logit around a real per-(segment,plan,tenure) churn baseline: `src/pathfinder/reaction_churn_transform.py:62-70`, unit U-other) and returns reduction = −mean(paired delta), a magnitude CI (panel SE ⊕ propagated beta SE), and a direction-only paired-t bound (`src/pathfinder/persona_reward.py:55-182`).
6. A gate decides whether the estimate may drive decisions. Live cards path: `ReactionSignificanceGate` — panel big enough AND `direction_ci_lower` clears a 1.0pp floor; magnitude is deliberately untrusted (`src/pathfinder/persona_gate.py:82-117`, floor at `src/pathfinder/persona_gate.py:48`, wired at `src/pathfinder/action_console/persona_response.py:670-672`). Everything else abstains fail-closed with the reward kept in diagnostics (`src/pathfinder/synthetic_persona_evaluator.py:216-227`).

The legacy path (provider `sequence-react`) is the same evaluator over `PersonaServiceClient`, an HTTP client for our Railway fork's `/api/sequence-react` with 429 retry (`src/pathfinder/persona_client.py:44-97`), gated by `CalibrationGate` (`src/pathfinder/action_console/persona_response.py:728`). It is env-selectable but no longer the default.

Support cast: `persona_fixture.FakePersonaService` is the deterministic no-LLM test double encoding a known reaction→churn truth (`src/pathfinder/persona_fixture.py:20-61`); `persona_pairing`/`persona_pairs_io` join reactions to real weekly-panel churn for the *older* calibration-minting scripts (`src/pathfinder/persona_pairing.py:7-53`, `src/pathfinder/persona_pairs_io.py:14-44`); `persona_shadow` runs both providers side-by-side with Railway as authority and summarizes agreement — the cutover-evidence tool (`src/pathfinder/persona_shadow.py:42-167`); `persona_cards_content` is the canned per-action_type stimulus copy used by calibration/validation scripts (`src/pathfinder/persona_cards_content.py:6-84`); `persona_provider` maps the two provider names to labels/modes and validates the env selection (`src/pathfinder/persona_provider.py:22-43`).

Panel economics as wired: search panel = **3 personas** (`src/pathfinder/action_console/persona_response.py:532`, `src/pathfinder/action_console/models.py:513`), confirm = 12 (`src/pathfinder/action_console/models.py:520`), final-choice readout = 10 (`src/pathfinder/action_console/models.py:514`), while every cards fetch is ≥24 cards due to Riley's service floor (`src/pathfinder/local_reaction_client.py:23,48`).

## 2. Per-file verdicts

| file | verdict | evidence |
|---|---|---|
| src/pathfinder/local_reaction_client.py | KEEP | Default-provider glue, small, fail-closed; wired at action_console/persona_response.py:627-646. Memo dict is unlocked (see §7) but the file ports as-is. |
| src/pathfinder/local_reaction_runner.py | PORT | Prompts, injection fencing (local_reaction_runner.py:59-101), and fail-closed walk (163-178) are the crown jewels; rebuild with per-persona concurrency (serial at 180-182) and a real model knob (model pinned via default ctor arg, 114-123; constructed argument-less at persona_response.py:645). |
| src/pathfinder/persona_card_snapshot.py | KEEP | Solves a real drift problem minimally; strict/fill modes + mixed-generation guard (persona_card_snapshot.py:122-133); used by env path (persona_response.py:576-599) and scripts/snapshot_persona_cards.py:37. |
| src/pathfinder/persona_cards_client.py | KEEP | 72-line fail-closed HTTP client, drift guard at 42-45; used by local client, snapshot filler, and 4 scripts. |
| src/pathfinder/persona_cards_content.py | KEEP | Calibration/validation stimuli + sha256 content fingerprint (persona_cards_content.py:78-84) pinning reaction checkpoints; used by scripts/build_cards_reaction_churn_calibration.py:32, scripts/run_cards_reaction_batch.py:47, scripts/compare_cards_reactions_to_golden.py:24. |
| src/pathfinder/persona_cards_contract.py | KEEP | 44 lines of pure wire shape; full card dict preserved so field names never rot (persona_cards_contract.py:29-44). |
| src/pathfinder/persona_client.py | DROP | Legacy sequence-react HTTP client. Cards is the default provider (persona_provider.py:24) and the deliberate direction; the only live imports of this file that matter are for `PersonaServiceError` (runner.py:89, local_reaction_client.py:19, synthetic_persona_evaluator.py:15) — relocate that one class (≈2 lines) into persona_contract and delete the client + its 429 retry loop (persona_client.py:71-97) with the legacy provider branch (persona_response.py:673-731). |
| src/pathfinder/persona_contract.py | KEEP | The locked request/response dataclasses every layer shares (persona_contract.py:13-81); forward-compatible parsing at 67-81. |
| src/pathfinder/persona_fixture.py | KEEP | Deterministic evaluator-truth fixture used by 6 test modules (e.g. tests/test_persona_reward.py:15, tests/test_synthetic_persona_evaluator.py:5). One fix at port time: production reaction_churn_transform.py:35 imports it only to serve `identity_truth()`, which has zero production callers — move/delete that classmethod so prod no longer imports a fixture. |
| src/pathfinder/persona_gate.py | PORT | Port = `ReactionSignificanceGate` + `SIGNIFICANCE_FLOOR_PP` + `significance_floor_fraction` (persona_gate.py:48-117) — the only gate live on the default path (persona_response.py:670). `RegionAwareGate` (120-140) has zero callers anywhere in src/, scripts/, or tests (grep). `CalibrationGate` survives only the legacy branch (persona_response.py:728) and a never-consulted arg at persona_response.py:941. |
| src/pathfinder/persona_pairing.py | DROP | Only callers are two superseded calibration scripts: the sign-forcing July build (scripts/build_cards_reaction_churn_calibration.py:37) and the 2026-06-24 legacy artifact mint (scripts/build_frozen_reaction_churn_calibration.py:29). The script that minted the live artifact does its own cohort/churn join (scripts/fit_cards_churn_calibration.py:143,186) and does not import this. |
| src/pathfinder/persona_pairs_io.py | DROP | No callers outside tests/test_persona_pairs_io.py:3 — grep over src/ and scripts/ finds only its own definitions. Dead envelope format. |
| src/pathfinder/persona_provider.py | PORT | Env validation is worth keeping (persona_provider.py:22-27); the two-entry label/mode registry (12-19) collapses to ~3 constants once the legacy provider is dropped. Callers: persona_response.py:58, reaction_provider.py:10, viewer/app.py:77, live_view.py:24. |
| src/pathfinder/persona_reward.py | KEEP | The statistical core: paired deltas, beta-SE propagation with shared t-quantile so ci_lower ≤ direction_ci_lower by construction (persona_reward.py:132-173), pivot-as-no-touch-anchor default (93-94). Dense, carefully argued, well-tested (tests/test_persona_reward.py, tests/test_persona_reward_group.py). |
| src/pathfinder/persona_shadow.py | DROP | Cutover-evidence tool hard-coding "Railway Legacy as the only authority" (persona_shadow.py:51,77) for a cutover that already happened (cards is the default provider, persona_provider.py:24). No src/ callers; only scripts/shadow_compare_providers.py:15 and scripts/summarize_shadow_run.py:9 + its test. Purpose fulfilled. |
| src/pathfinder/synthetic_persona_evaluator.py | PORT | Core paired-walk orchestration, prefix cache, fail-closed abstain (synthetic_persona_evaluator.py:136-172) all port. Rebuild cleanly: delete the RegionAwareGate duck-typing (175-181, serves a dead class), narrow the lock so it isn't held across the LLM call (124-134), and drop the zero-filled EvaluatorResult ceremony (unsub/complaint/arpa deltas at 211-214, 222-224) if the contract is rebuilt too. |

Tally: KEEP 8, PORT 4, DROP 4.

## 3. Ponytail findings (biggest cut first)

1. **delete:** legacy sequence-react path — persona_client.py (97 loc) + persona_response.py:673-731 branch + 7 legacy env vars; keep only `PersonaServiceError` relocated (persona_provider.py:24 makes cards the default; the fork path is a second backend the north star doesn't need).
2. **delete:** persona_shadow.py (167 loc) + scripts/shadow_compare_providers.py + scripts/summarize_shadow_run.py — cutover evidence already served; its authority model is the past (persona_shadow.py:51).
3. **delete:** RegionAwareGate (persona_gate.py:120-140) and its duck-typed support in synthetic_persona_evaluator.py:175-181 — zero callers in src/scripts/tests.
4. **delete:** persona_pairing.py (53) + persona_pairs_io.py (44) — superseded by fit_cards_churn_calibration.py's own join (fit_cards_churn_calibration.py:143,186); pairs_io has no non-test callers at all.
5. **delete:** `ReactionChurnTransform.identity_truth` + the prod→fixture import (reaction_churn_transform.py:35,111-115) — no production callers; kills the inverted dependency on persona_fixture.
6. **yagni:** persona_provider registry (44 loc → ~5) once there is one provider; label/mode maps for a two-value enum are dressing.
7. **shrink:** three stacked dedup layers do the same job — MemoizingTransport (persona_response.py:73-100), evaluator prefix cache (synthetic_persona_evaluator.py:115-134), LocalReactionClient memo (local_reaction_client.py:32,43-46). A rebuild needs exactly one, at the client.
8. **shrink:** CalibrationGate to legacy-only; the instance at persona_response.py:941 is constructed but never consulted (final_choice_panel calls `_client.react` directly, never `evaluate`).
9. **shrink:** `_abstain`'s zero-filled metrics (synthetic_persona_evaluator.py:219-227) exist only to satisfy EvaluatorResult's wide contract — trim with the contract.

## 4. Concern findings

**C2 (how are reactions tied to churn; churn risk overinflated?) — mechanism found; overinflation historically real, now bounded.**
Mechanism: churn = sigmoid(logit(baseline_g) + alpha + beta·(reaction − pivot)) with real per-(segment|plan|tenure) baselines (reaction_churn_transform.py:62-70); reward = −mean paired delta computed from each persona's **final-step reaction only** (persona_reward.py:40-46,113-129). Shipped artifact: beta=−0.359, beta_se=0.0264, alpha=+0.2745, pivot=4.2498, 166 group baselines (data/v1l/frozen/reaction_churn_calibration_cards.json). Near the 8.7% global baseline the sigmoid slope ≈0.079, so one reaction point ≈2.8pp churn shift — reactions are "very marginal" only on the 3–7 scale; the transform amplifies them.
FOR overinflation (historical): the pre-Task-14 single pivot=5.0 silently absorbed the level offset and "mis-anchored first-touch screening by roughly −3pp" (reaction_churn_transform.py:25-28); the old 2.9pp significance floor was itself inflated by that same artifact and was corrected to 1.0pp (persona_gate.py:39-48).
AGAINST (current): the system explicitly does not trust the magnitude — the live gate is direction-only over a paired t bound plus the 1.0pp floor (persona_gate.py:82-117, wiring persona_response.py:670-672), the magnitude CI carries propagated beta SE (persona_reward.py:132-155), and goal clearing uses ci_lower, not the point estimate (persona_response.py header, lines 8-16).

**C3 (3 personas = $; individual-pro persona ideal?) — confirmed 3-persona search; per-pro personas structurally blocked today.**
Search panel is literally 3 (persona_response.py:532; models.py:513); confirm 12 (models.py:520); yet every fetch pulls ≥24 cards because Riley's service rejects less, then slices to 3 (local_reaction_client.py:23,47-57) — cards are cheap, the LLM walk is the cost (comment at local_reaction_client.py:51-54), so the small panel IS the cost lever. Personas are segment archetypes keyed by 16-box cell (persona_card_snapshot.py:142-144); the entire panel identity is (segment, panel_size, seed) (persona_card_snapshot.py:45-53) and the evaluator's segment_of reads only `candidate["segment"]` (persona_response.py:661). Nothing in this stack accepts an org/pro id. New-repo decision: an individual-pro persona requires a new panel identity keyed by org/pro plus either Riley API support or local card synthesis from org context; the walk/scorer/reward layers would carry over unchanged since they are card-agnostic (persona_cards_contract.py:1-7 keeps the full card dict precisely so field shape is free).

**C4 (persona reactions vs. real historical churn had no correlation) — confirmed in the repo's own words for the July data; current artifact is a genuinely learned fit.**
FOR: scripts/build_cards_reaction_churn_calibration.py:148-155: "reaction<->churn has no real signal in this data (build_on_top_experiment.py: corr ~ +0.11, predicts worse than the mean), so the fitted slope's SIGN is noise and came out positive… Force it negative." The `sign_forced` field (reaction_churn_transform.py:49) and the acceptance of `slope_sign_forced_negative` artifacts (persona_response.py:540-556) exist because of exactly this.
AGAINST (current): the shipped cards artifact has a genuinely learned beta=−0.359 with beta_se and **no** sign-forced key (data/v1l/frozen/reaction_churn_calibration_cards.json; writer contract at scripts/fit_cards_churn_calibration.py:435-438 "NO slope_sign_forced_negative key — this is a genuinely learned calibration"). Residual caution: the production gate still refuses to trust magnitude (persona_gate.py:83-90), i.e. the codebase itself treats the correlation as directionally-only established.

**C5 (scoring logic questionable) — mixed; the questionable parts are pinpointable.**
Questionable: (a) a whole sequence's churn state is judged by the reaction to the LAST touch only (persona_reward.py:40-46) — earlier-touch damage that recovers by the final touch vanishes; (b) search decisions ride a paired t on n=3 with t(0.975,2)=4.30 (persona_reward.py:169-171; models.py:513) — honest but extremely low-powered; (c) `win_rate` is a binary 1.0/0.0 dressed as a rate (synthetic_persona_evaluator.py:210); (d) the 6.0/4.0 readout thresholds are unexplained constants (synthetic_persona_evaluator.py:38-43). Sound: paired same-seed design; Sdg is the same scale Riley's system calibrates (local_reaction_runner.py:7-11); fail-closed on every scoring failure (local_reaction_runner.py:145-150, synthetic_persona_evaluator.py:153-167); t chosen over z deliberately against ~70-candidate look-elsewhere (persona_reward.py:163-168).

**C6 (hard getting 50 running at once; what else speeds it up?) — root causes found in this unit.**
(a) The panel walk is fully serial: personas one-at-a-time, touches one-at-a-time, 2 LLM round-trips per persona-touch (local_reaction_runner.py:180-182 list comprehension; 163-178 loop). One confirm evaluation = 12 personas × touches × 2 calls, strictly sequential inside ONE thread. Per-persona walks are independent by construction — this is the single biggest available speedup.
(b) The evaluator's prefix-cache lock is held ACROSS the network/LLM call (synthetic_persona_evaluator.py:124-134): any cache-miss prefix reaction serializes all 10 scoring threads (concurrency wired at persona_response.py:796-797,877). When all candidates share one prefix this is intended dedup; with several prefixes/segments in flight it degrades concurrency toward serial — "starting 50 is different from them actually running".
(c) `LocalReactionClient._memo` is an unlocked dict (local_reaction_client.py:32,43-46): two threads asking the same key both miss and both pay for a full panel walk.
Beyond parallelization: three cache layers already exist (see §3.7) — consolidating and then parallelizing per-persona walks and per-touch scorer calls is where the time is; panel size (3) is already minimal.

**C7 (expensive, slow, can't swap model, inaccurate cost) — CONFIRMED on all three code-visible axes, with root causes.**
Model swap: `LocalReactionRunner()` is constructed with no arguments (persona_response.py:645), so the reaction model is pinned to `DEFAULT_MODEL` = "claude-sonnet-4-6" (llm_tooling.py:19; local_reaction_runner.py:114-123). The env knob `PATHFINDER_ACTION_CONSOLE_PERSONA_MODEL_KEY` flows only into response *metadata* (local_reaction_runner.py:181-188 — model_key lands in PanelMeta, never selects an LLM). The scorer is pinned to "gpt-4o-mini" (reaction_scorer.py, `SCORING_MODEL`). There is no way to swap either model without code changes on the default path.
Cost accuracy, root cause 1: LLM usage is recorded into a ContextVar-bound ledger (llm_usage.py:200-222) opened in the runner's thread (runner.py:946), but `estimate_many` submits scoring to a ThreadPoolExecutor with plain `pool.submit` (persona_response.py:877-886) and ContextVars do not propagate to worker threads (verified empirically: a worker's `.get()` returns the default None). So `record_response` no-ops for every persona and scorer call made during the search wave — **the most expensive call family in the system is invisible to the cost ledger**.
Cost accuracy, root cause 2: even where recorded, "gpt-4o-mini" is absent from `PRICES_USD_PER_MTOK` (llm_usage.py:33-42, Anthropic-only), and the ledger deliberately nulls `llm_cost_usd` when any model is unpriced (llm_usage.py:186-190) — so any run whose scorer usage IS captured reports cost = None.

**C10 (Karpathy method) — out of unit; one supporting note.** The evaluator's prefix cache and prefix-vs-prefix+candidate design (synthetic_persona_evaluator.py:115-152) is what makes committed-prefix, branch-per-candidate search cheap; verdict on the loop itself belongs to the runner unit.

**C14 (lots of env vars / long names) — confirmed for this seam.** The persona seam alone reads: PATHFINDER_REACTION_PROVIDER, PATHFINDER_PERSONA_CARDS_URL/_API_KEY/_TOKEN/_SNAPSHOT, PATHFINDER_ACTION_CONSOLE_PERSONA_MODEL_KEY, PATHFINDER_ACTION_CONSOLE_REQUIRE_FROZEN_SUBTYPE (persona_response.py:604-672) plus the legacy set PATHFINDER_PERSONA_SERVICE_URL/_TOKEN/_API_KEY/_TIMEOUT/_429_RETRIES/_429_RETRY_DELAY_S and PATHFINDER_ACTION_CONSOLE_PERSONA_TIMEOUT_S (persona_response.py:673-712) — ≈14 variables, half of which die with the legacy provider (§3.1).

**C15 (still pulling from Supabase) — not reproducible in this unit.** No file here touches Supabase; the persona path is Railway cards + local LLM calls only (imports in all 16 files: httpx, anthropic, numpy/scipy, stdlib).

**C16 (long unstructured output) / C18 (is churn-risk testing in the output?) — C18 YES at this layer.** Every scored result carries reduction, ci_lower/upper, direction_ci_lower, n, calibrated-range flag, group_key/confidence, and per-persona readouts in diagnostics (synthetic_persona_evaluator.py:189-203). Persona rationale text is bounded to 3–5 sentences by prompt (local_reaction_runner.py:65) and state entries to 120 chars (local_reaction_runner.py:160); the wall-of-text problem is downstream of this unit.

**C17 (use actual message copy) — partially blocked at calibration.** The calibration and validation stimuli are canned, generic per-action_type copy (persona_cards_content.py:6-71), so the reaction→churn fit was learned on synthetic copy; live scoring does react to the idea's real content (`touch.content` from the candidate, synthetic_persona_evaluator.py:76-82). If real copy enters the loop (C17), the calibration stimuli should follow — the `content_version()` fingerprint (persona_cards_content.py:78-84) already exists to detect exactly that drift.

**NOVEL-1 (with C7): silent cost blindness via thread-context loss** — persona_response.py:877-886 + llm_usage.py:200-222; fix is one line (`pool.submit(contextvars.copy_context().run, ...)` or passing the ledger explicitly).
**NOVEL-2: production imports a test fixture** — reaction_churn_transform.py:35 imports persona_fixture solely for the caller-less `identity_truth()` (reaction_churn_transform.py:111-115).
**NOVEL-3: triple-stacked memoization** — MemoizingTransport (persona_response.py:73-100), evaluator prefix cache (synthetic_persona_evaluator.py:115-134), client memo (local_reaction_client.py:32) all dedup the same requests; the `_transport = None` sentinel handshake (local_reaction_client.py:27, persona_response.py:751-760) exists only to stop them double-wrapping.
**NOVEL-4: decorative gate** — final_choice_panel builds `CalibrationGate(min_panel=1, authoritative=True)` (persona_response.py:941) but never calls `evaluate`, only `_client.react` (persona_response.py:956+); the gate is dead weight there.
**NOVEL-5 (C1-adjacent): swallowed misconfiguration** — `_get_evaluator`'s `except Exception: return None` (persona_response.py:820-822) turns an inverted calibration, missing snapshot, or bad URL into the generic "persona service not configured" (persona_response.py:833-842), hiding the real cause from the UI.
**NOVEL-6: docs/memory drift on the significance floor** — external notes say 2.9pp; code is 1.0pp with the correction narrative inline (persona_gate.py:39-48). Code is truth: 1.0.

## 5. Entry points and callers

| file | reachable via (grep evidence) |
|---|---|
| local_reaction_client.py | persona_response.py:627 (default provider build); tests/test_local_reaction_client.py, tests/test_cards_path_integration.py:12, tests/test_reaction_provider_selector.py:29 |
| local_reaction_runner.py | local_reaction_client.py:17; persona_response.py:628; scripts/build_cards_reaction_churn_calibration.py:30, scripts/run_cards_reaction_batch.py:45, scripts/compare_cards_reactions_to_golden.py:22 |
| persona_card_snapshot.py | persona_response.py:576 (env-gated pin); scripts/snapshot_persona_cards.py:37; tests/test_persona_card_snapshot.py:21 |
| persona_cards_client.py | local_reaction_client.py:18; persona_card_snapshot.py:30; persona_response.py:629; scripts (4: build_cards…, run_cards…, snapshot…, validate_persona_cards.py:16) |
| persona_cards_content.py | scripts/build_cards_reaction_churn_calibration.py:32, scripts/run_cards_reaction_batch.py:47, scripts/compare_cards_reactions_to_golden.py:24; tests/test_persona_cards_content.py |
| persona_cards_contract.py | persona_cards_client.py:12; persona_card_snapshot.py:31; local_reaction_runner.py:26; tests |
| persona_client.py | persona_response.py:51 (legacy branch + memo rewrap); runner.py:89 (error type only); synthetic_persona_evaluator.py:15; local_reaction_client.py:19 (error type only); tests |
| persona_contract.py | persona_client.py:12; local_reaction_runner.py:27; local_reaction_client.py:20; persona_reward.py:17; synthetic_persona_evaluator.py:16; persona_response.py:954; 3 scripts; tests |
| persona_fixture.py | reaction_churn_transform.py:35 (prod — see NOVEL-2); tests only otherwise (test_persona_reward.py:15, test_synthetic_persona_evaluator.py:5, test_evaluator_group_key.py:6, test_action_console_persona_response.py:14, test_action_console_live_like.py:24, test_reaction_churn_transform.py:3) |
| persona_gate.py | synthetic_persona_evaluator.py:17; persona_response.py:52-56,670,728,941; tests (test_reaction_significance_gate.py, test_reaction_provider_selector.py:44). RegionAwareGate: NO callers anywhere. |
| persona_pairing.py | scripts/build_cards_reaction_churn_calibration.py:37; scripts/build_frozen_reaction_churn_calibration.py:29; tests/test_persona_pairing.py |
| persona_pairs_io.py | tests/test_persona_pairs_io.py:3 ONLY (no src/ or scripts/ callers) |
| persona_provider.py | persona_response.py:58; action_console/reaction_provider.py:10; action_console/live_view.py:24; viewer/app.py:77; persona_shadow.py:13; tests |
| persona_reward.py | synthetic_persona_evaluator.py:18; persona_gate.py:12; tests (test_persona_reward.py, test_persona_reward_group.py, test_reaction_significance_gate.py:6) |
| persona_shadow.py | scripts/shadow_compare_providers.py:15; scripts/summarize_shadow_run.py:9; tests/test_persona_shadow.py ONLY (no src/ callers) |
| synthetic_persona_evaluator.py | persona_response.py:60 (both provider branches, final_choice_panel); tests (test_synthetic_persona_evaluator.py, test_cards_path_integration.py:13, test_evaluator_group_key.py:9, test_action_console_live_like.py:26) |

## 6. Integrations touched

- **Railway persona-cards (Riley's service):** POST `{base}/api/persona-cards` + GET refetch (persona_cards_client.py:57-72); 24-panel floor honored client-side (local_reaction_client.py:23). Auth = Bearer and/or X-API-Key headers (persona_cards_client.py:26-31).
- **Railway legacy sequence-react (our fork):** POST `{base}/api/sequence-react` with 429 retry + Retry-After (persona_client.py:71-97). Legacy provider only.
- **Anthropic:** direct `anthropic.Anthropic().messages.create` per persona-touch (local_reaction_runner.py:137-138); model fixed to llm_tooling.DEFAULT_MODEL.
- **OpenAI:** indirectly, via DoubtGapScorer (`reaction_scorer.py`, other unit) called at local_reaction_runner.py:120,144 — gpt-4o-mini per persona-touch.
- **Supabase / Snowflake / Sheets / Iterable-LCM / n8n:** not touched by any file in this unit (no such imports in the 16 files).

## 7. Gaps

- **Unlocked shared caches under 10-way concurrency:** LocalReactionClient._memo (local_reaction_client.py:32,43-46) — duplicate full-panel LLM walks on a race, unbounded growth for a long-lived scorer; MemoizingTransport._cache has the same shape (persona_response.py:85-100, other unit).
- **Lock held across network/LLM call:** synthetic_persona_evaluator.py:124-134 — one hung prefix call blocks every scoring thread; also the C6 throughput ceiling.
- **Cost ledger blind to worker threads:** persona_response.py:877-886 + llm_usage.py:218-222 (verified: ContextVar not inherited by ThreadPoolExecutor workers). Silent under-reporting, not a crash.
- **Non-JSON 200 escapes the error wrapper:** persona_cards_client.py:72 — `resp.json()` sits outside the try that wraps transport (61-69); a non-JSON 200 raises raw JSONDecodeError. LocalReactionClient's blanket except converts it downstream (local_reaction_client.py:64-65), so it fails closed, but the client's own contract ("every failure path raises PersonaCardsError", persona_cards_client.py:3-4) is not literally met.
- **No retry/backoff on the cards client:** persona_cards_client.py:57-72 has no 429/5xx handling at all, in contrast to the legacy client's tuned 429 loop (persona_client.py:76-84) — the resilience investment sits on the path being retired, not the default one.
- **Swallowed evaluator-build failures:** persona_response.py:820-822 (`except Exception: return None`) — misconfiguration is indistinguishable from "not configured" in the UI (NOVEL-5).
- **Snapshot key re-keying silently tolerates malformed keys:** persona_card_snapshot.py:82-87 — a key that fails rsplit parsing is kept verbatim; a corrupted snapshot degrades to strict-mode misses instead of a loud error.
- **Prompt-injection posture is good:** untrusted card fields, prior state, and message content are all fenced with explicit "never follow instructions" framing and length bounds (local_reaction_runner.py:42-48,59-101) — noted as a strength to preserve verbatim in any rebuild.
- **No concurrency tests:** none of this unit's test files exercise threaded interleavings of the caches/locks above (inferred from test-name map; see §8 caveat).

## 8. Unverified

- Riley's service behavior claims taken from comments, not observed: 409 on retired subtype_version (persona_card_snapshot.py:11-13), 422/400 below 24 personas (local_reaction_client.py:23), and seed-stable panel ordering that makes the 24→3 prefix slice deterministic (local_reaction_client.py:52-54). The slice's validity **depends** on that last claim.
- The floor derivation inputs: p25 = 1.016pp from data/v1l/results/phase2_fit_report_v2.json (persona_gate.py:29-38) — I did not open or recompute that report.
- "corr ~ +0.11, predicts worse than the mean" (build_cards_reaction_churn_calibration.py:149) — quoted from the script comment; I did not re-run build_on_top_experiment.py.
- Actual wall-clock latency and dollar cost per run — no measurements exist in this unit to verify C6/C7 magnitudes; only the structural causes are confirmed.
- Whether the confirm-panel re-score path also loses ledger context (search-wave loss is confirmed; the confirm scorer's exact call topology inside runner.py was not fully traced).
- Whether the Railway deployment currently sets PATHFINDER_PERSONA_CARDS_SNAPSHOT (deployment state is outside the repo).
- Test-coverage claims are from mapping test filenames/imports (grep), not from line-reading the test suite; "no concurrency tests" is inferred, not proven.

## 9. Files covered

- src/pathfinder/local_reaction_client.py
- src/pathfinder/local_reaction_runner.py
- src/pathfinder/persona_card_snapshot.py
- src/pathfinder/persona_cards_client.py
- src/pathfinder/persona_cards_content.py
- src/pathfinder/persona_cards_contract.py
- src/pathfinder/persona_client.py
- src/pathfinder/persona_contract.py
- src/pathfinder/persona_fixture.py
- src/pathfinder/persona_gate.py
- src/pathfinder/persona_pairing.py
- src/pathfinder/persona_pairs_io.py
- src/pathfinder/persona_provider.py
- src/pathfinder/persona_reward.py
- src/pathfinder/persona_shadow.py
- src/pathfinder/synthetic_persona_evaluator.py
