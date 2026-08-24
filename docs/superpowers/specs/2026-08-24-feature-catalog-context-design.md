# Wire the HCP Feature Catalog into Pro Context — Design

Date: 2026-08-24
Branch: `V2-Improvements`
Ships as: **one commit**.

## Problem

The idea generator sees only bands. A Pro's context is
`brief.model_dump_json()` — labels like `feature_voip_state: "attached_unused"`
and `top_unused_paid_feature: "wisetack"`. The model is never told what those
features *are*, so ideas that lean on a feature are vague or generic. The CSV at
`docs/knowledge/hcp_feature_catalog.csv` carries the human-readable meaning (and
CTA/link info) for each feature but is not wired into the service at all.

Goal: resolve the features a Pro's brief already references to their catalog
descriptions and inject them into context, so the model knows what each feature
is and whether this Pro uses it — producing more concrete, better-grounded
ideas. Making the loop smarter, not more constrained.

## Non-goals

- No new service, RAG store, or vector DB. Plain in-process CSV load + dict
  lookup, matching the existing `warmstart.retrieve` / `personas` discipline
  (select the relevant thing, never dump the world).
- Do not change the return-to-app objective, scoring, measurement, warm-start
  retrieval, persona screen, or the `handoff.py` contract.
- Do not touch the dead `generator_prompt` path (see below).
- **Do not augment `war_game_prompt`** (`pipeline.py:1465`). It plans a
  post-selection follow-up, not an idea, and is not critic-gated; feeding it the
  feature block is a separate future change. Its context intentionally stays the
  bare brief — this exclusion is deliberate, not an oversight.
- No per-Pro device (iOS/Android) feasibility — the brief carries no device
  field. Out of scope and impossible with current data (see Limitations).

## Key facts established during brainstorming

1. **`generator_prompt` is dead code.** The pipeline is all-`evolve_prompt`
   (`pipeline.py:612`). Only the live evolve path is wired. `generator_prompt`
   is left untouched; a one-line note is added marking it unused so no one wires
   a feature block into a path that never runs.
2. **The three idea-generation stages already share one `org_context`.**
   Verified at the call sites: the local `org_context` built at `pipeline.py:940`
   is passed to generation (`_prompt_builder`), the batch critic
   (`_verdicts_for_batch`, at `:1007`), and the candidate ranker (`_rank_batch`,
   at `:1033`). So augmenting that **single variable** makes all three see the
   identical string automatically — consistency is nearly free, not a
   three-site edit. The critic blocks ideas as `"ungrounded"` when they cite a
   value "not in the context," so this shared-string property is load-bearing: a
   regression test asserts all three stages receive byte-identical context.
   `war_game_prompt` at `:1465` is a *different* prompt (downstream follow-up
   planning, not idea generation, not critic-gated) and is deliberately left out
   of scope — see Non-goals.
3. **Feature-state vocabulary is not pinned in the repo** (only
   `"attached_unused"` appears). It is produced by the n8n flow. The design must
   not hardcode a set of "adopted" states; it passes state values through
   verbatim.

## Design

### 1. New module: `services/api/src/waypoint/catalog.py`

- Load a **packaged** copy of the CSV once at import into a module-level dict
  keyed by `feature`. Runtime data must ship with the deploy, so the CSV is
  copied to `services/api/data/hcp_feature_catalog.csv` — matching the existing
  packaged-data convention (`worker.py:45` loads
  `Path(__file__).parents[2] / "data" / ...`). The `docs/knowledge/` copy
  remains as documentation (the two may diverge; only the packaged one is
  load-bearing).
- The CSV has **multiple rows per feature** (a primary row with the
  `description`, plus additional CTA rows with empty `description`). The loader
  groups by `feature`:
  - `description`: taken from the first row of the feature that has a non-empty
    `description`.
  - CTA rows: the `label`/`url`/`works_on`/`notes` of every row for that
    feature, in file order, are retained for the feasibility payload.
- Trim helper: `description` is trimmed to its **first sentence**, dropping the
  "Distinct from X…" disambiguation tail. Keeps token cost flat even when a Pro
  has all ~11 feature-state fields populated. The split is on the first `". "`
  **followed by a capital letter** — a naive `". "` split truncates at the
  `"e.g. "` / `"i.e. "` that several descriptions contain, so the capital-letter
  guard is required. If no such boundary is found, the whole description is kept.
- Rows whose `feature` is `top_unused_paid_feature`, `generic_fallback`, or any
  value flagged `not_applicable`/`no_cta` in `works_on` are still loaded but
  carry no injectable description of their own; the resolver simply skips a
  feature key it cannot resolve rather than crashing.

Public surface (the whole interface — internals can change freely behind it):

```python
def feature_context(brief: OrgBrief, *, feasibility: bool) -> str:
    """The resolved-features block for one Pro, or "" if nothing resolves."""
```

### 2. Feature selection (in `feature_context`)

Resolve the **union** of:
- every `feature_<name>_state` field present (non-None) on the brief, and
- `top_unused_paid_feature` (its value is itself a feature key; may have no
  `feature_<name>_state` field of its own, e.g. `instapay`).

Dedupe by feature key. For each resolved feature emit one line:

```
- <feature> (state: <verbatim state or "unknown">)<, TOP UNUSED PAID if applicable>: <trimmed description>
```

- State is passed through **verbatim** from `feature_<name>_state`; a feature
  reached only via `top_unused_paid_feature` with no state field shows
  `state: unknown`.
- The `top_unused_paid_feature` entry is marked so the model knows it is the
  priority activation target.
- No "adopted vs unused" filter: passing the state through and letting the model
  reason is robust to n8n vocab drift and satisfies the "unused + adopted"
  requirement without interpreting the vocabulary in code.
- A feature key not found in the catalog (typo, retired feature) is skipped
  silently — never a crash, never a partial line.

Ordering is deterministic (`top_unused_paid_feature` first, then the brief's
`feature_<key>_state` fields in model-field declaration order) so the augmented
context string is stable across the three stages and across replays.

### 3. Payload toggle — `Settings.CTA_FEASIBILITY_HINTS: bool = False`

A single, clearly-named env flag on the existing `Settings` class (same shape as
`KILL_SWITCH`), default `False`.

- **Off (today, SMS-only world):** each line is description + state only. Keeps
  the "these ideas are SEEDS, not final copy" boundary clean — the marketing
  team still owns CTA/link selection downstream.
- **On (future, multi-channel):** each line additionally carries the feature's
  `works_on` values and a `broken` flag, and `feature_context` appends one
  channel-feasibility directive, e.g.:
  > "For an SMS touch, avoid anchoring an idea on a feature whose only
  > destination is web-only or marked broken; prefer features reachable on the
  > delivery channel."

The flag threads from `Settings` → worker → `PipelineDeps` (a bool field) →
the evolve stage, alongside the existing deps. No per-run UI, no new
`LoopConfig` key. Upgrade path if we later want per-run control: promote to a
`loop_config`/run field — deferred until feasibility proves out as the right
direction (YAGNI).

### 4. Wiring in `pipeline.py`

- Change the single `org_context` assignment in the evolve loop (`:940`) to the
  augmented value. Generation, critic (`:1007`), and ranker (`:1033`) already
  read this one variable, so all three pick up the block with no further edit:

  ```python
  org_context = brief.model_dump_json()
  block = feature_context(brief, feasibility=deps.cta_feasibility_hints)
  if block:
      org_context = f"{org_context}\n{block}"
  ```

  No other call site changes. `war_game_prompt` at `:1465` keeps the bare brief
  (Non-goals).
- The block is appended *outside* the org JSON, then the whole thing is fenced
  by the existing `fenced_context(...)` in each prompt — so the catalog text
  inherits the same untrusted-context treatment as the brief. (Catalog content
  is our own trusted data, but fencing it is free and keeps one uniform rule.)

### 5. `generator_prompt`

Left as-is. Add a one-line comment noting it is currently unused (pipeline is
all-`evolve`), so a future reader does not wire a feature block into a dead
path. Deleting it is out of scope for this commit.

## Limitations (stated so nobody expects more)

- **No device targeting.** `works_on` distinguishes web/ios/mobile/broken, but
  the brief has no per-Pro device field, so the feasibility toggle can only
  reason channel↔`works_on` coarsely (SMS vs email), never iOS-vs-Android for a
  specific Pro. Device-aware feasibility needs a new brief field and is a
  separate future change.
- **Packaged CSV can drift from the docs copy.** Only the packaged one is
  load-bearing; keeping them in sync is a manual convention, acceptable for a
  47-row reference file that changes rarely.

## Testing

New `tests/test_catalog.py`:
- Loader parses the real packaged CSV, keys by feature, groups multi-row
  features, picks the description from the row that has one.
- First-sentence trim behaves on a multi-sentence and a single-sentence
  description.
- `feature_context` resolves the union of state fields + `top_unused_paid_feature`,
  dedupes, passes state verbatim, marks the top-unused entry.
- Toggle off vs on: on adds `works_on`/broken + the directive; off does not.
- Unknown / unresolvable feature key is skipped, never raises; a brief with no
  feature fields yields `""`.

Extend pipeline tests:
- **Consistency test (load-bearing):** in one evolve round the generator,
  critic, and ranker are invoked with byte-identical org context.
- A grounded idea that references an injected feature description is NOT blocked
  `"ungrounded"` by the critic (regression guard for the consistency bug).

Existing suites (`test_prompts`, `test_pipeline`, `test_loop`, replay/resume)
must stay green: augmented context is additive and deterministic, so recorded
calls and ledger replay are unaffected.

## Definition of done

- `uv run pytest -q`, `uv run ruff check src tests`, `uv run mypy src` all clean.
- One commit on `V2-Improvements` containing: `catalog.py`, packaged CSV,
  `Settings` flag, `PipelineDeps` field + worker threading, `pipeline.py`
  wiring, `generator_prompt` note, and all tests.
