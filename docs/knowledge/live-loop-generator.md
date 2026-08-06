# U9a — action_console loop core: runner.py + generator.py

Unit files (read in full, line by line):
- `src/pathfinder/action_console/runner.py` (1787 lines)
- `src/pathfinder/action_console/generator.py` (1592 lines)

Context files read/grepped one hop for caller tracing only (no verdicts issued on them): `src/pathfinder/viewer/app.py`, `src/pathfinder/action_console/models.py`, `src/pathfinder/action_console/persona_response.py`, `src/pathfinder/action_console/search_policy.py`, `src/pathfinder/action_console/llm_usage.py`, `src/pathfinder/action_console/calibration_priors.py`, `src/pathfinder/persona_gate.py`, `src/pathfinder/viewer/batch_queue.py`, `src/pathfinder/action_console/sequence_runner.py`, `src/pathfinder/action_console/breadth_critic.py`.

---

## 1. What it actually does

**runner.py** is the goal-seek loop the viewer API drives. `start_action_console_run` (runner.py:933) opens a token-usage scope then runs `_run_action_console_loop` (runner.py:950): resolve audience (or accept one pre-resolved, runner.py:1039-1040), optionally build a flag-gated Snowflake-via-n8n org context pack for org mode (runner.py:885-925, org mode requires exactly one org, runner.py:1044-1048), save a local "running" run, then loop:

1. Build per-round prompt memory from all prior ideas — `results_log`, `current_branch`/`best_so_far`, `discarded_experiments`, `stall_count`, `avoid_repeating` (runner.py:583-714) — plus a `search_directive` from `build_search_directive` (runner.py:1144-1149, always called with `goal_reached=False`, runner.py:1148).
2. Call `generate_round` for exactly ONE idea per round (`_ROUND_SIZE = 1`, runner.py:124; comment says multi-idea Anthropic calls timed out live, runner.py:120-123).
3. Gate the batch: org mode → grounding critic only; segment mode → breadth critic + applicability stamp (runner.py:1173-1189).
4. Score the idea with a synthetic-persona panel (`scorer.estimate`, runner.py:1207, sequential), attach fields, attach historical corroboration (defaults to `{"status": "insufficient_evidence"}` when no `historical` callable is injected, runner.py:1217-1222), consolidate `churn_risk_reduction_pp` (scored → persona pp; anything else → 0.0, runner.py:424-428).
5. Apply Karpathy-vocabulary branch advancement: keep if strictly `idea.score() > current_branch.score()` (runner.py:555), discard otherwise, crash if unscored (keeps prior branch, runner.py:511-524); suppressed ideas are forced discards (runner.py:530-543).
6. Persist: async best-effort Supabase upserts on a 1-worker executor (runner.py:288-321), local save per idea (runner.py:1236-1244).
7. Stop reasons: `kill_switch` (CANCEL file polled per idea, runner.py:1129, 1193), `cap_reached` (default 50 ideas), `budget_exhausted` (default 80 scorer calls), `target_reached` — only after a screen-then-confirm re-score at a larger panel (search n=3 → confirm n=12) both clear `goal_pp` at CI-lower (runner.py:1250-1293), and `search_exhausted` after 3 consecutive rounds where the generator exhausted all directive retries (runner.py:1119-1123, 1295-1303).

Post-loop (only when `run_final_panel=True`): rank ideas, run a winner-only channel run-off via personas (runner.py:1311-1351), enrich winner reasoning, run the final choice panel (runner.py:1357-1364). Both live entry points pass `run_final_panel=False` (viewer/app.py:446, 1016) — see finding N3. Terminal supabase blob gets real LLM token/cost totals from the usage scope (runner.py:1402-1406).

`validate_action_console_winner` (runner.py:1450) re-runs the final-choice panel out-of-band for a stored run. `branch_action_console_run` (runner.py:1580) creates a child run seeded with the parent winner's theme snapshot plus a fixed signal `state_mutation` (BRANCH_SIGNALS, runner.py:97-118). `ActionConsoleRunner` (runner.py:1702-1786) is a thin DI wrapper over the three module functions.

**generator.py** produces `GeneratedIdea`s. Primary path `_generate_llm_round` (generator.py:838): builds an audience fact pack (segment mode, generator.py:335-371) or org prompt (generator.py:659-662), forces a single Anthropic tool call (`tool_choice` forced, generator.py:664; temperature 0.8, generator.py:653; max_tokens = 5000/idea, generator.py:74; model from `PATHFINDER_ACTION_CONSOLE_LLM_MODEL` else `DEFAULT_MODEL` = claude-sonnet-4-6, generator.py:649, llm_tooling.py:19), validates research-judgment fields against enums, and enforces the search directive with up to 3 attempts (plain → soft prose retry → hard retry that mutates `must_avoid`, generator.py:870-896, 981-1056). Directive exhaustion raises `IdeaDirectiveExhaustedError`, which `generate_round` converts into a fall-through to the deterministic path with `generation_source=DETERMINISTIC_DIRECTIVE_EXHAUSTED_SOURCE` (generator.py:1463-1484) — the signal the runner's `search_exhausted` counter reads (runner.py:1168-1171).

Fallback path: a bank of 10 canned "plays" (generator.py:123-248) rotated by a deterministic hash offset (generator.py:276-284), fully templated idea text (`_build_idea`, generator.py:1158-1320). `generate_action_ideas` + `ActionIdeaGenerator` (generator.py:1508-1591) batch-generate to a fixed count; no production caller (see §5).

---

## 2. Per-file verdicts

| file | verdict | evidence |
|---|---|---|
| `src/pathfinder/action_console/runner.py` | **PORT** | The loop core is the north star's engine and its logic is worth preserving: guard-ordered termination (runner.py:1125-1136), screen-then-confirm (runner.py:1250-1293), keep/discard/crash ledger (runner.py:504-580), search-exhausted stop (runner.py:1295-1303), kill switch (runner.py:283-285). But port the SIMPLEST form: drop the ~200 lines of Supabase write-accounting ceremony (runner.py:332-367, 741-784, 1379-1445, 1503-1576 — three reconciliation passes for a sink that "absorbs all errors and never raises", runner.py:28), the dead `persona_rate_limited` guard (runner.py:1322), and the live-dead channel run-off (runner.py:1325-1351, see N3). |
| `src/pathfinder/action_console/generator.py` | **PORT** | The LLM round — fact pack (generator.py:335-371), forced tool schema (generator.py:374-479), research-judgment validation (generator.py:685-835), directive-miss enforcement + retries (generator.py:1059-1126, 981-1056) — is the idea engine; rebuild cleanly. Drop in transfer: the 10-play deterministic template engine (~450 lines: generator.py:123-248, 1158-1419) which fabricates canned "ideas" that still consume persona budget (its one real job — signaling directive exhaustion — is a one-line sentinel, not 450 lines); dead `generate_action_ideas`/`ActionIdeaGenerator` (generator.py:1508-1591); v4 field aliases (generator.py:724-767) that the forced tool schema makes unreachable. |

---

## 3. Ponytail findings (biggest cut first)

1. **delete:** deterministic `_PLAYS` fallback engine, ~450 lines (generator.py:123-248, 1158-1419) — canned templates masquerading as research; replace with a sentinel return so the runner stops instead of persona-scoring boilerplate (the `search_exhausted` stop already exists, runner.py:1302).
2. **shrink:** Supabase writes_attempted/succeeded/failed/pending bookkeeping with three post-hoc reconciliation dances, ~200 lines (runner.py:332-367, 741-784, 1379-1445, 1503-1576) — one honest failure counter at `drain()` covers the Task-12 incident; the rest is accounting theater for a fail-open sink.
3. **delete:** `generate_action_ideas` + `ActionIdeaGenerator`, 84 lines (generator.py:1508-1591) — only callers are tests (see §5).
4. **delete:** winner channel run-off (runner.py:1325-1351) + `_estimate_from_idea` (runner.py:393-421) — never executes on live paths (`run_final_panel=False`, app.py:446, 1016) and is a no-op with `PROPOSAL_CHANNELS = ("sms",)` (models.py:93) since there are no alternates to score.
5. **shrink:** `_idea_generation_memory` sends four overlapping views of the same history every round (`previous_ideas`, `results_log`, `discarded_experiments`, `current_branch`+`best_so_far` which are the same dict, runner.py:676-698) — one ledger table would do; also cuts per-round prompt cost (C7).
6. **yagni:** v4 alias fallbacks in `_strategy_from_raw` (`idea_text`, `persona_strategy_summary`, `why_good`, `business_rationale`… generator.py:724-767) — the tool schema (generator.py:389-410) makes the new names `required` and `additionalProperties:false`; the aliases cannot fire on the real path.
7. **delete:** `"persona_rate_limited"` in the run-off guard (runner.py:1322) — no code anywhere sets this stop_reason (repo-wide grep: only this line).
8. **shrink:** `_directive_hard_retry_request` (generator.py:996-1056) — a third attempt whose `must_avoid.problem_keys`/`mechanism_keys` are, per its own docstring, "prompt-facing context ONLY -- nothing reads them back" (generator.py:1005-1008); two attempts + fallback is the same behavior minus 60 lines.
9. **yagni:** `ActionConsoleRunner` class (runner.py:1702-1786) — a field-holder that forwards three calls; app.py could call the module functions it already imports.
10. **shrink:** `_context_for_seed` channel-stripping ritual (generator.py:287-316) exists only to stabilize the fallback engine's play offset — dies with cut #1.

---

## 4. Concern findings

**C2 (diagnose — reaction→churn tie, overinflated churn risk).** FOR: the runner promotes the persona panel's pp estimate directly to `churn_risk_reduction_pp` (runner.py:424-428) and the default target is `goal_pp = 11.0` percentage points of churn reduction for a single message idea (runner.py:954; models.py:487; app.py:991 defaults 11.0) — the loop is institutionally aimed at double-digit-pp claims. The fraction→pp conversion happens once at the adapter boundary (`_PP_SCALE = 100.0`, persona_response.py:245, 409-420). AGAINST: clearing goal requires CI-lower ≥ floor, not the point estimate (persona_response.py:136-171, floor = `SIGNIFICANCE_FLOOR_PP` = 1.0pp, persona_gate.py:48), plus a confirm re-score at n=12 (runner.py:1250-1293) — the stop path is guarded even if the displayed point estimates run hot. The calibration math itself lives outside this unit.

**C3 (design — individual-pro personas).** Nothing in this unit hardcodes 3 personas: panel sizes are config (search 3 / confirm 12 / final 10, models.py:491-497) and the scorer is an injected seam (`persona_scorer`, `confirm_persona_scorer`, runner.py:961-962, 994-1025). A custom/per-pro persona provider slots in behind `PersonaResponseScorer` with zero runner/generator changes. The generator already attaches per-idea real org_uuid samples (generator.py:264-273) that a per-pro provider could key on.

**C4 (diagnose — personas vs. actual churn: no correlation).** Root cause visible here: the loop never consults actual churn history at run time. (a) The `historical` corroboration hook defaults to `{"status": "insufficient_evidence"}` (runner.py:1217-1222) and NO live caller injects it — app.py:431 and app.py:1008 construct `ActionConsoleRunner(runs_base_dir, sink=...)` without `historical`, so `self.historical=None` (runner.py:1727) on every UI/batch run; repo-wide grep finds no non-test `historical=` caller outside runner.py's own forwarding (runner.py:1687, 1751, 1785). (b) The one measured-churn input, `measured_action_priors`, is keyed on `filters["cell"]` (runner.py:1060-1065) — org-mode runs filter on `org_uuid`, so `run_segments` is empty and priors are `None` on the primary live path (batch org runs, app.py:428-447). The artifact exists (`data/v1l/frozen/segment_action_priors.json`, present on disk) but only segment-mode runs ever see it. So correlation with historical churn isn't broken — it is unwired.

**C5 (diagnose — scoring logic questionable).** FOR: branch advancement is a strict `>` on a single point estimate from the n=3 search panel (runner.py:555; search_panel_size=3, models.py:491) — the confirm panel only fires at goal-clear time, so the champion chain is steered by 3-persona noise. Abstained/failed ideas are flattened to 0.0 (runner.py:428), so any scored-positive idea beats every abstained idea regardless of screening evidence. AGAINST: Task-13 partially compensates by threading `screening_reduction_pp` into prompt memory (runner.py:633-639, 496-501) and screen-then-confirm blocks noisy early stops (runner.py:1250-1293); suppression can never advance the branch (runner.py:530-543).

**C6 (diagnose — 50 concurrent; what else speeds it up).** Within-run, everything is serial by design: one idea per LLM request (runner.py:124, 1154), up to 3 LLM attempts per round (generator.py:870), one synchronous persona estimate per idea (runner.py:1207), local save per idea, sink writes on a single-worker executor (runner.py:290 `max_workers=1` — deliberately not a throughput path). Cross-run concurrency is batch_queue's job (out of unit). Non-parallel speedups visible here: (a) prompt grows every round — `results_log` includes ALL evaluated ideas (runner.py:592), and the fact pack + branch context are re-serialized into every request (generator.py:612-616), so late rounds are strictly slower and pricier; (b) the 2 extra directive-retry LLM calls per non-compliant round (generator.py:870-896); (c) the confirm re-score doubling panel spend at every false goal-clear (runner.py:1259).

**C7 (diagnose — cost, slow, can't swap model, inaccurate cost reporting).** AGAINST (partly fixed): real token/cost totals are now merged into the terminal run record (runner.py:1402-1406), computed from recorded Anthropic responses with an explicit "None when any model is unpriced" honesty rule (llm_usage.py:162-190); the generation model IS swappable via `PATHFINDER_ACTION_CONSOLE_LLM_MODEL` (generator.py:649) and timeout via `PATHFINDER_ACTION_CONSOLE_LLM_TIMEOUT_S` (generator.py:646). FOR: the price table is hand-maintained (llm_usage.py:33-41, "verified 2026-07-31") and goes stale silently; temperature is hardcoded 0.8 (generator.py:653); recorded call sites are generator + critics + local reaction scorer only (grep: generator.py:671, breadth_critic.py:170, grounding_critic.py:165, reaction_scorer.py:223, local_reaction_runner.py:139) — remote persona-cards service calls are not in the ledger, so reported `llm_cost_usd` understates true run cost whenever the cards provider is active (unverified which provider dominates at runtime — §8). Also `persona_call_budget` counts scorer *estimates*, each of which is an N-persona panel (runner.py:1207-1208; models.py:489-491) — the budget's unit doesn't match its name, which muddies any cost math done from it.

**C8 (diagnose — seed crucial, micro-improvements; want exploration+exploitation).** AGAINST: real explore/exploit machinery exists — the search policy phases are baseline → normal → escape (forced `new_direction` after 2 stalls) → reassess → confirm (search_policy.py:80-101), screening leaders/laggards give a gradient over abstained ideas (search_policy.py:33-67), and the prompt explicitly orders mechanism changes over rephrasing (generator.py:583-591). FOR: (a) branch runs hard-seed the child with the parent winner (`prior_winner_theme` + "Improve or extend it", runner.py:1663-1670); (b) `must_avoid.titles` blocks only exact normalized title matches (generator.py:1074, 1146-1151) — near-duplicate rephrasings pass; (c) when the LLM path fails, "exploration" degenerates to 10 canned plays (generator.py:123-248); (d) `best_so_far` is just `current_branch` (runner.py:692-695), so "learning on the winningest ideas" == hill-climbing one champion — there is no top-K portfolio to exploit.

**C10 (diagnose — is it actually the Karpathy method?). Code-level verdict: the ledger semantics are genuinely implemented; the substrate is not.** Real: an append-only results log fed back each round (runner.py:677-691), a current branch that advances only on strict improvement (runner.py:555), keep/discard/crash outcomes with crash preserving the prior keep (runner.py:504-580), and the model told to treat it "like Karpathy's results.tsv" (runner.py:705-711; generator.py:573-579). Not Karpathy: (a) there is no artifact to branch/revert — the "branch" is a dict snapshot in prompt context (runner.py:438-470), and an "experiment" is one persona-panel scoring of a text idea, not an executed change; (b) the researcher is not autonomous — a deterministic policy (`build_search_directive`) constrains `allowed_research_moves` in 3 of 5 phases (search_policy.py:81-101) and directive misses are policed with retries and a deterministic fallback (generator.py:870-901); (c) it is a single greedy chain — no parallel candidates, no tree. Accurate label: a Karpathy-style ledger + greedy champion hill-climb, with the "research judgment" split between the LLM and a hand-written policy.

**C11 (design — loop decides its own ceiling, capped).** Partially present: hard caps exist (max_candidates 50, persona_call_budget 80, models.py:488-489) and two self-stops exist — confirmed target (runner.py:1272-1283) and `search_exhausted` after 3 consecutive directive-exhausted rounds (runner.py:1302-1303). Missing: a convergence/plateau stop. `stall_count` is computed every round (runner.py:670-675) but is only used to force an escape move (search_policy.py:85-99), never to stop; a run that keeps producing valid-but-losing ideas grinds to cap/budget. New-repo decision: add a stall-based stop (e.g., N consecutive discards after a keep) beside runner.py:1302 — the counter already exists.

**C12 (design — more data through n8n).** This unit's n8n use is one flag-gated webhook for org context (runner.py:791-925), fetching one org per run (runner.py:918-925, deliberate — the docstring at runner.py:896-903 accepts 5 webhook calls for a 5-org batch). The adapter seam (`build_n8n_fetch_rows(url, token, timeout)`, runner.py:877-880) is data-shape-agnostic: more data means widening the n8n workflow/view, not this code.

**C13 (design — Snowflake pulls adjustable).** In this unit, what gets pulled is NOT in code — it's whatever the n8n workflow's Snowflake view returns; runner.py only validates/misroutes config (runner.py:844-882). The inflexible part is downstream shaping: the segment fact pack's fields are fixed in code (generator.py:335-371). Supports the concern's direction: query changes are n8n-side (good), pack-shape changes are code-side (needs a rebuild decision).

**C14 (design — env var sprawl, long names).** FOR: this unit alone reads ~14 env vars, most with the 34-char `PATHFINDER_ACTION_CONSOLE_` prefix: CONTROLLER_BRANCH + 4 CI fallbacks (runner.py:128-134), ORG_CONTEXT_N8N_URL/TOKEN (runner.py:791-792), LLM_TIMEOUT_S / LLM_MODEL (generator.py:646, 649), ANTHROPIC_API_KEY (generator.py:638), plus the config's GOAL_PP / MAX_CANDIDATES / PERSONA_CALL_BUDGET / PERSONA_CONCURRENCY / SEARCH_PANEL_SIZE / FINAL_PANEL_SIZE / CONFIRM_PANEL_SIZE / AUDIENCE_MODE / ORG_CONTEXT_* (models.py:509-572). Two knobs even have dual alias names (SEARCH_PANEL_SIZE vs PERSONA_PANEL_SIZE; FINAL_PANEL_SIZE vs FINAL_CHOICE_PANEL_SIZE, models.py:511-522). New-repo decision: one config file/object, short names, no aliases.

**C15 (diagnose — still on Supabase).** CONFIRMED for this unit: every run and idea is upserted to Supabase throughout the loop (runner.py:1087, 1229-1231, 1245-1248, 1433) and the live entry points resolve audiences with `source="supabase"` (app.py:429-434, 1003-1007). n8n covers only the flag-gated org-context fetch (off by default: `org_context_source: str = "none"`, models.py:499). Reads for branching use the LOCAL store, not Supabase (runner.py:1622). Root cause: Supabase is the run-history plane and the audience-resolution plane; n8n/Snowflake only ever became the org-context plane.

**C16 (diagnose — output long/unstructured).** Root cause is manufacturing, not just rendering: the generator concatenates three concerns into one prose blob per idea — `prose_action` = pro_facing + " Manager rationale: " + rationale + " Recommended channel/timing: " (generator.py:936-940 LLM path; 1219-1221 fallback), `rationale` = two more concatenated sentences (generator.py:961-964), `manager_rationale` fallback concatenates three fields (generator.py:763-767). The runner clips text for prompts (280/180-char clips, runner.py:431-435) but persists the full blobs. Fix in rebuild: keep fields separate; compose in the view layer.

**C17 (design — actual message copy).** BLOCKED BY DESIGN in this unit: the prompt orders "Do not write final email copy, SMS copy, or campaign copy unless the action type truly calls for copy" (generator.py:545) and frames ideas as "SEEDS, not final copy" with personalization explicitly deferred to marketing (generator.py:551-568). Nothing structural prevents copy — delivering C17 is a prompt+schema change (add a copy field to the tool schema at generator.py:374-479), not an architecture change.

**C18 (diagnose — is churn-risk testing in the output?).** YES: every persisted idea row carries `persona_reduction_pp`, `persona_ci_lower/upper`, `in_calibrated_range`, `persona_status` as top-level columns (runner.py:259-273) plus screening fields and `confirmation_status` inside `payload` (runner.py:249-258 explains why payload-only); the run row carries goal/stop_reason (runner.py:227-241). The goal test is CI-lower-bound based (persona_response.py:136-171), and confirmed clears are labeled `confirmation_status="confirmed"` (runner.py:1274).

**C19 (design — past runs feed future decisions).** Current support is exactly one hop and manual: `branch_action_console_run` seeds a child from a chosen parent idea + signal (runner.py:1663-1670). Fresh `start` runs have zero cross-run memory — `_idea_generation_memory` spans only the current run's ideas (runner.py:583-714). The seam for wargaming already exists: `parent_context` is an arbitrary dict merged into every generation prompt (generator.py:616); feeding prior winners for the same org/segment into it requires only a read-and-inject step at the entry point.

**C20 (design — real file structure).** These two files are 3,379 lines for one loop. runner.py mixes orchestration, Supabase write accounting, org-context config plumbing, and branch/validate entry points; generator.py mixes an LLM client, a prompt library, a validation layer, and a 450-line template engine. The action_console package split itself (models/scoring/policy/critics) is sound — the rebuild cut is within these two files (see §3), not the package layout.

**C21 (analytics page).** Not this unit's surface, but the runner persists what an analytics page needs: per-run token/cost totals (runner.py:1402-1406), stop reasons, per-idea scores/statuses, heartbeats (runner.py:360).

**NOVEL findings:** see N1–N8 below (§7 lists the gap-flavored ones).

- **N1 (novel, data-loss risk):** deterministic run_id clobber. POST /runs without a client `run_id` (app.py:994 `payload.get("run_id") or None`) derives `ac_<sha256(filters)[:12]>` (runner.py:203-211, 1032) — re-running the same audience silently overwrites the prior run's local dir and Supabase rows (all writes are upserts, runner.py:1087). Batch runs are safe only because batch_queue mints `{batch_id}-{i:02d}` ids (batch_queue.py:119).
- **N2 (novel, dead code):** `stop_reason == "persona_rate_limited"` is checked (runner.py:1322) but never assigned anywhere in the repo (grep: single occurrence).
- **N3 (novel, dead-on-live-path):** the winner channel run-off + in-loop final panel (runner.py:1311-1375) never executes on either live entry point — both pass `run_final_panel=False` (app.py:446, 1016); the out-of-band winner path (`validate_action_console_winner`) does NOT do the channel run-off (runner.py:1450-1577). With `PROPOSAL_CHANNELS = ("sms",)` (models.py:93) the run-off has no alternates anyway (persona_response.py:218). Net: "personas settle the channel" currently happens only on branch runs, and even there is a no-op for sms ideas.
- **N4 (novel, inconsistency):** the deterministic fallback emits `recommended_channel="email"` on most paths (generator.py:1405, 1407, 1411) without passing through `normalize_recommended_channel`, while the LLM path clamps everything to the sms-only proposal set (generator.py:768-770; models.py:158-168). Fallback ideas therefore carry a channel the system has declared out of scope.
- **N5 (novel, dead code):** `generate_action_ideas` + `ActionIdeaGenerator` have no non-test callers (grep: only generator.py and tests/test_action_console_generator.py).
- **N6 (novel, smell):** caller-supplied `run_metadata` is dict-spread AFTER the supabase counters/labels (runner.py:766-773), so a caller could clobber `writes_succeeded`/`origin`/`enabled`; current live callers only pass `run_label`/`batch_id`/`batch_size` (app.py:436-440, 1018-1019), so exposure is latent, not live.
- **N7 (novel, cost/latency creep):** prompt context grows monotonically per round — `results_log` carries every evaluated idea (runner.py:592) while `previous_ideas` is capped at 12 (runner.py:601); at cap that's ~50 rows × ~15 fields re-serialized into every generation request (generator.py:616). Feeds C6/C7.
- **N8 (novel, coupling):** breadth_critic imports generator privates `_audience_fact_pack`, `_subsegment_summary` (breadth_critic.py:27) — the fact pack is a shared contract pretending to be a private helper.

---

## 5. Entry points and callers

- `ActionConsoleRunner` — imported and constructed by the viewer API: app.py:72 (also imports `_run_origin`, a cross-module private use), app.py:431 (`_batch_run_org` → `start_run` with `audience_mode="org"`, app.py:440-447), app.py:1008-1016 (POST `/api/action-console/runs` → `start_run`), app.py:1049-1051 (POST `.../winner-validation` → `validate_winner`), app.py:1094-1096 (branch endpoint → `branch_run`).
- `generate_round` — called by the runner (runner.py:1150) and by `sequence_runner.py` (sequence_runner.py:8, 107, 156), so it has two production consumers.
- `generate_action_ideas` / `ActionIdeaGenerator` — callers exist ONLY in `tests/test_action_console_generator.py` (grep across src/ and scripts/ finds none). Dead in production.
- `_audience_fact_pack` / `_subsegment_summary` — additionally consumed by `breadth_critic.py:27`.
- Tests exercising the unit: `tests/test_action_console_runner.py` (2,961 lines) and `tests/test_action_console_generator.py` (2,009 lines) per the manifest; mapping quality is U13/U14's scope.

---

## 6. Integrations touched

- **Supabase** — write-only from this unit: `upsert_action_run`, `upsert_action_generated_idea`, `upsert_action_org_uuid_evidence[_many]`, `upsert_action_branch_signal` (runner.py:1087, 1101-1109, 1229-1231, 1433, 1653-1659), all best-effort via `SupabaseSink` (runner.py:28, 90).
- **Anthropic** — direct SDK call in the generator (generator.py:642-665): forced tool call, model/timeout env-swappable, `max_retries=0`, temperature 0.8; usage recorded to the per-run ledger (generator.py:671).
- **Railway persona-cards / persona service** — indirect via the `PersonaResponseScorer` seam (runner.py:994-1002) and `PersonaServiceError` handling around the final panel (runner.py:89, 1362-1364, 1502); no direct HTTP here. Railway is also detected for run origin (runner.py:724) and controller branch (runner.py:130).
- **Snowflake (via n8n)** — flag-gated org-context source: config validation, error re-labeling and secret scrubbing (runner.py:791-925); off by default (models.py:499).
- **n8n** — the webhook transport for the above (`build_n8n_fetch_rows`, runner.py:75, 877-880).
- **Sheets / Iterable / LCM** — not touched by this unit (export pipeline is U10x).
- **git/CI env** — `_controller_branch` shells out to `git branch --show-current` with a 2s timeout and reads 4 CI env fallbacks (runner.py:128-165).

---

## 7. Gaps

- **Mid-loop local-store failure is fatal and unhandled:** `store.save_run` is called per idea (runner.py:1236, via `_save_running_progress` runner.py:366) with no try — a disk error kills the run and leaves local+Supabase status "running" forever; recovery depends entirely on heartbeat-staleness logic elsewhere (viewer, out of unit).
- **The generator's fatal path is only accidentally unreachable:** `_generate_llm_round` raises a fatal `IdeaResearchDecisionError` when no `search_directive` is present (generator.py:897-901); the runner always sets one (runner.py:1144) — remove that line and mid-run crashes return. The invariant is implicit, unasserted.
- **`sink_writes.drain()` blocks indefinitely:** run completion waits on all queued futures with no timeout (runner.py:307-321, 1379); a hung PostgREST write hangs the run at 100%. Whether the sink's HTTP client enforces timeouts is out of unit (unverified, §8).
- **Trust-boundary handling that is present and good:** LLM applicability conditions validated against the real factor library, junk silently dropped (generator.py:47-60); org-context misconfig messages scrubbed with explicit secrets AND env sweep (runner.py:834-838); NaN/inf LLM estimates coerced (generator.py:710-717); non-dict transport payloads rejected (generator.py:634-636).
- **Untested-looking branches (from code reading; mapping is U13/U14's job):** the `bulk_evidence is None` per-row fallback (runner.py:1106-1109), the `_run_label` no-parts fallback (runner.py:197-198), and the run-off `channel_before != "none"` accounting (runner.py:1340-1343) which is arithmetic-dead with a 1-channel set.
- **Security smells:** none serious in-unit. `subprocess.run(["git", ...])` is list-form with timeout (runner.py:155-162). `run_metadata` clobber (N6) is the closest thing to an integrity hole.

---

## 8. Unverified

Stated as unverified; nothing below is asserted elsewhere in this report.

- Whether remote persona-cards service calls truly dominate live runs (and thus how much `llm_cost_usd` under-reports). The recorded call sites are local-LLM ones; which reaction provider is active at runtime is configuration I did not trace.
- Whether `SupabaseSink`'s HTTP layer enforces request timeouts (bears on the `drain()` hang risk). Sink internals are U10x.
- Whether batch_queue catches exceptions from `_batch_run_org` and marks items failed (bears on how orphaned "running" runs surface). batch_queue is U10a.
- Actual live wall-clock times and dollar costs per run — no run artifacts or logs were examined for this unit.
- Whether the two giant test files meaningfully cover the branches listed in §7 — test-to-source mapping is U13/U14's mandate, not mine.
- Whether `sequence_runner.py`'s use of `generate_round` is itself reachable from any live surface (its callers were not traced; it is outside this unit).

---

## 9. Files covered

Unit files read in full (verdicts issued):
- `src/pathfinder/action_console/runner.py`
- `src/pathfinder/action_console/generator.py`

(Context-only reads, no verdicts, listed in the header of this report.)
