# Compounding "evolve" loop — design spec

**Date:** 2026-08-07
**Branch:** `feature/compounding-evolve-loop` (off `fable/production-build`, squashed to **one commit** for clean merge back)
**Status:** design approved, pending spec review

## Goal

Replace v2's one-shot "generate 3 blind ideas + one retry" with a **compounding
loop** per Pro, faithful to Karpathy's `autoresearch`: read the full history of
what's been tried, propose ONE evolved idea, score it against a frozen metric,
keep it only if it beats the best-so-far, repeat. The loop gets smarter each
round because every proposal is conditioned on the running log of wins and losses.

The search rhythm is **win-stay / lose-shift** (see below) so the loop cannot
collapse into v1's failure mode.

Non-goals (explicitly deferred): cross-run memory (round 0 is cold each run);
local/zero-cost LLM hosting; agent-as-tool / Snowflake-MCP-in-session
architecture. The first implementation uses per-Pro durable jobs with a
fleet-wide `MAX_IN_FLIGHT_LLM_CALLS = 4` cap; this is a throughput guard, not a
cost-reduction claim.

## Why the worker, not agent-as-tool

Karpathy's script can't be used "as-is": its git/file/shell substrate assumes
the artifact is a *file*, the fitness is a *shell command*, keep/discard is *git
commit/revert*. Our artifact is an *idea* (text); our fitness is a *persona-panel
score*. Only the ~30-line loop skeleton transfers — any build re-implements it.

We build it in the existing durable worker because everything the product needs
— Postgres checkpoints, resume-without-re-paying, cost reservation, kill switch,
the ≥1pp floor gate, honest `no_action`, an auditable durable row per
keep/discard — is already enforced there for free. The loop core is identical to
the future local-LLM version; that day is an LLM-gateway backend swap.

## What killed v1, and how v2 prevents each failure

From the post-mortem ([liann-synthesis.md](../../knowledge/liann-synthesis.md), C8):

| v1 failure | v2 antidote |
|---|---|
| "Population-of-one greedy champion walk" — once a champion existed the search **collapsed onto it and never structurally left** | **Win-stay/lose-shift**: the first non-improving attempt *ejects* the loop from that mechanism. It cannot collapse to one idea. |
| Weak novelty (exact-title-only) let it re-tweak the same idea forever | On a "shift," the already-tried mechanisms are **forbidden** — forced genuine difference, exactly when refinement dries up |
| Noisy keep bar: a bare `>` between stochastic scores | **Temp-0 frozen metric** + `KEEP_DELTA_PP` margin (see Problem 1) |
| Dead convergence signal (`goal_reached` hardcoded False) | Mechanical stop: `MAX_NO_IMPROVE` dry mechanisms. No self-reported convergence to game. |

## The loop: win-stay / lose-shift

One reactive rule, one challenger per round:

> **Win (last idea improved the best-so-far) → stay:** refine the same mechanism.
> **Lose (it didn't) → shift:** propose an *untried* mechanism.
> Stop after `MAX_NO_IMPROVE` dry mechanisms, or the round cap, or a `WIN_THRESHOLD_PP`
> idea, or budget/kill.

One-sentence model: **"Climb the best idea you've got; the moment a line stops
improving, jump to a genuinely new mechanism; stop once several jumps in a row
beat nothing."**

`PATIENCE` sets how many refine attempts a mechanism gets before it counts as a
dry hole and the loop shifts — the explore/exploit dial. `PATIENCE = 1` is pure
win-stay/lose-shift (dig each hole once); higher digs deeper before moving on.

**Why this is the elegant cost balance:** compute flows to what's working. A rich
mechanism keeps earning refinement rounds while it improves; a dead line is
abandoned after `PATIENCE` misses. The explore/exploit balance *emerges from the
data* (are we still improving?) — no schedule, no phases, no patch.

## Autonomy model — the faithful Karpathy split

- **Keep/discard: mechanical.** The frozen metric decides by number comparison.
  No LLM judgment in the gate.
- **Intelligence: in the proposal.** The generator reads the full history + the
  best-so-far and, told the mode (stay=refine / shift=new mechanism), proposes
  the next idea. This is where compounding lives.
- **Stop: mechanical.** `MAX_NO_IMPROVE` dry mechanisms — not a self-reported
  "converged" flag (dropped: a lazy model games it; v1's dead `goal_reached`).

## Three correctness fixes (baked in)

**Problem 1 — the metric must actually be frozen.** Today reactions are scored at
temperature 1.0, so the *same idea scores differently each round* — a worse idea
can catch a lucky score and dethrone a good champion, and the loop goes backwards.
Fix: score **evaluation** calls (screen + final reactions) at **temperature 0**;
keep **generation** creative (default temp). Karpathy's split exactly: proposals
vary, the fitness measurement is fixed. `KEEP_DELTA_PP` then only has to cover real
idea-to-idea differences, not measurement noise. Requires a `temperature` kwarg on
`LLM.complete` (defaulted so existing callers are unchanged).

**Problem 2 — explore/exploit** — solved by the win-stay/lose-shift rhythm above.

**Problem 3 — the loop optimizes the cheap 3-panel; the 5-panel is the real
judge.** If they disagree, the loop can optimize a proxy that fails the final gate
(→ wasted run). Panels are deterministic and heavily overlapping, and the
held-out `final` catches the catastrophic case (→ `no_action`). For v1 this is an
**accepted ceiling**, marked with a `ponytail:` comment — not mid-loop
revalidation (cost + complexity we don't need yet).

## Stage changes

```
old:  context → generate → critics → screen → search → final → score → measure → ready
new:  context → evolve ───────────────────────────────→ final → score → measure → ready
```

`generate`, `critics`, `screen`, and the one-shot `search` retry collapse into a
single looped `evolve` stage. `final` (5-persona confirmation), `score`,
`measure`, `ready` unchanged. `search_directive_prompt` is deleted — a
history-informed proposal strictly subsumes it.

## The `evolve` loop, per round

State per Pro, reconstructable from durable rows (see Persistence): `best` (best
kept idea + its frozen score, or none), `current_mechanism`, `tries_on_current`,
`dry_mechanisms`, `history` (append-only).

1. **Reserve budget + heartbeat** — existing `_reserve` / `_heartbeat`.
2. **Pick mode** — no `best` yet, or `best` improved last round, or
   `tries_on_current < PATIENCE` → **stay** (refine `current_mechanism`).
   Otherwise → **shift** (new untried mechanism; reset `tries_on_current`).
3. **Propose ONE challenger** — new `evolve_prompt` gets brief + best + full
   history + mode + the list of tried mechanisms (forbidden on a shift).
4. **Critic grounding gate** — existing `_critic_pass`. Suppressed → a loss, no
   persona spend.
5. **Score at temp 0** — the **3-persona screen panel** (existing `_react` +
   `score_candidate`). Same deterministic panel every round → comparable scores.
6. **Win/lose (code):** `best` is none → win if `score ≥ MIN_REDUCTION_FLOOR_PP`;
   else win if `score ≥ best + KEEP_DELTA_PP`. Win → `best` = idea,
   `current_mechanism` = its mechanism, reset `tries_on_current` and
   `dry_mechanisms`. Lose → `tries_on_current += 1`; if the mechanism is now
   exhausted (`tries_on_current ≥ PATIENCE`) → `dry_mechanisms += 1`. Append to
   history either way.
7. **Stop when any of:** `best_score > WIN_THRESHOLD_PP` · `dry_mechanisms ≥
   MAX_NO_IMPROVE` · `round ≥ MAX_ROUNDS` · budget exhausted / fleet killed /
   lease lost (existing handling).

After the loop, `best` runs once through the existing **5-persona `final`** panel
(temp 0) — the held-out confirmation. Winner declared only if `final` clears the
floor (existing `_stage_score` / `select_winner`). No `best` ever cleared →
`no_action`.

Cost per round ≈ 3 LLM calls (generate + critic + screen), bounded by
`MAX_ROUNDS × pros`, metered by the existing reservation + kill switch.

Spend protection is separate from concurrency protection. Before each paid
call, the worker reserves the call's worst-case configured cost for the selected
model and output ceiling. After the provider response, persisted usage
reconciles the reservation to actual cost (including provider-reported usage
when available). A failed or abandoned call keeps its worst-case reservation
until reconciliation resolves it. The run and fleet admission checks happen
before every paid call, so a concurrency slot never bypasses budget limits.
The implementation must not rely on one flat estimate such as `$0.10` when
model pricing or token ceilings differ.

Per-Pro jobs may run concurrently, but every worker shares one fleet-wide
in-flight model-call limiter. The limiter applies to generation, critic, screen,
and final calls. A call waits for a slot before spending; the cap is visible in
the run-setup UI beside the loop constraints as **"Max simultaneous model calls
(fleet): 4"**. It is read-only for ordinary run creation and cannot be raised by
per-run loop overrides.

## The five tunable constraints

Surfaced in the run-setup UI, stored per run; persisted defaults live on the
`fleet_control` singleton. "Safe hard-coded": fixed defaults, editable, every edit
confirm-gated.

| Key | Default | Meaning |
|-----|---------|---------|
| `MAX_ROUNDS` | 10 | **max rounds per Pro** (hard cap) |
| `MAX_NO_IMPROVE` | 3 | dry mechanisms (no new best) before stopping |
| `PATIENCE` | 1 | refine attempts per mechanism before shifting (explore/exploit dial) |
| `KEEP_DELTA_PP` | 0.5 | min improvement (pp) to set a new best |
| `WIN_THRESHOLD_PP` | 15 | reduction over this → stop early (success cop-out) |

### UI behavior (run-setup panel, `apps/web`)

- Organize the panel into three visible groups, in this order:
  1. **Run inputs:** Pro IDs, audience query version, audience run timestamp,
     and channel. This answers what will run.
  2. **Loop behavior:** the five per-run constraints and their short helper
     text. This answers how the loop will search.
  3. **Fleet safety:** the read-only `MAX_IN_FLIGHT_LLM_CALLS` setting and its
     API-pressure explanation. This answers what the run cannot change.
- Keep the fleet-safety group visible in the initial form view. Do not place it
  behind an advanced-settings disclosure or style it like an editable run
  override.
- A "Loop constraints" panel in the new-run form shows all five, each labeled
  plainly (`MAX_ROUNDS` reads **"Max rounds per Pro"**).
- Each field is pre-filled with its current persisted default.
- Changing a value requires typing `confirm` in a per-field input before it is
  accepted.
- On confirm: (a) applies to this run and (b) is written back as the new persisted
  default (`fleet_control.loop_defaults`), pre-filling next time.
- Any direction allowed, within each field's own bound (`PATIENCE ≥ 1`; all ≥ 0;
  `KEEP_DELTA_PP ≤ WIN_THRESHOLD_PP`).
- Beside the five per-run loop controls, show the read-only fleet safety setting:
  `MAX_IN_FLIGHT_LLM_CALLS = 4` with the plain label **"Max simultaneous model
  calls (fleet)"** and helper text: **"Shared across all workers; limits API
  pressure, not total run cost."**

### UI interaction states

| Feature | Loading | Empty | Error | Success | Partial |
|---------|---------|-------|-------|---------|---------|
| Start-run form | Disable submit and show **"Starting run…"**; preserve entered values. | Keep the form usable; require Pro IDs, audience query version, audience run timestamp, and channel before enabling submit. | Show an inline alert with the server message; keep values and re-enable submit without silently retrying. | Navigate to run status and show **"Run queued"** with the run ID. | If the server accepts the run but some input rows are rejected, show the accepted/rejected counts and let the operator correct and start a new run. |
| Loop controls | Disable edits while the start request is in flight. | Show persisted defaults and one short explanation per field. | Identify the invalid field and bound; do not write defaults or create a run. | Show the effective snapshot that will be stored on the run. | If one field is not confirmed, keep that field unchanged and make the missing confirmation explicit; never apply a mixed, invisible configuration. |
| Run status | Show **"Queued"** or **"Working"**, current stage, and last update time; polling must not duplicate work. | For a newly created run, show the run ID and **"Waiting for a worker"**, not an empty-result message. | Show the named failure or stop reason, whether work may have been paid for, and the safe next action. | Show terminal result, champion/no-action/abstention outcome, spend, and handoff availability. | Show completed and failed Pro counts, the affected Pro IDs, spend reserved/spent, and whether the remaining work continues or is stopped. |

Status copy must distinguish `failed`, `stopped`, `degraded`, `abstained`, and
`no_action`; none may be rendered as a generic error. A terminal run cannot be
submitted again from the status view, and the kill action requires a clear
confirmation while the run is non-terminal.

### Operator journey

| Step | Operator does | Operator should feel | UI must support |
|------|---------------|----------------------|-----------------|
| 1. Configure | Enters the audience and reviews loop controls plus the fleet cap. | Informed before any paid work begins. | Group inputs by scope, show effective values, and explain that the fleet cap is shared and read-only. |
| 2. Start | Submits once and waits for acknowledgement. | Confident that one run, not duplicate work, was created. | Disable submit during the request, preserve values on error, and show the returned run ID. |
| 3. Monitor | Watches queued, running, waiting, and partial progress. | Calm rather than compelled to refresh or retry. | Show current stage, last update, completed/remaining Pro counts, and the next safe action. |
| 4. Recover | Encounters a failed Pro, unavailable evaluation, budget stop, lease recovery, or kill. | In control, with an honest explanation. | Name the reason, identify whether paid work occurred, show what continued, and distinguish retryable from terminal states. |
| 5. Review | Reads the champion, no-action, abstention, or degraded outcome. | Able to make a defensible decision. | Show evidence and spend together, identify affected Pros, and make handoff availability explicit. |

The five-second scan should answer **what run is this, what is it doing, and
what can I safely do next?** The five-minute review should answer **what was
decided, what evidence supports it, what did it cost, and what remains
uncertain?**

The status view also includes a compact read-only **Run settings** summary for
the immutable per-run snapshot: `MAX_ROUNDS`, `MAX_NO_IMPROVE`, `PATIENCE`,
`KEEP_DELTA_PP`, and `WIN_THRESHOLD_PP`. Show the plain-language label first
and the stored value second so the operator can audit a result without opening
the database or guessing which defaults were active.

### Feature-level visual direction

- Keep the existing calm operator surface: one primary column, native form
  controls, compact headings, and the current CSS variables for background,
  text, border, accent, error, and success colors.
- Use hierarchy instead of decoration: the run identity and current action come
  first, configuration groups second, and supporting evidence/cost third.
- Use semantic color only for state meaning (`success`, `error`, and warning or
  waiting states); never use color as the only state indicator.
- Treat the fleet cap as an informational safety row with a lock/read-only cue,
  not as a fifth editable loop input. Keep technical names in secondary text
  after plain-language labels.
- Do not add a dashboard card mosaic, decorative gradients, icon circles, or
  repeated status pills. A bordered group earns its space only when it contains
  a distinct operator decision or recovery action.

### What already exists

- `apps/web/src/components/RunStart.tsx`: the existing single-column run-start
  form, native labels/inputs, inline alert, and disabled-submit pattern.
- `apps/web/src/components/RunStatus.tsx`: the existing status panel, stage
  list, cost summary, terminal-state handling, and kill action.
- `apps/web/src/app/globals.css`: CSS variables for surface, text, border,
  accent, error, and success colors; 44px button targets; visible focus rings;
  and the existing panel/control vocabulary.
- No `DESIGN.md` exists. This feature must not create a competing design system;
  it should reuse these patterns and record only feature-specific rules here.

### Accessibility contract

- Preserve a logical keyboard order: run inputs, loop behavior, fleet safety,
  then submit and inline errors. Every visible control keeps a persistent label;
  placeholders are never the only labels.
- Keep the existing visible focus ring and 44px minimum button target. The
  fleet cap is exposed as read-only text with an explicit accessible label and
  is not included in the editable-field tab sequence.
- Announce start errors and terminal status changes through `role="alert"` or
  `aria-live="polite"` according to urgency. Do not rely on color, a checkmark,
  or a status badge alone to communicate state.
- Associate each field bound, confirmation requirement, and validation error
  with that field programmatically. On validation failure, move focus to the
  first invalid field without clearing valid entries.
- Keep the current single-column responsive flow for this release. Bespoke
  tablet/mobile layout changes are deferred until the real control density is
  measured.

## Persistence

- **Per-run values:** new `loop_config: Mapped[dict]` JSON on `RunRow`, set at run
  creation from the (possibly-overridden) defaults.
- **Fleet concurrency:** `MAX_IN_FLIGHT_LLM_CALLS` is a fleet-owned setting with
  default `4`; it is not part of `loop_config` and cannot be overridden by a run.
- **Persisted defaults:** new `loop_defaults: Mapped[dict]` JSON on the
  `fleet_control` singleton (id=1). Missing → fall back to the table defaults.
- **Loop state:** each challenger is a `CandidateRow` (add `round: int` and a
  `mechanism` marker; reuse `status`: `champion` | `discarded` | `suppressed`). On
  resume, `best` / counters / `current_mechanism` are replayed from the durable
  round-attempt ledger (one row per completed round, ordered by `round`) — the
  ledger is authoritative for recovery; round state is never inferred from
  candidate timestamps (see Engineering implementation sequence, item 1).
  `_stage_evolve` is idempotent: a fully-persisted round is skipped on re-entry.

## New / changed code

- **New:** `_stage_evolve` (the win-stay/lose-shift loop), `evolve_prompt`
  (`prompts.py`), the five constants + a typed `LoopConfig` reader,
  `loop_config`/`loop_defaults` columns + migration, run-setup UI panel + the API
  field to accept overrides, `temperature` kwarg on `LLM.complete`.
- **Reused unchanged:** panels (`_panel_for`), `_react`, `score_candidate`,
  calibration, `_critic_pass`, budget/heartbeat/kill/lease, `final` / `score` /
  `measure` / `ready`, all tables, `select_winner`.
- **Deleted:** `search_directive_prompt`, `_stage_search`, `_stage_generate`,
  `_stage_screen`, `_stage_critics` (logic moves into `evolve`).

## Deliberately NOT built (the v1 "loop no one understands")

Rejected on the record: top-k frontier / beam search, bandit / Thompson sampling,
novelty-similarity gates, mid-loop 5-panel revalidation, self-reported
convergence, extra tuning knobs. Win-stay/lose-shift already samples multiple
mechanisms on a single track; the frontier is the named next rung **only if**
single-track proves to get stuck — not day one.

## Testing

- Win-stay: an improving refinement keeps the same mechanism next round.
  Lose-shift: a non-improving attempt (at `PATIENCE=1`) forces an untried
  mechanism next round.
- `KEEP_DELTA` gate: a +0.1pp challenger under a 0.5 delta is a loss.
- Temp-0 metric: re-scoring the same idea yields the same number; a good `best`
  is never dethroned by a worse idea's lucky score.
- Each stop fires in isolation: cap, `MAX_NO_IMPROVE` dry mechanisms,
  `WIN_THRESHOLD` cop-out.
- `PATIENCE=2`: a mechanism gets two refine attempts before it counts dry.
- Resume mid-loop: re-entry after N persisted rounds does not re-pay or duplicate,
  and recomputes `best` / counters / `current_mechanism` correctly.
- No `best` ever clears the floor → `no_action`.
- `loop_config` override flows run-setup → `RunRow` → `_stage_evolve`; a confirmed
  edit updates `fleet_control.loop_defaults`; a change without typing `confirm` is
  rejected.

## Fidelity check vs Karpathy

frozen (now temp-0) metric decides keep/discard ✓ · agent reads full history to
propose the next step ✓ · one change per round ✓ · hard cap + stop-on-no-progress
✓ · token/cost guardrails ✓. Deliberate additions: `KEEP_DELTA_PP` (persona scores
noisier than `val_bpb`), `WIN_THRESHOLD_PP` (stop on a clearly-strong idea), and
win-stay/lose-shift (his hyperparameter landscape is smoother; ours is multimodal
across mechanisms, so directed restarts prevent local-optimum collapse).

## Engineering implementation sequence

1. Define the typed `LoopConfig`, round contract, named failure taxonomy, and
   durable round-attempt ledger. Make the ledger authoritative for recovery;
   do not infer round state from candidate timestamps.
2. Implement the pure win-stay / lose-shift policy and the complete transition
   matrix, including suppression, unavailable evaluation, stop, budget, lease,
   crash, and reconciliation paths.
3. Add deterministic paid-call keys, pending-call recovery, provider request
   IDs, usage IDs, worst-case reservations, and actual-cost reconciliation.
4. Migrate all stage producers and consumers to
   `context → evolve → final → score → measure → ready`, then wire champion-
   authoritative finalization.
5. Partition execution into independently leased per-Pro durable jobs. Enforce
   the fleet-wide `MAX_IN_FLIGHT_LLM_CALLS = 4` limiter across every model-call
   stage and expose the read-only setting beside the run variables in setup.
6. Run contract, transition, temperature, concurrency, reconciliation, and
   end-to-end recovery tests before enabling the loop for real traffic.

The first implementation should be mostly sequential within one Pro. The
parallelism boundary is between Pro jobs, not between rounds or calls inside a
Pro. This keeps the round ledger and champion state easy to reason about while
still allowing bounded fleet throughput.

## NOT in scope

- Timeline UI: deferred because the first release needs trustworthy status and
  recovery copy, not a second visualization surface.
- Shared-default editor: deferred because per-run snapshots and backend-owned
  defaults are enough for the first implementation.
- Advanced metrics dashboard: deferred because cost, stage, and outcome data
  must first be proven accurate in the run view.
- Rollout matrix: deferred because launch sequencing is an operational decision,
  not part of this screen’s interaction model.
- Cross-run memory: deferred because round 0 intentionally starts cold each run.
- Pause/resume controls: deferred because kill, lease recovery, and restart are
  the safer first recovery contract.
- Periodic mid-loop revalidation: deferred because it would add recurring paid
  calls before the core loop is calibrated.
- Full product design system: deferred because this feature can reuse the
  existing CSS and component vocabulary without a product-wide redesign.

Responsive refinement is tracked in the repository TODO list. It is not a
release blocker for the first loop implementation.

The run-setup panel does include the five per-run loop variables and the
read-only fleet concurrency setting because forgetting that safety decision
would create operational risk.

## Implementation Tasks

Synthesized from this design review findings. Run with Claude Code or Codex;
checkbox as you ship.

- [ ] **T1 (P1, human: ~2h / CC: ~15min)** — RunStart — split the form into Run inputs, Loop behavior, and Fleet safety groups while keeping the fleet cap visible and read-only.
  - Surfaced by: Information Architecture — the prior flat form did not distinguish per-run values from fleet-wide safety.
  - Files: `apps/web/src/components/RunStart.tsx`, `apps/web/src/app/globals.css`
  - Verify: component test asserts group headings, effective values, and read-only fleet cap are visible before submission.
- [ ] **T2 (P1, human: ~3h / CC: ~20min)** — RunStart/RunStatus — implement the loading, empty, error, success, and partial state copy and safe actions from the interaction-state table.
  - Surfaced by: Interaction State Coverage — operational states must not collapse into a generic error or empty result.
  - Files: `apps/web/src/components/RunStart.tsx`, `apps/web/src/components/RunStatus.tsx`, `apps/web/src/components/RunStart.test.tsx`, `apps/web/src/components/RunStatus.test.tsx`
  - Verify: tests cover duplicate-submit prevention, queued/waiting/degraded/failed/stopped/abstained/no-action states, and terminal action behavior.
- [ ] **T3 (P1, human: ~2h / CC: ~15min)** — RunStatus — add the immutable Run settings audit section beside status, stages, outcome, and spend.
  - Surfaced by: Unresolved Design Decisions — operators need to know which loop snapshot produced a result.
  - Files: `apps/web/src/components/RunStatus.tsx`, `apps/web/src/lib/api.ts`
  - Verify: a run with stored `loop_config` renders all five plain-language settings and cannot edit them.
- [ ] **T4 (P1, human: ~2h / CC: ~15min)** — RunStart/RunStatus — implement the keyboard and screen-reader contract, including field associations, live regions, focus recovery, and read-only fleet semantics.
  - Surfaced by: Responsive and Accessibility — existing focus and target foundations need explicit state-announcement behavior.
  - Files: `apps/web/src/components/RunStart.tsx`, `apps/web/src/components/RunStatus.tsx`, `apps/web/src/app/globals.css`
  - Verify: keyboard-only pass and automated accessibility checks confirm logical order, persistent labels, announced errors/status, and non-color state cues.
- [ ] **T5 (P2, human: ~1h / CC: ~10min)** — Web UI — apply the feature-level visual direction without adding decorative cards, gradients, icon circles, or repeated status pills.
  - Surfaced by: AI Slop Risk and Design-System Alignment — preserve the existing calm operator vocabulary.
  - Files: `apps/web/src/app/globals.css`, `apps/web/src/components/RunStart.tsx`, `apps/web/src/components/RunStatus.tsx`
  - Verify: visual review confirms hierarchy is identity/action first, configuration second, evidence/cost third.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | COMPLETE | 5 proposals, 1 accepted, 3 deferred |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | COMPLETE | 12 decisions resolved; no critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | COMPLETE | score: 4/10 → 8/10, 8 decisions |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

**VERDICT:** CEO + ENG + DESIGN CLEARED — ready to implement within the reduced backend scope. Two design follow-ups are tracked in `TODOS.md`; no mockups were generated because the gstack designer was unavailable.

NO UNRESOLVED DECISIONS
