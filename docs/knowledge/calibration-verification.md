# C4 Adversarial Verify — "How personas react vs. how pros actually churn historically had no correlation"

Phase 4 CONCERN-skeptic report. Every root-cause claim about C4 in the Phase 2/3 reports
(units U1, U2, U3, U5, U7, U9a, U9c, U11a, U11b, U13, U14; lanes L1; knowledge D1, D2,
D5a, D5b, D6a, D6b) was extracted, deduplicated to 9 distinct claims, attacked with the
strongest alternative explanation I could construct, and tested against the code. Code
was read only; no report under attack was modified.

Verdict key: **CONFIRMED** = survived the attack. **REFUTED** = the alternative wins;
corrected diagnosis given. **UNRESOLVED** = needs a human or a live run.

---

## Claim 1 — "No correlation" was empirically REAL on the July (pre-2026-07-28) data

**Sources:** U2 §C4, U3 §C4, U11a §C4, U13 §C4, D6a §C4, D6b §C4.
**Claim:** raw reaction↔churn correlation was ~+0.11 (backwards), the fitted slope's
sign was noise and came out positive, and the interim fix was to force the sign
negative — an asserted, not learned, correlation.

**Alternative tested:** the "+0.11" comment describes a side experiment that never
shipped; the sign-forced regime is a report-level myth or the artifact never existed.

**Outcome: alternative loses. CONFIRMED.**
- `scripts/build_cards_reaction_churn_calibration.py:148-157` — verbatim: "reaction<->churn
  has no real signal in this data (build_on_top_experiment.py: corr ~ +0.11, predicts
  worse than the mean), so the fitted slope's SIGN is noise and came out positive
  (= happier persona -> higher churn, backwards). Force it negative…"; line 157:
  `directional_slope = -abs(transform.slope)`; the artifact dict writes
  `"slope_sign_forced_negative": True` (line ~165).
- The measuring experiment exists and is honest: `scripts/build_on_top_experiment.py:36-55`
  (LOO MAE per dim vs a predict-the-mean floor), closing print at :78-80: "If nothing
  beats the predict-mean floor… the signal is structurally weak."
- The failed fit's artifact is still on disk: `data/v1l/frozen/reaction_churn_calibration_cards_tuned.json`
  — `slope: +0.03435…`, `authoritative: false`, `holdout_mae: 0.1717` (read directly).
- Locked in a shipped test: `tests/test_cards_calibration_direction.py:1-11` docstring
  states the same; `test_positive_tuned_calibration_is_explicitly_non_authoritative`
  (:38-42) asserts slope>0 and authoritative=False on that artifact.

## Claim 2 — The current shipped artifact is a genuinely LEARNED Exit-A fit; C4 as stated is not reproducible on the current artifact chain

**Sources:** U2 §C4, U3 §C4, U11a §C4, U14 §C4, D1 §C4, D6a §C4.
**Claim:** the Phase-2 pre-registered fit exited A (β=−0.359, β_se=0.0264, CI excl. 0,
LOO beats the raw-baseline floor, sign-robust in 3 sub-specs); the shipped artifact is
that fit, with no sign-forced key; guard tests hard-fail a reversion.

**Alternatives tested:** (a) artifact/fit mismatch (run_id-clobber-style drift);
(b) the exit gate is softer than reported; (c) the learned artifact is not what the
live path loads; (d) the "signal" is a spurious win over a strawman floor.

**Outcome: (a)–(c) refuted as alternatives; (d) partially lands but is already
disclosed in the reports. CONFIRMED.**
- (a) `data/v1l/frozen/reaction_churn_calibration_cards.json` carries
  `beta: -0.3590560839101312, beta_se: 0.026411026168919282, alpha: 0.2745, pivot: 4.2498,
  holdout_mae: 0.0493, baselines: 166 keys`, and **no** `slope_sign_forced_negative`
  key (read directly). Its beta equals `data/v1l/results/phase2_fit_report_v2.json`
  `primary.beta` to the last digit — the artifact IS the v2 (Task 14/15-corrected) fit.
- (b) `scripts/fit_cards_churn_calibration.py:240` — `e1 = bool(primary_fit.beta < 0 and
  primary_fit.beta_ci95[1] < 0)`; :248-249 E2 against the pre-registered RAW baseline
  floor; :277-295 three robustness refits, `exit_scalar = "A" if (e1 and e2 and e3)`;
  writer contract :439-444: "NO slope_sign_forced_negative key -- this is a genuinely
  learned calibration". Report on disk: `exit: "A"`, all six criteria true, n=63,510,
  54 cells, LOO mae_model 0.0493 vs raw floor 0.0723 vs global-mean 0.1763.
- (c) The live path defaults to this artifact: `persona_provider.py:24` default
  `persona-cards`; `persona_response.py:65-69` `CARDS_CALIBRATION_PATH =
  data/v1l/frozen/reaction_churn_calibration_cards.json`; loaded at :648;
  `frozen_calibration.py:45` `sign_forced = bool(data.get("slope_sign_forced_negative",
  False))` → False here, so `persona_response.py:506` resolves
  `calibration_confidence = "learned"`. `_validate_cards_direction`
  (persona_response.py:553) checks `transform.beta` (the learned β) on this shape.
  Shape-locked by `tests/test_cards_calibration_direction.py:45-70` (sign_forced is
  False, baselines present, beta<0) and `tests/test_frozen_calibration.py:38-45,116-119`.
- (d) The strongest attack: the fit report's own `honest_floor_diagnostic` (v2 report,
  verified) — model vs LEVEL-CORRECTED (α-only) floor ratio 0.9576, bootstrap CI
  [0.8185, 1.1184], P(≤0.95)=0.462, "inconclusive at n=54 cells… does not gate the
  exit". So E2's win is over the raw floor; incremental predictive lift of the reaction
  term beyond corrected baselines is unproven. Also the exposure is observational
  plurality-of-events (`calibration_cohort.py:38-49` dominant action type + tie-drop),
  covariate-adjusted, not randomized. **These caveats are stated inside the very reports
  making the claim** (D1 "incremental predictive lift beyond baselines still unproven";
  U2 "association… not a causal effect") — they qualify Claim 2, they do not refute it.

## Claim 3 — "Was real in July" vs "contradicted by the shipped Exit-A fit": consistent or contradictory?

**Sources:** the apparent tension across U2/U3/U13/U11a/D6a/D6b.

**Outcome: CONSISTENT — different artifacts, different dates, different designs. Not a
contradiction between reports. CONFIRMED (as a consistency finding).**
- July finding: pooled affine **cell-mean** fit (56 cells, means-on-means, no
  covariates) on `cards_reactions_ckpt` reactions joined to cell churn → corr +0.11,
  noise slope (`build_cards_reaction_churn_calibration.py:148-156`; tuned artifact on
  disk, `subtype_version 273e9fe…`).
- 2026-07-28 finding: **org-level** logistic (n=63,510) with group baselines, covariates,
  segment FE, exposure-weighted pivot + separate fitted α, cluster-robust SEs
  (`fit_cards_churn_calibration.py:230-296`; artifact `subtype_version 22cc4a1c…`).
- The mechanism reconciling them is stated and plausible: raw correlations were positive
  because of a targeting confound; after covariate adjustment the association is
  strongly negative (D6a/D6b citing the cutover design). Both artifacts coexist on disk;
  every report that asserts "was true" also time-indexes it ("July data", "cell-mean
  era", "pre-2026-07-28"). No report claims both states for the same artifact.

## Claim 4 — Live-path root cause: persona↔historical correlation is structurally UNWIRED at run time

**Sources:** U1 §C4 ("the comparison is unplugged"), U9a §C4 ("unwired, not broken").
**Claim:** (a) the `historical` corroboration hook is never injected by any live caller,
so every UI/batch run defaults it to None → `{"status": "insufficient_evidence"}`;
(b) the one measured-churn input, `measured_action_priors`, keys on `filters["cell"]`,
so org-mode runs (the primary live path) get None.

**Alternative tested:** some non-viewer caller (script/CLI) wires `historical=`, or the
corroboration result feeds scoring some other way.

**Outcome: alternative loses. CONFIRMED.**
- `src/pathfinder/viewer/app.py:431, 1008, 1049, 1094` — all four constructions are
  `ActionConsoleRunner(self._runs_base_dir, sink=self._sink)`; no `historical`.
- Repo-wide grep for `historical=` in `src/` + `scripts/` (excluding tests): only
  `runner.py:1687, 1751, 1785` — the runner's own forwarding. Default is None
  (`runner.py:1738-1744` sets `self.historical = historical`).
- `runner.py:1217-1222`: `if historical is not None … else corroboration =
  {"status": "insufficient_evidence"}` — comment: "corroboration only; never gates".
- `_consolidate_score` (`runner.py:424-428`) reads ONLY persona fields; historical
  corroboration never touches `churn_risk_reduction_pp`.
- `runner.py:1060-1065`: `run_segments = [ … for value in (filters.get("cell") or []) ]`;
  `action_priors = measured_action_priors(run_segments) if run_segments else None`.
  Batch org runs go through `_batch_run_org` (`app.py:428-449`) with org filters and
  `audience_mode="org"` — no `cell` key, so priors are None on that path.

## Claim 5 — Structural: the real-history estimation chain and the persona path never meet in data; nothing forces the correlation to ever be measured

**Sources:** U7 §C4, U11b §C4, D2 §C4, D5a §C4.
**Claim:** `frame_provider` computes real per-org churn outcomes; the persona path's only
import from that world is `segment_vocab`; shadow comparisons are provider-vs-provider;
no contract/schema exists for a reaction-vs-realized-churn backtest.

**Alternative tested:** the calibration pipeline IS the bridge, making "never meet"
false.

**Outcome: CONFIRMED, with the boundary the reports themselves drew.**
- `src/pathfinder/frame_provider.py:73` — `pop["outcome_m3"] = 1.0 - pop["churned_m3"]…`
  (real per-org outcome, with MATCH_KEYS arm assignment at :65-70).
- `persona_response.py:59` — the only import from the extract world is
  `segment_vocab.segment_code_to_persona_key`. Grep for `frame_provider|extract_reader`
  across the persona stack (action_console/, synthetic_persona_evaluator, persona_reward,
  local_reaction_runner): zero matches.
- `scripts/shadow_compare_providers.py:60-66` — `compare_candidate(…,
  railway_provider=railway, cards_provider=cards)`: persona provider vs persona
  provider; no churn outcome anywhere in the loop.
- The calibration pipeline (calibration_cohort → fit) does join reactions to real churn,
  but at (segment, action_type) cell grain, offline, producing a frozen artifact — it is
  a fit, not a recurring predicted-vs-realized backtest, and no schema in
  data_contracts/ binds one (D2's point stands: nothing forces the C4 correlation to be
  measured on an ongoing basis).
- Scope note on D5a's "never wired, not measured-and-failed": correct as scoped to the
  June archive era it audits (and D5a flags the scope itself). Cross-era, the synthesis
  is: first unwired (June) → then measured-and-failed (July, corr +0.11) → then re-fit
  and passed pre-registered gates (2026-07-28 Exit A).

## Claim 6 — "The shipped cards calibration does not carry a learned slope — sign is forced negative" (current state)

**Sources:** U9c §C4 FOR-branch ("the shipped cards calibration does not carry a learned
reaction→churn slope — sign is forced negative (persona_response.py:552-556)"); echoed
softer by L1 §C4 ("the calibration magnitude is currently NOT validated (sign-forced
accepted… the code itself concedes C4 for magnitude)").

**Alternative tested:** the cited docstring is stale pre-Exit-A text, and the actual
shipped artifact is the learned fit.

**Outcome: the alternative WINS. REFUTED.**
- The evidence U9c cites is a docstring, not the artifact:
  `persona_response.py:541-556` (`_validate_cards_direction`) says a
  `slope_sign_forced_negative` artifact "is the expected, current state of the shipped
  cards calibration" — text written for the 2026-07-27 interim cutover and never
  updated after the 2026-07-28 Exit-A re-fit.
- The artifact the live path actually loads (`CARDS_CALIBRATION_PATH`,
  persona_response.py:65-69 → `reaction_churn_calibration_cards.json`) has **no**
  sign-forced key; `frozen_calibration.py:45` therefore loads `sign_forced=False`, and
  `persona_response.py:506` labels estimates `calibration_confidence="learned"`, not
  `"directional_prior"`. `beta=-0.359` with `beta_se` present switches the magnitude CI
  to propagated beta uncertainty (`frozen_calibration.py:36-41`).
- Tests forbid the state U9c describes: `tests/test_cards_calibration_direction.py:59-63`
  asserts `t.sign_forced is False` on the shipped artifact;
  `tests/test_frozen_calibration.py:38-45` asserts a real group-aware fit with beta<0.
- **Corrected diagnosis:** a stale docstring at `persona_response.py:546-551` (and the
  validator's continued *tolerance* of sign-forced artifacts) misdescribes the current
  shipped state. What remains true from L1: the run-time gate deliberately decides on
  direction only (`persona_gate.py:85-118`, ReactionSignificanceGate) — but that is a
  design choice about trusting magnitude, not evidence that the correlation is absent
  or asserted. L1's own hedge ("not adjudicable from the spine") keeps L1 short of a
  false claim; U9c's FOR-branch states the false current fact and is refuted.

## Claim 7 — Legacy Phase-1 loop: any correlation with real churn was impossible by construction (planted effects)

**Sources:** U5 §C4 (context evidence).

**Alternative tested:** the "planted" language is about test fixtures, not the loop's
actual scoring substrate.

**Outcome: alternative loses. CONFIRMED (scoped to the dead Phase-1 lane).**
- `src/pathfinder/bandit_proposer.py:17-19` — "on the synthetic simulator the best arm
  is *planted*, so the bandit 'finds the best option we planted,' it does NOT 'discover'
  a real-world truth." Arm list at :50-57 annotates planted true ATEs (0.00/0.03/0.06/
  0.09) per action_type. Scoped correctly by U5: this says nothing about the persona
  pipeline, only that legacy-loop outputs could never correlate with real churn.

## Claim 8 — The C4 guard tests silently no-op when the calibration artifact is absent

**Sources:** U13 §3/§5 ("half a guard").

**Alternative tested:** the guards use pytest.skip or a committed fixture, so the
"silent" part is overstated.

**Outcome: alternative loses. CONFIRMED.**
- `tests/test_cards_calibration_direction.py:28-29` and :57-58 — both the sign
  invariant and the learned-artifact shape lock begin
  `if not CARDS_CALIBRATION_PATH.exists(): return` — a plain green pass, not a skip,
  on any checkout without `data/v1l/frozen/reaction_churn_calibration_cards.json`.
  (The artifact exists on this checkout, so the guards do run here.)
- Footnote found while attacking: the first guard asserts `t.slope <= 0`, but the
  group-aware loader hardcodes `slope=0.0` for learned artifacts
  (`frozen_calibration.py:49-50`), so that particular assertion is vacuously true on
  the current artifact shape; the real protection for the learned shape is the third
  test's `t.beta < 0` (:64-66). Same conclusion as U13, one more reason the guard is
  weaker than it looks.

## Claim 9 — D5b: "no correlation" is the DESIGNED relationship (corroboration-only) plus a weak calibration, not a single bug

**Sources:** D5b §C4.

**Alternative tested:** the two numbers were meant to be reconciled and the
disconnect is drift, not design.

**Outcome: CONFIRMED for the design mechanism; one archival sub-measurement not
re-verifiable offline.**
- Corroboration-only is in the code: `runner.py:1217` comment "corroboration only;
  never gates"; `_consolidate_score` (runner.py:424-428) never reads
  `historical_corroboration`; the two numbers are never summed or averaged anywhere in
  the runner.
- "Weak calibration" leg: true of the legacy artifact on disk
  (`reaction_churn_calibration.json`: beta −0.049, holdout_mae 0.16879 — an
  absolute-level band wider than most baselines), matching U2/D5b.
- The EvaluatorM3-boundary leg ("2A Max + 6 factors → 40 → 0 orgs, so often nothing to
  correlate against") is an archival measurement I cannot re-run read-only; the gating
  machinery it describes exists (`prefix_cohort.py` support/positivity gates). Flagged
  under Unresolved but it does not change the claim's verdict.

---

## Unresolved

1. **Out-of-sample generalization of the Exit-A beta.** The fit's LOO is by-cell on the
   same extract; whether β=−0.359 holds on future cohorts (or under real message copy —
   calibration stimuli are canned per-action_type content, `persona_cards_content.py`)
   needs a fresh extract or a live backtest. No code path currently produces a recurring
   predicted-vs-realized check (Claim 5), so this stays open by construction.
2. **Incremental lift beyond level-corrected baselines.** The honest-floor diagnostic is
   pre-registered as non-gating and is inconclusive at n=54 cells (ratio 0.9576, CI
   [0.819, 1.118], P=0.462 — `phase2_fit_report_v2.json`). Whether the reaction term
   adds cell-level predictive power beyond corrected baselines needs more cells/data —
   not adjudicable from code. This is the strongest surviving kernel of C4 against the
   current fit, and the reports already carry it.
3. **D5b's "0 orgs at realistic boundaries" EvaluatorM3 measurement** — archival
   (2026-06-24 design doc); would need a live run against current extracts to re-verify.
4. **Stale docstring cleanup** (from Claim 6): `persona_response.py:546-551` still
   describes the sign-forced regime as "the expected, current state". A human should
   confirm intent and update; as-is it misled one Phase-3 report (U9c) into a wrong
   current-state diagnosis.
