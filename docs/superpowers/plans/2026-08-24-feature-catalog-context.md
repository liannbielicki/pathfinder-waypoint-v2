# Feature Catalog Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the HCP features a Pro's brief references to their catalog descriptions and inject them into the idea-generation context, so the generator/critic/ranker know what each feature is and whether the Pro uses it.

**Architecture:** A new `catalog.py` loads the packaged CSV once at import and exposes one function, `feature_context(brief, *, feasibility)`, that returns a resolved-features block. The evolve loop appends that block to the single `org_context` string at `pipeline.py:940`; generation, critic, and ranker already read that one variable, so all three see identical context. A default-off `Settings` flag toggles CTA-feasibility hints on the block.

**Tech Stack:** Python 3.14, stdlib `csv` + `re`, pydantic v2 (`OrgBrief`), pytest, `services/api` (`uv`).

## Global Constraints

- Waypoint is **recommendation-only**; do not change scoring, measurement, warm-start retrieval, persona screen, or the `handoff.py` contract.
- No new service, RAG store, or vector DB — plain in-process CSV load + dict lookup.
- Do NOT touch `war_game_prompt` (`pipeline.py:1465`) or the dead `generator_prompt`.
- Deterministic replay must be preserved: the augmented context is additive and deterministic, so recorded calls and ledger replay are unaffected.
- Work on branch `V2-Improvements`. Ship as **exactly one commit** (Task 4 squashes the per-task commits back to the tag `wp-fcatalog-base`, which marks HEAD before any implementation commit).
- Run tests: `cd services/api && uv run pytest -q`. Lint/type: `uv run ruff check src tests` and `uv run mypy src` must stay clean.
- Style: small functions (<50 lines), small files (<400 lines); comments only for constraints the code can't show; match the heavily-commented existing idiom.
- Packaged runtime data lives in `services/api/data/` (see `worker.py:45`), NOT in `src/`.
- Spec: `docs/superpowers/specs/2026-08-24-feature-catalog-context-design.md`.

---

### Task 1: Catalog module + packaged CSV + unit tests

**Files:**
- Create: `services/api/data/hcp_feature_catalog.csv` (copy of `docs/knowledge/hcp_feature_catalog.csv`)
- Create: `services/api/src/waypoint/catalog.py`
- Test: `services/api/tests/test_catalog.py`

**Interfaces:**
- Consumes: `waypoint.n8n.OrgBrief` (pydantic model; feature fields are `feature_<key>_state: str | None` and `top_unused_paid_feature: str | None`).
- Produces:
  - `CATALOG: dict[str, CatalogEntry]` — module-level, loaded at import.
  - `CatalogEntry(feature: str, description: str, ctas: tuple[dict[str, str], ...])` — `description` already trimmed to its first sentence.
  - `feature_context(brief: OrgBrief, *, feasibility: bool) -> str` — the resolved block, or `""` when nothing resolves.

- [ ] **Step 1: Copy the CSV into the service package**

```bash
cd /Users/jakefassora/projects/pathfinder-waypoint-v2
cp docs/knowledge/hcp_feature_catalog.csv services/api/data/hcp_feature_catalog.csv
```

- [ ] **Step 2: Write the failing tests**

Create `services/api/tests/test_catalog.py`:

```python
from waypoint.catalog import CATALOG, feature_context, _first_sentence
from waypoint.n8n import OrgBrief


def _brief(**fields) -> OrgBrief:
    return OrgBrief(org_uuid="org-1", **fields)


def test_first_sentence_trims_at_capital_boundary():
    text = "A does X. Distinct from Y and Z."
    assert _first_sentence(text) == "A does X."


def test_first_sentence_ignores_eg_period():
    # "e.g. quarterly" must NOT be treated as a sentence end (lowercase follows).
    text = "Plans (e.g. quarterly tune-ups) sold to a customer. Distinct from a job."
    assert _first_sentence(text) == "Plans (e.g. quarterly tune-ups) sold to a customer."


def test_first_sentence_keeps_unbroken_text():
    assert _first_sentence("no boundary here") == "no boundary here"


def test_catalog_loads_and_groups_multi_row_feature():
    entry = CATALOG["online_booking"]
    assert entry.description.startswith("Lets a customer request or schedule")
    assert entry.description.endswith("office.")  # trimmed to first sentence
    assert len(entry.ctas) >= 2  # primary + replacement CTA rows


def test_feature_context_resolves_union_and_marks_top_unused():
    brief = _brief(
        feature_voip_state="attached_unused",
        top_unused_paid_feature="wisetack",
    )
    block = feature_context(brief, feasibility=False)
    assert "wisetack" in block and "TOP UNUSED PAID FEATURE" in block
    assert "voip (state: attached_unused" in block  # state passed verbatim


def test_feature_context_empty_when_no_features():
    assert feature_context(_brief(), feasibility=False) == ""


def test_feature_context_skips_unresolvable_feature():
    # A pointer to a feature with no catalog description must not crash or emit a line.
    block = feature_context(_brief(top_unused_paid_feature="customer_portal"), feasibility=False)
    assert block == ""


def test_feasibility_toggle_changes_payload():
    brief = _brief(feature_voip_state="attached_unused")
    off = feature_context(brief, feasibility=False)
    on = feature_context(brief, feasibility=True)
    assert "reachable on" not in off
    assert "reachable on" in on  # works_on summary only when feasibility=True
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd services/api && uv run pytest tests/test_catalog.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'waypoint.catalog'`.

- [ ] **Step 4: Write `catalog.py`**

Create `services/api/src/waypoint/catalog.py`:

```python
"""HCP feature catalog: resolve the features a Pro's brief references to their
human-readable meaning, so the idea generator knows what each feature IS and
whether this Pro uses it. Loaded once at import from the packaged CSV.

No RAG, no dump: only features the brief already references are resolved,
mirroring warmstart.retrieve's select-the-relevant-thing discipline.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from waypoint.n8n import OrgBrief

# Packaged runtime data, same convention as worker.CALIBRATION_PATH.
CATALOG_PATH = Path(__file__).parents[2] / "data" / "hcp_feature_catalog.csv"

_STATE_PREFIX = "feature_"
_STATE_SUFFIX = "_state"
_CTA_COLUMNS = ("label", "url", "works_on", "notes")

# Sentence boundary = ". " followed by a capital. Skips "e.g. " / "i.e. " which
# several descriptions contain, so the first sentence is not truncated mid-clause.
_SENTENCE_END = re.compile(r"\. (?=[A-Z])")

# Only when the feasibility toggle is on: one directive appended after the block.
_FEASIBILITY_DIRECTIVE = (
    "Feasibility: prefer features reachable on this touch's delivery channel; "
    "do not anchor an idea on a feature whose only destination is web-only or "
    "marked broken for the channel being sent."
)


@dataclass(frozen=True)
class CatalogEntry:
    feature: str
    description: str  # already trimmed to first sentence
    ctas: tuple[dict[str, str], ...]  # label/url/works_on/notes rows, file order


def _first_sentence(text: str) -> str:
    match = _SENTENCE_END.search(text)
    return text[: match.start() + 1] if match else text


def _load(path: Path = CATALOG_PATH) -> dict[str, CatalogEntry]:
    rows_by_feature: dict[str, list[dict[str, str]]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            feature = (row.get("feature") or "").strip()
            if feature:
                rows_by_feature.setdefault(feature, []).append(row)
    catalog: dict[str, CatalogEntry] = {}
    for feature, rows in rows_by_feature.items():
        description = next(
            (r["description"].strip() for r in rows if (r.get("description") or "").strip()),
            "",
        )
        ctas = tuple(
            {col: (r.get(col) or "").strip() for col in _CTA_COLUMNS} for r in rows
        )
        catalog[feature] = CatalogEntry(feature, _first_sentence(description), ctas)
    return catalog


CATALOG: dict[str, CatalogEntry] = _load()


def _state_features(brief: OrgBrief) -> dict[str, str]:
    """{feature_key: state} for every feature_<key>_state set on the brief, in
    field-declaration order. Derived from the model's fields, so a new
    feature_<key>_state column resolves with no change here."""
    out: dict[str, str] = {}
    for name in type(brief).model_fields:
        if name.startswith(_STATE_PREFIX) and name.endswith(_STATE_SUFFIX):
            value = getattr(brief, name)
            if value is not None:
                out[name[len(_STATE_PREFIX) : -len(_STATE_SUFFIX)]] = value
    return out


def _feasibility_suffix(entry: CatalogEntry) -> str:
    works = sorted({c["works_on"] for c in entry.ctas if c["works_on"]})
    return f" [reachable on: {', '.join(works)}]" if works else ""


def feature_context(brief: OrgBrief, *, feasibility: bool) -> str:
    """The resolved-features block for one Pro, or "" if nothing resolves.
    Ordered union: top_unused_paid_feature first (the priority activation
    target), then every feature_<key>_state present; deduped by key."""
    states = _state_features(brief)
    top = brief.top_unused_paid_feature or None
    keys: list[str] = ([top] if top else []) + [k for k in states if k != top]

    lines: list[str] = []
    for key in keys:
        entry = CATALOG.get(key)
        if entry is None or not entry.description:
            continue  # unresolvable or description-less pointer: skip, never crash
        tag = ", TOP UNUSED PAID FEATURE" if key == top else ""
        state = states.get(key, "unknown")
        line = f"- {key} (state: {state}{tag}): {entry.description}"
        if feasibility:
            line += _feasibility_suffix(entry)
        lines.append(line)
    if not lines:
        return ""

    header = "HCP features referenced in this Pro's context (reference data):"
    block = "\n".join([header, *lines])
    return f"{block}\n{_FEASIBILITY_DIRECTIVE}" if feasibility else block
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd services/api && uv run pytest tests/test_catalog.py -q`
Expected: PASS (all 8 tests).

- [ ] **Step 6: Lint, type-check, commit**

```bash
cd services/api && uv run ruff check src tests && uv run mypy src
git add services/api/data/hcp_feature_catalog.csv services/api/src/waypoint/catalog.py services/api/tests/test_catalog.py
git commit -m "feat: catalog module + packaged CSV"
```

---

### Task 2: Settings flag + PipelineDeps field + worker threading

**Files:**
- Modify: `services/api/src/waypoint/settings.py` (add field after `KILL_SWITCH`)
- Modify: `services/api/src/waypoint/pipeline.py` (add field to `PipelineDeps`)
- Modify: `services/api/src/waypoint/worker.py:238` (pass the flag into `PipelineDeps(...)`)
- Test: `services/api/tests/test_settings.py`

**Interfaces:**
- Consumes: `feature_context` from Task 1 (used in Task 3, not here).
- Produces: `Settings.CTA_FEASIBILITY_HINTS: bool = False`; `PipelineDeps.cta_feasibility_hints: bool = False`.

- [ ] **Step 1: Write the failing test**

Add to `services/api/tests/test_settings.py`:

```python
def test_cta_feasibility_hints_defaults_off(monkeypatch):
    from waypoint.settings import Settings
    # Provide the required env so load() succeeds; default of the new flag is what we assert.
    for key, val in _MINIMAL_ENV.items():  # reuse the file's existing minimal-env helper
        monkeypatch.setenv(key, val)
    monkeypatch.delenv("CTA_FEASIBILITY_HINTS", raising=False)
    assert Settings.load().CTA_FEASIBILITY_HINTS is False
```

> If `test_settings.py` has no `_MINIMAL_ENV` helper, read the file's existing setup and mirror whatever fixture/env pattern the other Settings tests use to construct a valid `Settings`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd services/api && uv run pytest tests/test_settings.py -k cta_feasibility -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'CTA_FEASIBILITY_HINTS'`.

- [ ] **Step 3: Add the Settings field**

In `services/api/src/waypoint/settings.py`, immediately after the `KILL_SWITCH: bool = False` line:

```python
    # Feature-catalog CTA feasibility hints in idea context. Default OFF: today's
    # world is SMS-only and we do not yet trust channel<->works_on filtering.
    # Flip ON once multi-channel is live so ideas avoid web-only/broken links.
    CTA_FEASIBILITY_HINTS: bool = False
```

- [ ] **Step 4: Add the PipelineDeps field**

In `services/api/src/waypoint/pipeline.py`, in the `PipelineDeps` dataclass, after `metric_catalog`:

```python
    # Feature-catalog feasibility toggle (Settings.CTA_FEASIBILITY_HINTS). Off
    # keeps the idea context to description+state; on adds works_on hints.
    cta_feasibility_hints: bool = False
```

- [ ] **Step 5: Thread it through the worker**

In `services/api/src/waypoint/worker.py`, in the `PipelineDeps(...)` construction at ~line 238, add (alongside `metric_catalog=metric_catalog,`):

```python
                    cta_feasibility_hints=settings.CTA_FEASIBILITY_HINTS,
```

- [ ] **Step 6: Run tests, lint, type-check, commit**

```bash
cd services/api && uv run pytest tests/test_settings.py -q && uv run ruff check src tests && uv run mypy src
git add services/api/src/waypoint/settings.py services/api/src/waypoint/pipeline.py services/api/src/waypoint/worker.py services/api/tests/test_settings.py
git commit -m "feat: CTA_FEASIBILITY_HINTS flag threaded to PipelineDeps"
```

---

### Task 3: Wire the block into the evolve context + consistency tests + generator_prompt note

**Files:**
- Modify: `services/api/src/waypoint/pipeline.py:940` (augment `org_context`)
- Modify: `services/api/src/waypoint/prompts.py` (one-line note on `generator_prompt`)
- Test: `services/api/tests/test_pipeline.py`

**Interfaces:**
- Consumes: `feature_context` (Task 1), `deps.cta_feasibility_hints` (Task 2).
- The test fixture brief (`tests/fixtures/n8n_context.json`) already carries `feature_online_booking_state: "attached_unused"`, so the block is non-empty in pipeline tests and `online_booking`'s trimmed description ("…without calling the office.") is a stable substring to assert.

- [ ] **Step 1: Write the failing tests**

Add to `services/api/tests/test_pipeline.py`:

```python
FEATURE_SUBSTR = "without calling the office"  # from online_booking's trimmed description


@pytest.mark.asyncio
async def test_feature_block_shared_by_generate_critic_rank(deps: FakeDeps, seeded_job) -> None:
    """The resolved feature block must reach generation, critic, AND ranker
    identically — a critic that can't see it wrongly blocks grounded ideas."""
    await run_job(seeded_job.id, deps)
    for stage in ("evolve", "critics", "rank"):
        prompts = deps.gateway.prompts_for(stage)
        assert prompts, f"no {stage} prompt captured"
        assert all(FEATURE_SUBSTR in p for p in prompts), f"{stage} missing feature block"


@pytest.mark.asyncio
async def test_feasibility_hints_off_by_default_in_context(deps: FakeDeps, seeded_job) -> None:
    await run_job(seeded_job.id, deps)
    assert all("reachable on" not in p for p in deps.gateway.prompts_for("evolve"))
```

> If `FakeDeps` needs the flag set on for a positive feasibility assertion later, set `deps.cta_feasibility_hints = True` before `run_job`. Default construction leaves it `False` (dataclass default), matching production default.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/api && uv run pytest tests/test_pipeline.py -k "feature_block or feasibility_hints_off" -q`
Expected: FAIL — `FEATURE_SUBSTR` not in the prompts (block not yet wired).

- [ ] **Step 3: Wire the block into `org_context`**

In `services/api/src/waypoint/pipeline.py`, add the import near the other `waypoint.*` imports:

```python
from waypoint.catalog import feature_context
```

Then change the `org_context` assignment at `:940` from:

```python
        org_context = brief.model_dump_json()
```

to:

```python
        org_context = brief.model_dump_json()
        # Resolve the features this brief references to their catalog meaning,
        # so generation/critic/ranker (all reading this one string) know what
        # each feature is and whether this Pro uses it. Additive + deterministic.
        block = feature_context(brief, feasibility=deps.cta_feasibility_hints)
        if block:
            org_context = f"{org_context}\n{block}"
```

- [ ] **Step 4: Add the `generator_prompt` unused note**

In `services/api/src/waypoint/prompts.py`, add a comment directly above `def generator_prompt(` (line 63):

```python
# NOTE: currently unused — the pipeline generates via evolve_prompt only. If you
# revive a cold-start generator, augment its org_context with catalog.feature_context
# the same way the evolve stage does, or its ideas run blind to feature meaning.
```

- [ ] **Step 5: Run the new tests + full suite**

Run: `cd services/api && uv run pytest tests/test_pipeline.py -k "feature_block or feasibility_hints_off" -q`
Expected: PASS.

Run: `cd services/api && uv run pytest -q`
Expected: PASS — the whole suite stays green (augmented context is additive/deterministic, so replay and recorded-call tests are unaffected).

- [ ] **Step 6: Lint, type-check, commit**

```bash
cd services/api && uv run ruff check src tests && uv run mypy src
git add services/api/src/waypoint/pipeline.py services/api/src/waypoint/prompts.py services/api/tests/test_pipeline.py
git commit -m "feat: inject feature catalog block into evolve context"
```

---

### Task 4: Full verification + squash to one commit

**Files:** none (git + verification only).

- [ ] **Step 1: Full gate**

```bash
cd services/api && uv run pytest -q && uv run ruff check src tests && uv run mypy src
```
Expected: all pass, zero lint/type errors.

- [ ] **Step 2: Squash the per-task commits into one**

The tag `wp-fcatalog-base` marks HEAD before any implementation commit; all
Task 1-3 commits sit on top of it. Collapse them:

```bash
cd /Users/jakefassora/projects/pathfinder-waypoint-v2
git reset --soft wp-fcatalog-base
git commit -m "feat: wire HCP feature catalog into Pro idea-generation context

Resolve the features a Pro's brief references (feature_<key>_state +
top_unused_paid_feature) to their catalog descriptions and inject them into the
single org_context read by generation, critic, and ranker. New catalog.py loads
the packaged CSV once at import. CTA_FEASIBILITY_HINTS (default off) gates
works_on hints for the future multi-channel world.

Spec: docs/superpowers/specs/2026-08-24-feature-catalog-context-design.md

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 3: Verify one clean commit**

```bash
git log --oneline -2
git show --stat HEAD
```
Expected: HEAD is the single feature commit; `wp-fcatalog-base` is its parent; the diffstat lists exactly `catalog.py`, the packaged CSV, `settings.py`, `pipeline.py`, `worker.py`, `prompts.py`, `test_catalog.py`, `test_settings.py`, `test_pipeline.py`. Then delete the tag: `git tag -d wp-fcatalog-base`.

---

## Self-Review

- **Spec coverage:** catalog module + packaged CSV (Task 1) · union selection + verbatim state + top-unused mark + skip-unresolvable + first-sentence trim (Task 1) · toggle default off/on payload (Tasks 1-2) · single-variable wiring with shared-context consistency test (Task 3) · generator_prompt note (Task 3) · war_game excluded (untouched, no task) · one commit (Task 4). All spec sections map to a task.
- **Placeholder scan:** every code step carries complete code; the two `>` notes point the implementer at an existing file pattern to mirror (`_MINIMAL_ENV`, `deps.cta_feasibility_hints`), not deferred work.
- **Type consistency:** `feature_context(brief, *, feasibility: bool) -> str`, `CatalogEntry(feature, description, ctas)`, `CATALOG`, `_first_sentence`, `deps.cta_feasibility_hints`, and `Settings.CTA_FEASIBILITY_HINTS` are named identically everywhere they appear across tasks.
