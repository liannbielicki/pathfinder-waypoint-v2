# Loop Reliability and Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the evolve loop actually refine, stop resolving persona noise as signal, restore per-Pro churn baselines, and stop discarding a Pro's finished work when one generation call returns bad JSON.

**Architecture:** The loop already decides its own intent (`next_mode` → `refine` / `shift` / `cold`) and tells the model that intent in the prompt. Today it then throws the intent away and tries to re-derive it by string-comparing the model's free-text `mechanism` label. That comparison is the root cause of issues 1 and its knock-on costs. The fix is to carry the intent forward as data. No new vocabulary, no classifier call, no similarity threshold — and idea generation stays unbounded. The remaining three fixes are small and independent: one constant, one method body, one `try` block.

**Tech Stack:** Python 3.14 / Pydantic / SQLAlchemy / pytest (`asyncio_mode = "auto"`), React / TypeScript / Vitest.

**Spec:** This document. The diagnosis it argues from is reproduced under "Evidence" below — every claim there was confirmed by execution against this branch, not by reading.

## Global Constraints

- Work in the worktree `/Users/jakefassora/projects/pathfinder-waypoint-v2/.claude/worktrees/pathfinder-waypoint-v4`, branch `V4-Improvements`. Never `cd` to the main checkout — another session owns `v5`.
- Backend tests: `cd services/api && .venv/bin/python -m pytest <path> -v`. Frontend: `cd apps/web && pnpm vitest run <path>`.
- `ruff` line-length is 100. `mypy` is strict.
- Never use bare `git stash` / `git stash pop` — the stash stack is shared with other sessions.
- Do not push. The user reviews and approves before anything leaves the worktree.
- No schema migration is required by this plan. If a task seems to need one, stop and ask.
- Preserve existing comment density and the `ponytail:` convention for deliberate simplifications with a named ceiling.

## Evidence

Confirmed by execution on this branch. Retain for reviewers.

1. **Refine rounds discard their whole batch.** `_dedupe_ideas` drops every idea whose mechanism key differs from the champion's ([pipeline.py:638](../../../services/api/src/waypoint/pipeline.py#L638)). Fed a realistic refine batch where the model reworded its own label, it keeps **0 of 3**. The batch is paid for and thrown away; up to two refills then fire, each hardcoded to `build_prompt("shift", ...)`. Refinement never happens.
2. **Patience never advances.** Because the surviving challenger's mechanism differs from the champion's, `same` is false, `tries_on_current` pins at 1, `dry_mechanisms` stays 0, and `MAX_NO_IMPROVE` can never fire. Replaying 842846's real score sequence through this branch's `loop.py` ends `dry=0 stop=round_cap` with rounds 6–15 all nominally `refine`.
3. **Fuzzy matching cannot fix this.** Over 9 real mechanism pairs, same-lever pairs score 0.07–0.33 and different-lever pairs 0.03–0.15 (Jaccard on tokens); difflib overlaps too. The classes invert — worst same-lever (0.07) scores below worst false-collapse (0.15). Terse `ENUM_CASE` and long prose describe the same lever with no shared tokens, while unrelated levers share finance vocabulary (`cash`, `flow`, `offer`). No threshold exists.
4. **The win bar sits below the measurement's step size.** The score is a pure function of the 5-persona mean reaction. Five integers on a 3–7 scale move the mean in steps of 0.2, worth ~0.53 pp near the operating point. `KEEP_DELTA_PP` defaults to 0.5, so one persona moving one point crowns a champion.
5. **Every Pro scores on the global baseline.** `calibration_cell()` returns `None` unconditionally ([n8n.py:170](../../../services/api/src/waypoint/n8n.py#L170)), so all 166 per-cell baselines (0.040–0.362, a 9× spread) are unused and every Pro uses `global_baseline = 0.0869`. The stale comment claims v2 has no `segment`; it now does, and `segment` / `plan_tier` already use v1-compatible vocabulary (`"1A"`, `"basic"`).
6. **One bad batch kills a Pro.** The primary `generate(f"{key}:generate", ...)` at [pipeline.py:762](../../../services/api/src/waypoint/pipeline.py#L762) is outside any `try`; only refills are guarded. Pro `713346` had a 2.4 pp screen win at round 3 and was recorded as "failed — no result recorded" when round 4's generation returned bad JSON three times.

---

### Task 1: Round intent drives patience, not string comparison

The loop computes `mode = next_mode(lstate, config)` and tells the model. `apply_round` must be told the same thing instead of guessing it back from prose.

The slot the winner came from does not matter. A win resets `tries_on_current` to 0 regardless; a loss in a refine round means we spent an attempt in this mechanism's neighborhood without improving, regardless of which candidate lost. So `mode` alone is sufficient.

`replay` needs no stored column and no migration: `mode` is a pure function of `(state, config)`, so replay recomputes it exactly as the live loop does.

**Files:**
- Modify: `services/api/src/waypoint/loop.py` (`apply_round`, `replay`)
- Modify: `services/api/src/waypoint/pipeline.py:1145` (the `apply_round` call site)
- Test: `services/api/tests/test_loop.py`

**Interfaces:**
- Produces: `apply_round(state, *, mechanism, candidate_id, score_pp, outcome, config, mode, also_tried=())` — `mode` is a new **required keyword-only** `str`, one of `"cold" | "refine" | "shift"`. The `mechanism` parameter stays (it still feeds `tried_mechanisms` and `current_mechanism`) but no longer drives patience.
- Produces: `replay(rounds, config)` unchanged in signature and return type.

- [ ] **Step 1: Write the failing tests**

Add to `services/api/tests/test_loop.py`:

```python
def test_refine_round_advances_patience_even_when_the_label_is_reworded() -> None:
    """Regression: the model rewords its own mechanism every round. Patience
    must track the loop's INTENT, not the model's prose."""
    config = cfg(patience=2, max_no_improve=5)
    state = LoopState(
        round=1,
        best_score=2.4,
        best_candidate_id="c1",
        current_mechanism="Simplify payment collection workflow",
        tries_on_current=0,
    )
    state = apply_round(
        state,
        mechanism="Streamline payment collection to reduce friction",
        candidate_id="c2",
        score_pp=1.4,
        outcome="lose",
        config=config,
        mode="refine",
    )
    assert state.tries_on_current == 1
    state = apply_round(
        state,
        mechanism="Make collecting payment effortless",
        candidate_id="c3",
        score_pp=1.4,
        outcome="lose",
        config=config,
        mode="refine",
    )
    assert state.tries_on_current == 2
    assert state.dry_mechanisms == 1
    assert next_mode(state, config) == "shift"


def test_shift_round_restarts_the_patience_count() -> None:
    config = cfg(patience=2)
    state = LoopState(round=3, current_mechanism="payment collection", tries_on_current=2)
    state = apply_round(
        state,
        mechanism="online booking adoption",
        candidate_id="c9",
        score_pp=0.3,
        outcome="lose",
        config=config,
        mode="shift",
    )
    assert state.tries_on_current == 1


def test_a_win_resets_patience_whatever_the_mode() -> None:
    config = cfg(patience=2)
    state = LoopState(round=2, best_score=1.4, current_mechanism="payments", tries_on_current=1)
    state = apply_round(
        state,
        mechanism="payments reworded",
        candidate_id="c4",
        score_pp=3.3,
        outcome="win",
        config=config,
        mode="refine",
    )
    assert state.tries_on_current == 0
    assert state.dry_mechanisms == 0
    assert state.current_mechanism == "payments reworded"


def test_dry_mechanisms_exhaust_and_stop_the_loop() -> None:
    """The end-to-end point of Task 1: a Pro that stops improving now STOPS."""
    config = cfg(max_rounds=15, max_no_improve=5, patience=2, keep_delta_pp=0.6)
    state = LoopState()
    rounds = 0
    for index in range(15):
        if stop_reason(state, config) is not None:
            break
        state = apply_round(
            state,
            mechanism=f"payment friction variant {index}",
            candidate_id=f"c{index}",
            score_pp=1.4,
            outcome="win" if state.best_score is None else "lose",
            config=config,
            mode=next_mode(state, config),
        )
        rounds += 1
    assert stop_reason(state, config) == "no_improve_exhausted"
    assert rounds < 15, "loop must stop before the round cap once refinement dries up"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd services/api && .venv/bin/python -m pytest tests/test_loop.py -v -k "reworded or restarts or whatever_the_mode or exhaust"
```

Expected: FAIL — `apply_round() got an unexpected keyword argument 'mode'`.

- [ ] **Step 3: Make `apply_round` take the intent**

In `services/api/src/waypoint/loop.py`, add `mode: str` to the keyword-only parameters of `apply_round`, and replace the string-comparison line. The current tail of the function reads:

```python
    same = mechanism_key(mechanism) == mechanism_key(state.current_mechanism or "")
    tries = state.tries_on_current + 1 if same else 1
    dry = state.dry_mechanisms + (1 if tries >= config.patience else 0)
```

Replace those three lines with:

```python
    # The loop DECIDED this round's intent and told the model (see next_mode /
    # evolve_prompt). Re-deriving it by string-comparing the model's free-text
    # mechanism label is recovering information we already had, through the one
    # channel the model does not hold stable: it rewords the label every round,
    # so `same` was always False, tries pinned at 1, and dry never advanced.
    # A win resets tries below regardless, and a losing refine round spent an
    # attempt on this mechanism's neighborhood whichever candidate lost — so
    # the mode alone is sufficient and the winner's slot is irrelevant.
    tries = state.tries_on_current + 1 if mode == "refine" else 1
    dry = state.dry_mechanisms + (1 if tries >= config.patience else 0)
```

Leave the `suppressed`/`unavailable` and `win` branches above exactly as they are — both already ignore the string comparison.

- [ ] **Step 4: Make `replay` recompute the intent**

In the same file, `replay` must pass the mode the live loop would have used at that point. Replace its loop body:

```python
def replay(rounds: Sequence[RoundLike], config: LoopConfig) -> LoopState:
    """Rebuild loop state from the durable ledger — the one recovery code path."""
    state = LoopState()
    for row in rounds:
        # `mode` is a pure function of (state, config), computed identically at
        # the top of the live round loop — so replay reproduces it exactly and
        # the ledger needs no `mode` column and no migration.
        state = apply_round(
            state,
            mechanism=row.mechanism,
            candidate_id=row.candidate_id or "",
            score_pp=row.score_pp,
            outcome=row.outcome,
            config=config,
            mode=next_mode(state, config),
            also_tried=_round_also_tried(getattr(row, "ranking", None) or {}),
        )
    return state
```

- [ ] **Step 5: Update the pipeline call site**

In `services/api/src/waypoint/pipeline.py`, the round loop already has `mode` in scope from `mode = next_mode(lstate, config)` near line 1077. Add it to the `apply_round(...)` call around line 1145:

```python
        lstate = apply_round(
            lstate,
            mechanism=mechanism,
            candidate_id=candidate_ids[challenger],
            score_pp=score_pp,
            outcome=outcome,
            config=config,
            mode=mode,
            # The batch's other mechanisms were generated, critiqued and ranked
            # this round; forbid them next round instead of re-buying them.
            also_tried=[i.mechanism for index, i in enumerate(ideas) if index != challenger],
        )
```

- [ ] **Step 6: Run the full loop and pipeline suites**

```bash
cd services/api && .venv/bin/python -m pytest tests/test_loop.py tests/test_pipeline.py tests/test_resume.py -v
```

Expected: PASS. `test_resume.py` covers the replay path — if it fails, replay and the live loop have diverged, which is the one thing this task must not do.

- [ ] **Step 7: Commit**

```bash
git add services/api/src/waypoint/loop.py services/api/src/waypoint/pipeline.py services/api/tests/test_loop.py
git commit -m "fix: drive loop patience from the round's intent, not the model's prose

The loop decided refine/shift and told the model, then tried to re-derive
that decision by string-comparing the model's free-text mechanism label.
The model rewords the label every round, so tries_on_current pinned at 1,
dry_mechanisms never advanced, and MAX_NO_IMPROVE could never fire — every
Pro burned to the round cap. Carry the intent forward instead.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Refine rounds stop discarding their own batch

With Task 1 done, the loop's bookkeeping is right but refinement still never happens: `_dedupe_ideas` deletes every idea in a refine batch whose label was reworded, and the refills that replace them are hardcoded to `shift`.

In refine mode we *instructed* the model to keep the mechanism. Its ideas are refinements by construction. Dedupe them on concept (which `_dedupe_ideas` already does in refine mode) and stop second-guessing the label.

**Files:**
- Modify: `services/api/src/waypoint/pipeline.py` (`_dedupe_ideas` ~line 623, `_generate_batch` refill ~line 788)
- Test: `services/api/tests/test_pipeline.py`

**Interfaces:**
- Consumes: `apply_round(..., mode=...)` from Task 1.
- Produces: `_dedupe_ideas` keeps its signature. `current_mechanism` stays a parameter (still used by callers) but no longer filters in refine mode.

- [ ] **Step 1: Write the failing tests**

Add to `services/api/tests/test_pipeline.py` (import `_dedupe_ideas` from `waypoint.pipeline` and `Recommendation` from `waypoint.models` if not already imported):

```python
def _idea(mechanism: str, concept: str = "") -> Recommendation:
    return Recommendation(
        title="t",
        mechanism=mechanism,
        actions=["a"],
        pro_facing_concept=concept or f"concept for {mechanism}",
        manager_rationale="r",
        channel="sms",
    )


def test_refine_keeps_ideas_whose_mechanism_label_was_reworded() -> None:
    """Regression: the model rewords its label, so an exact-match filter threw
    the entire paid batch away and fell through to shift-mode refills."""
    champion = "Simplify payment collection workflow to lower barriers to cash flow"
    batch = [
        _idea("Simplify payment collection workflow to lower barriers and billing confidence"),
        _idea("Streamline payment collection to reduce friction"),
        _idea("Make collecting payment effortless for this Pro"),
    ]
    kept = _dedupe_ideas(batch, 3, mode="refine", current_mechanism=champion)
    assert len(kept) == 3


def test_refine_still_deduplicates_identical_concepts() -> None:
    champion = "payment collection"
    batch = [
        _idea("payment collection", concept="Store the card once, charge every time"),
        _idea("payment collection reworded", concept="store the card once, charge every time"),
        _idea("payment collection again", concept="Send a reminder the day after the job"),
    ]
    kept = _dedupe_ideas(batch, 3, mode="refine", current_mechanism=champion)
    assert len(kept) == 2, "same concept in different words is one candidate, not two"


def test_shift_still_forbids_already_tried_mechanisms() -> None:
    batch = [_idea("payment collection"), _idea("online booking adoption")]
    kept = _dedupe_ideas(
        batch, 3, mode="shift", forbidden_mechanisms=["Payment Collection"]
    )
    assert [i.mechanism for i in kept] == ["online booking adoption"]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd services/api && .venv/bin/python -m pytest tests/test_pipeline.py -v -k "reworded or identical_concepts or already_tried"
```

Expected: `test_refine_keeps_ideas_whose_mechanism_label_was_reworded` FAILS with `assert 0 == 3`. The other two should already pass — they are guardrails proving this task does not break dedupe or the shift path.

- [ ] **Step 3: Remove the refine-mode mechanism filter**

In `_dedupe_ideas`, delete these two lines:

```python
        if mode == "refine" and idea_key != current_key:
            continue
```

and replace the now-unused `current_key` assignment with a comment explaining why it is gone. The function head becomes:

```python
    held: dict[str, Recommendation] = {}
    # No refine-mode mechanism filter: in refine mode we INSTRUCTED the model to
    # keep the mechanism, so its ideas are refinements by construction. Matching
    # its free-text label against the champion's exact-dropped whole paid
    # batches (the model rewords the label every round) and fell through to
    # shift-mode refills, so refinement never actually ran. Concept-level dedupe
    # below is what keeps a refine batch honest.
    forbidden = {mechanism_key(item) for item in (forbidden_mechanisms or [])}
```

Keep `current_mechanism` in the signature — callers still pass it and removing it is a wider change than this task needs.

- [ ] **Step 4: Keep refills in the round's own mode**

In `_generate_batch`, the refill currently forces `"shift"`. Change the `build_prompt` call so a refine round refills with refine:

```python
            more = await generate(
                f"{key}:refill{refill}",
                missing,
                build_prompt(mode, missing, forbidden, warm if warm_missing else None),
            )
```

- [ ] **Step 5: Verify `mode` is in scope and typed**

`_generate_batch` already takes `mode: str` (it passes it to `_dedupe_ideas`). Confirm with:

```bash
cd services/api && grep -n "async def _generate_batch" -A 14 src/waypoint/pipeline.py
```

Expected: `mode: str,` appears in the parameter list. If it does not, add it and thread it from the round loop, which already has `mode` in scope.

- [ ] **Step 6: Run the suites**

```bash
cd services/api && .venv/bin/python -m pytest tests/test_pipeline.py tests/test_loop.py tests/test_warmstart.py -v
```

Expected: PASS. `test_warmstart.py` covers the `keep=` path through `_dedupe_ideas`.

- [ ] **Step 7: Commit**

```bash
git add services/api/src/waypoint/pipeline.py services/api/tests/test_pipeline.py
git commit -m "fix: stop discarding refine batches over a reworded mechanism label

_dedupe_ideas dropped every refine-mode idea whose mechanism string did not
exactly match the champion's. The model rewords that label constantly, so a
refine round kept 0 of 3 ideas, paid for them anyway, and fell through to
refills hardcoded to shift mode — refinement never ran. Refills now inherit
the round's own mode.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Tell the model how many refinement attempts remain

With Tasks 1–2 the loop refines and shifts correctly. This makes the refinement itself better: a model told "this is your last attempt before we abandon this mechanism" can go bolder than one told nothing. Raising `PATIENCE` to 3 then means the prompt says "two more attempts", automatically.

**Files:**
- Modify: `services/api/src/waypoint/prompts.py` (`evolve_prompt`)
- Modify: `services/api/src/waypoint/pipeline.py` (`_prompt_builder` / the `build_prompt` call, to pass the remaining count)
- Test: `services/api/tests/test_prompts.py`

**Interfaces:**
- Produces: `evolve_prompt(..., attempts_left: int | None = None)` — keyword-only, defaults to `None` so every existing caller and test keeps working. When `mode == "stay"`/`"refine"` and `attempts_left` is not `None`, the REFINE directive states the budget.

- [ ] **Step 1: Write the failing test**

Add to `services/api/tests/test_prompts.py`:

```python
def test_refine_prompt_states_the_remaining_attempt_budget() -> None:
    prompt = evolve_prompt(
        "org context",
        mode="refine",
        best_json='{"mechanism": "payment collection"}',
        history_json="[]",
        tried_mechanisms=[],
        channels=["sms"],
        journey_window="churn_risk_open",
        evidence="none",
        count=3,
        attempts_left=1,
    )
    assert "last attempt" in prompt.lower()


def test_refine_prompt_states_a_multi_attempt_budget() -> None:
    prompt = evolve_prompt(
        "org context",
        mode="refine",
        best_json='{"mechanism": "payment collection"}',
        history_json="[]",
        tried_mechanisms=[],
        channels=["sms"],
        journey_window="churn_risk_open",
        evidence="none",
        count=3,
        attempts_left=2,
    )
    assert "2 more attempts" in prompt


def test_refine_prompt_omits_the_budget_when_unknown() -> None:
    prompt = evolve_prompt(
        "org context",
        mode="refine",
        best_json="{}",
        history_json="[]",
        tried_mechanisms=[],
        channels=["sms"],
        journey_window="churn_risk_open",
        evidence="none",
        count=3,
    )
    assert "attempt" not in prompt.lower().split("mode: refine")[1][:400]
```

- [ ] **Step 2: Run to verify failure**

```bash
cd services/api && .venv/bin/python -m pytest tests/test_prompts.py -v -k "attempt"
```

Expected: FAIL — `evolve_prompt() got an unexpected keyword argument 'attempts_left'`.

- [ ] **Step 3: Add the budget line to the REFINE directive**

In `evolve_prompt`, add `attempts_left: int | None = None` to the keyword-only parameters. The REFINE branch is `if mode == "refine":` at `prompts.py:227`. Replace that branch's directive with the version below — note it ALSO softens "Keep the mechanism label exactly" to "Keep the same mechanism", because after Task 2 nothing depends on the literal string any more and demanding an exact label the model reliably ignores is a stale instruction:

```python
        budget = ""
        if attempts_left is not None:
            budget = (
                "\nThis is your LAST attempt on this mechanism — the next round "
                "abandons it for an untried one. Make this refinement count: change "
                "something substantive, not the wording.\n"
                if attempts_left <= 1
                else f"\nYou have {attempts_left} more attempts on this mechanism "
                "before it is abandoned for an untried one.\n"
            )
        directive = f"""Mode: REFINE. EVERY idea must be a distinct execution variant of the same
current mechanism. Keep the same mechanism, but vary the concept, timing,
framing, or specificity based on the round history. Do not introduce a
different mechanism in this batch.

Current selected idea (refine this mechanism):
{best_json}
{budget}"""
```

Leave the `cold` and `shift` branches and the `batch_rule` above untouched — `test_refine_prompt_omits_the_budget_when_unknown` guards against making the budget unconditional.

- [ ] **Step 4: Pass the budget from the round loop**

In `pipeline.py`, `_prompt_builder` wraps `evolve_prompt`. Thread `attempts_left` through it, computed from loop state at the call site:

```python
        attempts_left = (
            max(config.patience - lstate.tries_on_current, 1)
            if mode == "refine"
            else None
        )
        prompt = build_prompt(mode, count, tried, warm_mechanism, attempts_left=attempts_left)
```

`_prompt_builder`'s returned closure needs a matching `attempts_left: int | None = None` keyword-only parameter that it forwards to `evolve_prompt`. The refill call in `_generate_batch` does not pass it, so refills omit the budget — that is correct, a refill is topping up the batch, not spending a fresh attempt.

- [ ] **Step 5: Run the suites**

```bash
cd services/api && .venv/bin/python -m pytest tests/test_prompts.py tests/test_pipeline.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/api/src/waypoint/prompts.py services/api/src/waypoint/pipeline.py services/api/tests/test_prompts.py
git commit -m "feat: tell the generator how many refinement attempts remain

A model that knows this is its last try on a mechanism can change something
substantive instead of rewording. Raising PATIENCE now surfaces directly in
the prompt as a larger budget.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Raise the win bar above the measurement's step size

The score is a pure function of the 5-persona mean reaction. Five integers on a 3–7 scale move that mean in steps of 0.2, worth ~0.53 pp near the operating point. A `KEEP_DELTA_PP` of 0.5 therefore lets **one persona moving one point** crown a new champion. 0.6 requires two steps: two personas moving a point, or one moving two.

**Files:**
- Modify: `services/api/src/waypoint/loop.py` (`DEFAULT_LOOP_CONFIG`)
- Test: `services/api/tests/test_loop.py`, `services/api/tests/test_scoring.py`
- Modify: `apps/web/src/test/fixtures.ts`, `apps/web/src/components/RunStart.test.tsx`

**Interfaces:**
- Produces: `DEFAULT_LOOP_CONFIG.keep_delta_pp == 0.6`. No signature changes.

- [ ] **Step 1: Write the failing tests**

In `services/api/tests/test_loop.py`, update the `cfg()` helper's default `"keep_delta_pp"` from `0.5` to `0.6` (this keeps `test_defaults_match_the_spec` meaningful), and add:

```python
def test_a_single_persona_point_cannot_crown_a_champion() -> None:
    """One persona moving one point on the 3-7 scale is ~0.53pp. That is noise,
    not an improvement — the keep bar must sit above it."""
    config = cfg()
    assert config.keep_delta_pp > 0.53
    state = LoopState(round=1, best_score=2.4, current_mechanism="m", best_candidate_id="c1")
    assert is_win(state, 2.4 + 0.53, config, FLOOR) is False
    assert is_win(state, 2.4 + 1.06, config, FLOOR) is True
```

In `services/api/tests/test_scoring.py`, add a test pinning the arithmetic this bar depends on, so a future calibration swap cannot silently invalidate it:

```python
def test_one_persona_point_is_about_half_a_pp() -> None:
    """Documents the lattice the keep bar is calibrated against. CALIBRATION is
    the module-level constant already defined at the top of this file."""
    base = score_candidate([5.0] * 5, "missing-cell", CALIBRATION).reduction_pp
    nudged = score_candidate([5.0, 5.0, 5.0, 5.0, 6.0], "missing-cell", CALIBRATION).reduction_pp
    assert base is not None and nudged is not None
    assert 0.4 < nudged - base < 0.7
```

- [ ] **Step 2: Run to verify failure**

```bash
cd services/api && .venv/bin/python -m pytest tests/test_loop.py tests/test_scoring.py -v -k "single_persona or defaults_match or one_persona_point"
```

Expected: `test_defaults_match_the_spec` and `test_a_single_persona_point_cannot_crown_a_champion` FAIL (0.5 != 0.6). `test_one_persona_point_is_about_half_a_pp` should PASS immediately — it documents existing behavior. If it fails, stop and report the real step size before changing the bar.

- [ ] **Step 3: Raise the default**

In `services/api/src/waypoint/loop.py`:

```python
DEFAULT_LOOP_CONFIG = LoopConfig(
    max_rounds=10,
    max_no_improve=3,
    patience=1,
    # The score is a pure function of the 5-persona mean reaction. Five integers
    # on a 3-7 scale move that mean in steps of 0.2 — about 0.53pp near the
    # operating point — so a bar below 0.53 lets ONE persona moving ONE point
    # crown a champion. 0.6 requires two steps. See test_scoring for the lattice.
    keep_delta_pp=0.6,
    win_threshold_pp=15.0,
    candidate_count=3,
    tie_margin=0.05,
    warm_start_threshold=0.75,
)
```

Leave `win_threshold_pp` alone — the user is setting early-stop from the run form and explicitly took it out of scope.

- [ ] **Step 4: Update the frontend fixtures**

In `apps/web/src/test/fixtures.ts` and `apps/web/src/components/RunStart.test.tsx`, change `KEEP_DELTA_PP: 0.5` to `KEEP_DELTA_PP: 0.6`.

- [ ] **Step 5: Run both suites**

```bash
cd services/api && .venv/bin/python -m pytest tests/test_loop.py tests/test_scoring.py -v
cd apps/web && pnpm vitest run src/components/RunStart.test.tsx
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/api/src/waypoint/loop.py services/api/tests/test_loop.py services/api/tests/test_scoring.py apps/web/src/test/fixtures.ts apps/web/src/components/RunStart.test.tsx
git commit -m "fix: raise the keep bar above one persona's Likert step

The pp score is a pure function of the 5-persona mean reaction, which moves
in ~0.53pp steps. A 0.5pp keep bar let a single persona moving a single point
dethrone a champion, which is what made round-to-round results read as noise.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Reconnect the per-cell churn baselines

`calibration_cell()` returns `None` unconditionally, so all 166 per-cell baselines go unused and every Pro is scored against `global_baseline = 0.0869`. The cells are keyed `segment|plan|tenure`; v2's `segment` (`"1A"`) and `plan_tier` (`"basic"`) already use the calibration's vocabulary. `tenure_band` is the uncertain one, and real values cannot be verified from here.

`score_candidate` already falls back to the global baseline and `confidence="global"` when a cell key is absent (`calibration.baselines.get(cell)`), so an unmapped Pro degrades exactly as it does today. That makes this change safe by construction: it can only add signal, never fabricate it.

**Files:**
- Modify: `services/api/src/waypoint/n8n.py` (`calibration_cell`)
- Test: `services/api/tests/test_n8n.py`, `services/api/tests/test_scoring.py`

**Interfaces:**
- Produces: `OrgBrief.calibration_cell() -> str | None` returns `f"{segment}|{plan_tier}|{tenure_band}"` when all three are present and non-empty, else `None`.

- [ ] **Step 1: Write the failing tests**

Add to `services/api/tests/test_n8n.py`:

```python
def test_calibration_cell_composes_the_v1_key() -> None:
    brief = OrgBrief(
        org_uuid="pro_1", segment="1A", plan_tier="basic", tenure_band="0-3m"
    )
    assert brief.calibration_cell() == "1A|basic|0-3m"


def test_calibration_cell_is_none_when_any_component_is_missing() -> None:
    assert OrgBrief(org_uuid="p", plan_tier="basic", tenure_band="0-3m").calibration_cell() is None
    assert OrgBrief(org_uuid="p", segment="1A", tenure_band="0-3m").calibration_cell() is None
    assert OrgBrief(org_uuid="p", segment="1A", plan_tier="basic").calibration_cell() is None


def test_calibration_cell_normalizes_case_and_whitespace() -> None:
    brief = OrgBrief(
        org_uuid="p", segment=" 1A ", plan_tier="Basic", tenure_band="0-3M"
    )
    assert brief.calibration_cell() == "1A|basic|0-3m"


def test_composed_cell_hits_a_real_calibration_baseline() -> None:
    """The point of the whole task: the composed key must actually resolve."""
    import json
    from pathlib import Path

    cards = json.loads(
        (Path(__file__).parent.parent / "data" / "reaction_churn_calibration_cards.json").read_text()
    )
    brief = OrgBrief(org_uuid="p", segment="1A", plan_tier="basic", tenure_band="0-3m")
    cell = brief.calibration_cell()
    assert cell in cards["baselines"]
    assert cards["baselines"][cell]["baseline"] != cards["global_baseline"]
```

Add to `services/api/tests/test_scoring.py`:

```python
def test_an_unmapped_cell_still_falls_back_to_global() -> None:
    """Safety property: a tenure band we cannot map degrades exactly as today.
    CALIBRATION is the module-level constant at the top of this file."""
    score = score_candidate([5.0] * 5, "9Z|nonexistent|99m", CALIBRATION)
    assert score.baseline_confidence == "global"
    assert score.reduction_pp is not None
```

Note: `test_scoring.py` already defines `CELL = "1A|basic|0-3m"` at module level, which independently confirms the cell key format Task 5 composes.

- [ ] **Step 2: Run to verify failure**

```bash
cd services/api && .venv/bin/python -m pytest tests/test_n8n.py tests/test_scoring.py -v -k "calibration_cell or unmapped_cell or composed_cell"
```

Expected: the four `calibration_cell` / `composed_cell` tests FAIL (returns `None`). `test_an_unmapped_cell_still_falls_back_to_global` PASSES immediately — it pins the existing safety net this task relies on.

- [ ] **Step 3: Compose the cell key**

Replace the body of `calibration_cell` in `services/api/src/waypoint/n8n.py`:

```python
    def calibration_cell(self) -> str | None:
        """The v1 `segment|plan|tenure` key this Pro scores against.

        v2 emits `segment` ("1A") and `plan_tier` ("basic") in the calibration's
        own vocabulary. `tenure_band` is the one component whose live vocabulary
        is unverified against the cards' "0-3m" / "4-12m" / "13-36m" / "37m+".
        That is safe to compose anyway: scoring.score_candidate falls back to the
        global baseline with confidence="global" for any key the cards do not
        hold, so an unmappable Pro degrades to exactly today's behavior and a
        mappable one gains its real baseline.

        ponytail: no band translation table. Add one only if a run's cell-miss
        rate (logged below) shows live tenure values the cards do not carry.
        """
        parts = [
            (self.segment or "").strip().lower(),
            (self.plan_tier or "").strip().lower(),
            (self.tenure_band or "").strip().lower(),
        ]
        if not all(parts):
            return None
        # Segment is upper-case in the cards ("1A"), the other two lower-case.
        parts[0] = parts[0].upper()
        return "|".join(parts)
```

- [ ] **Step 4: Log cell coverage so the miss rate is observable**

Find where `calibration_cell()` is called in `pipeline.py`:

```bash
cd services/api && grep -n "calibration_cell" src/waypoint/pipeline.py
```

At that call site, add a debug log naming the resolved cell and whether it hit, using the module's existing `log` object and its established style:

```python
        cell = brief.calibration_cell()
        log.debug(
            "calibration cell %s for pro %s: %s",
            cell or "<unmappable>",
            state.pro_id,
            "hit" if cell in calibration.baselines else "miss -> global baseline",
        )
```

Match the surrounding code's variable names for `calibration` and `brief` — read the call site before editing.

- [ ] **Step 5: Run the suites**

```bash
cd services/api && .venv/bin/python -m pytest tests/test_n8n.py tests/test_scoring.py tests/test_pipeline.py tests/test_contract.py -v
```

Expected: PASS. If `test_pipeline.py` or `test_contract.py` assert `baseline_confidence == "global"` on a fixture that now maps to a real cell, update that assertion to the cell's true confidence — do not revert the change to make an old assertion pass.

- [ ] **Step 6: Commit**

```bash
git add services/api/src/waypoint/n8n.py services/api/tests/test_n8n.py services/api/tests/test_scoring.py
git commit -m "fix: score each Pro against its own churn baseline

calibration_cell() returned None unconditionally, so all 166 per-cell
baselines (0.040-0.362, a 9x spread) went unused and every Pro scored
against the 0.0869 global. v2 already emits segment and plan_tier in the
cards' vocabulary; compose the key and let the existing global fallback
handle any cell the cards do not carry.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: A failed generation ships the champion instead of losing the Pro

The primary generation call sits outside any `try`. When it exhausts its three re-asks the whole Pro job fails, discarding every round already won and paid for. Pro `713346` lost a 2.4 pp winner this way.

Per the user's decision: stop the loop, keep the champion, let the normal `final` → `score` stages run. The run is **not** marked degraded — but the stop reason is recorded so the failure stays visible.

**Files:**
- Modify: `services/api/src/waypoint/pipeline.py` (the round loop's `_generate_batch` call ~line 1130)
- Test: `services/api/tests/test_pipeline.py`

**Interfaces:**
- Consumes: `PipelineFailure` (already imported in `pipeline.py`).
- Produces: `_run_evolve` returns `{"rounds": ..., "stop": "generation_unavailable: <reason>", ...}` instead of propagating `PipelineFailure`.

- [ ] **Step 1: Write the failing test**

Add to `services/api/tests/test_pipeline.py`. Follow the module's existing harness for driving a pro job — read a neighbouring pipeline test first and mirror its fixtures rather than inventing new ones:

```python
async def test_generation_failure_mid_loop_keeps_the_existing_champion(...) -> None:
    """Regression: 713346 won 2.4pp at round 3, then round 4's generation
    returned bad JSON three times and the Pro was recorded as
    'failed - no result recorded', discarding the win."""
    # Arrange: a stub LLM that succeeds for round 1 (producing a champion) and
    # then returns unparseable text for every subsequent evolve call.
    # Assert, after running the evolve + score stages:
    #   - no PipelineFailure escaped
    #   - a WinnerRow exists for the pro with kind == "winner"
    #   - the recorded stop reason starts with "generation_unavailable"


async def test_generation_failure_with_no_champion_records_no_action(...) -> None:
    """Nothing was ever won: the Pro still must not vanish — _stage_score
    records no_action rather than the job failing."""
    # Assert: WinnerRow.kind == "no_action", no PipelineFailure escaped.
```

Fill these in against the module's real fixtures. The two assertions that matter are: **no exception escapes**, and **a `WinnerRow` exists either way**.

- [ ] **Step 2: Run to verify failure**

```bash
cd services/api && .venv/bin/python -m pytest tests/test_pipeline.py -v -k "generation_failure"
```

Expected: FAIL — `PipelineFailure: evolve_invalid_output after 3 attempts` propagates out.

- [ ] **Step 3: Catch the failure and stop the loop**

Wrap the round loop's `_generate_batch` call:

```python
        try:
            ideas = await _generate_batch(
                state,
                deps,
                key=key,
                count=batch_count,
                prompt=prompt,
                build_prompt=build_prompt,
                tried=tried,
                mode=mode,
                current_mechanism=lstate.current_mechanism,
                warm=warm_mechanism,
            )
        except PipelineFailure as error:
            # Generation is unavailable this round. Rounds already won are
            # durable and paid for — ending the loop here ships the champion
            # through the normal final/score stages, where a Pro with no
            # champion still records an honest no_action. Failing the whole job
            # instead discarded finished work (pro 713346 lost a 2.4pp winner).
            # BudgetExhausted and LeaseLost are NOT PipelineFailure and still
            # propagate to their own handlers.
            reason = f"generation_unavailable: {error.reason}"
            break
```

The `while (reason := stop_reason(lstate, config)) is None:` walrus means `reason` is already the loop's stop variable, so assigning it before `break` flows into the existing `return {"rounds": lstate.round, "stop": reason, ...}`. Verify that by reading the loop's return statement before relying on it.

- [ ] **Step 4: Run to verify the tests pass**

```bash
cd services/api && .venv/bin/python -m pytest tests/test_pipeline.py -v -k "generation_failure"
```

Expected: PASS.

- [ ] **Step 5: Run the whole backend suite**

```bash
cd services/api && .venv/bin/python -m pytest -v
```

Expected: PASS. This is the last backend task — everything must be green before review.

- [ ] **Step 6: Lint and type-check**

```bash
cd services/api && .venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && .venv/bin/mypy src
```

Expected: clean. Fix anything reported.

- [ ] **Step 7: Commit**

```bash
git add services/api/src/waypoint/pipeline.py services/api/tests/test_pipeline.py
git commit -m "fix: a failed generation ends the loop instead of losing the Pro

Only refill generation was guarded; the primary call was not, so one batch of
bad JSON failed the whole Pro job and discarded every round already won and
paid for. End the loop instead and let final/score ship the champion, or
record an honest no_action when there is none.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Verify against a real run

Tests prove the units. This proves the loop behaves differently end to end, which is the actual goal.

**Files:** none — this is an observation task.

- [ ] **Step 1: Run the 4-Pro audience that produced the original evidence**

Use the same query lineage as run `4e7cb9fdec764d45bfa76588b395bdef` (`workbench:promotion-d1a0d4e84f97318b`), with `MAX_ROUNDS=15`, `MAX_NO_IMPROVE=5`, `PATIENCE=3`, `KEEP_DELTA_PP=0.6`.

- [ ] **Step 2: Record these five numbers against the baseline run**

| Metric | Baseline (run `4e7c…`) | After |
|---|---|---|
| Pros that failed with no result | 1 of 4 | expect 0 |
| Rounds used by the winning Pros | 12, 14 of 15 | expect materially fewer |
| Stop reasons | all `round_cap` | expect some `no_improve_exhausted` |
| Candidate lists within a round | 3 rewordings of one lever | expect distinct levers after a shift |
| `baseline confidence` on winners | `global` | expect `high` / `medium` where cells map |

- [ ] **Step 3: Report the cell-miss rate**

From the Task 5 debug logs, report how many Pros mapped to a real calibration cell. If most miss, the live `tenure_band` vocabulary differs from the cards' and a translation table is the follow-up — do not guess the mapping, report the observed values.

- [ ] **Step 4: Stop and hand back**

Do not push. Report the table above to the user for review.

---

## Phase 2 (deferred — do not start until Phase 1 is reviewed, approved and pushed)

Scoped here so the work is captured, **not** for implementation in this plan. Per the user: pick this up only after the Phase 1 code review completes and the changes are on the remote.

### 2A. Make RECO arrive at all

`suggested_outreach_channel()` reads `suggested_channel`, sourced from `production.reco.channel_recommendations.recommended_action` joined via the org's founding admin pro. The winner card currently prints `RECO unavailable`. Two candidate causes, unresolved:

1. **The main context flow never fetches it.** That SQL union exists only in the three `waypoint-variable-audit-context-*` flows; `waypoint-context-snowflake-v1.json` has zero references to `recommended_action` or `channel_recommendations`. RECO reaches a run only as a promoted workbench variable canonicalized to `suggested_channel`.
2. **The value is not a bare channel word.** The filter accepts only `{"sms", "email", "call"}` after lower/strip. The column is `recommended_action`, not `recommended_channel` — a value like `"SMS_OUTREACH"` or `"NO_ACTION"` silently becomes `None`.

**First step, before any code:**

```sql
select distinct recommended_action, count(*)
from production.reco.channel_recommendations
group by 1 order by 2 desc;
```

### 2B. Verify the consent vocabulary

`gate_pro` already beats RECO — an opted-out channel is stripped before generation, and the RECO rule is skipped when the suggested channel is not in the gated set. The gap is that `_consent_blocks` fails **open** against a hardcoded guess:

```python
NEGATIVE_CONSENT = frozenset({"opted_out", "opted-out", "unsubscribed",
                              "suppressed", "dnc", "blocked", "revoked", "no"})
```

The code comment already flags this as unverified. If Snowflake emits `"OPT_OUT"` or `"N"`, an opted-out Pro stays contactable at this layer. Resolve with the same kind of `select distinct` against the live consent columns. Note separately that `call` has no consent field at all and always fails open.

### 2C. Channel-first selection

Channel is currently chosen per-idea by the model inside the same call that invents the theme, with RECO advisory only (an override needs a `channel_override_reason` string, which the model can always write). Because `mechanism` identity ignores channel while the persona screen reframes substantially by channel, channel is a large uncontrolled lever swinging the score between rounds — it compounds the Task 4 noise problem.

Proposal: decide channel deterministically **before** the loop, from (1) the consent gate, (2) RECO, (3) observed per-channel returns, (4) engagement bands (`email_engagement_state`, `outreach_count_28d_band`); pin it per-Pro; allow one re-pick if the pinned channel exhausts without clearing the floor. Every input already exists.

### 2D. Fix the population/individual evidence misattribution

**This is a live correctness bug, not a design change.** `pattern_summaries(session, journey_window, channels)` takes no `pro_id` — it is population-wide, and `evidence_block` labels it correctly as *"Observed outcomes for similar pros."* Yet 842846's shipped manager rationale reads *"SMS showed strong returns **for this Pro** (82/121 at 1d, 116/121 at 7d)."* The model read population evidence and narrated it as this Pro's own history, and that sentence went into the handoff.

Two parts: tighten the prompt so population evidence cannot be restated as per-Pro fact, and add genuine per-`(pro, channel)` return evidence (`failed_mechanisms(session, pro_id)` shows the per-Pro query shape already exists).

---

## Self-Review

**Spec coverage.** Issue 1 → Tasks 1, 2, 3. Issue 2 → Task 4. Issue 4 → Task 5. Issue 5 → Task 6. End-to-end proof → Task 7. RECO / channel-fit → Phase 2, explicitly deferred. Issue 3 (early stop) is out of scope by the user's decision. Persona fit (`_fit` returning 1.00 for every member) was raised in diagnosis but never accepted into scope — it is **not** covered here and remains open.

**Placeholders.** Task 6 Step 1 gives test intent and assertions rather than literal code, because `test_pipeline.py`'s async harness and fixtures must be mirrored rather than invented; the required assertions are stated explicitly. Task 5 Step 4 requires reading the call site for local variable names. Both are deliberate and flagged inline.

**Type consistency.** `apply_round(..., mode: str)` is added in Task 1 and called with the pipeline's existing `mode` variable in the same task, so the tree never breaks between tasks. `replay` recomputes `mode` via `next_mode`, needing no ledger column and no migration. `_dedupe_ideas` keeps its signature in Task 2 (`current_mechanism` retained, unused in refine). `evolve_prompt` gains `attempts_left: int | None = None`, defaulted so every existing caller and test is unaffected. `calibration_cell` keeps `-> str | None`.
