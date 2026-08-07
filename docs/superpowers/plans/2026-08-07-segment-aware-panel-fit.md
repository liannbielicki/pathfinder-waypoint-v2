# Segment-Aware Panel Fit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop spurious "low panel fit / only 0 available" abstentions by feeding `segment` into the Pro match input and pulling the persona pool for the Pro's segment.

**Architecture:** Root cause (confirmed from `tests/test_personas_load.py`): real persona-cards items are flat — `{"persona_id","segment", <usage booleans>}` — carrying *none* of the band keys the Pro maps to. The only key that can ever be shared between a Pro and a persona is `segment`, and the Pro currently has no `segment`, so `_fit` returns `0.0` for every persona → `available=0`. Fix = (1) add `segment` to the org-context-v2 client so it reaches `match_features`, and (2) load personas for the Pro's segment instead of a hardcoded `"2B"` pool. Once both hold, `segment` is a shared, matching key → `_fit`=1.0 for every persona in the pool → panels form. `personas.py` (`select_panel`/`_fit`) needs **no change**.

**Tech Stack:** Python 3, FastAPI service, Pydantic v2, pytest / pytest-asyncio / pytest-httpx, httpx.

## Global Constraints

- Match may only use `PERMITTED_MATCH_FEATURES` (`personas.py:15`). We add `segment` to the Pro side; `segment` is already in that allowlist.
- No fabricated representativeness: a Pro with no segment, or a segment whose pool can't supply the roles, must abstain honestly — never fall back to a wrong-segment pool.
- `OrgBrief` is `extra="forbid"`: a new wire field must be added to the model before any fixture may carry it.
- One commit at the end on branch `fable/production-build` (user override of frequent-commits).
- Ponytail: `personas.py` stays untouched; no new abstraction beyond the injected async getter (mirrors the existing `create_plan` injected-callable pattern).

---

### Task 1: Feed `segment` into the Pro match input (org-context-v2 client)

**Files:**
- Modify: `services/api/src/waypoint/n8n.py` (`ALLOWED_FIELDS` ~30, `_MATCH_FEATURE_MAP` ~47, `OrgBrief` ~60)
- Test: `services/api/tests/test_n8n.py` (or nearest existing n8n test; else add a focused test here)

**Interfaces:**
- Produces: `OrgBrief.segment: str | None`; `OrgBrief.match_feature_map()` now includes key `"segment"` when the wire row has `segment`.

- [ ] **Step 1: Failing test** — a v2 row with `segment` surfaces it in `match_feature_map()`:

```python
def test_segment_reaches_match_features():
    brief = OrgBrief(org_uuid="pro_1", segment="1A", plan_tier="basic")
    assert brief.match_feature_map()["segment"] == "1A"
```

- [ ] **Step 2: Run** `pytest tests/test_n8n.py -k segment -v` — expect FAIL (`segment` not a field / not in map).

- [ ] **Step 3: Implement** in `n8n.py`:
  - Add `"segment",` to `ALLOWED_FIELDS`.
  - Add `segment: str | None = None` to `OrgBrief`.
  - Add `"segment": "segment",` to `_MATCH_FEATURE_MAP`.

- [ ] **Step 4: Run** the test — expect PASS.

---

### Task 2: Load personas for the Pro's segment (per-segment cached pool)

**Files:**
- Modify: `services/api/src/waypoint/worker.py` (`PERSONA_PANEL_REQUEST` ~47, `load_personas` ~56, `main` ~102/129)
- Modify: `services/api/src/waypoint/pipeline.py` (`PipelineDeps` ~178, `_panel_for` ~256, callers ~325 and ~439)
- Test: `services/api/tests/test_personas_load.py`

**Interfaces:**
- `load_personas(settings, segment: str) -> list[Persona]` — segment now a required arg, used in the POST body.
- `PipelineDeps.get_personas: Callable[[str], Awaitable[list[Persona]]]` replaces `personas: list[Persona]`.
- `_panel_for(state, deps, brief, size)` becomes `async`; raises `InsufficientPanelFit(size, 0)` when `brief.segment is None`.

- [ ] **Step 1: Update `test_personas_load.py`** to pass a segment and assert it is posted:

```python
personas = await load_personas(SETTINGS, "2B")
...
assert json.loads(request.content) == {**PERSONA_PANEL_REQUEST, "segment": "2B"}
```
(Keep `PERSONA_PANEL_REQUEST` as the base body without a fixed `segment`.)

- [ ] **Step 2: Run** `pytest tests/test_personas_load.py -v` — expect FAIL (`load_personas` takes 1 arg; body lacks/mismatches segment).

- [ ] **Step 3: Implement `worker.py`:**
  - `PERSONA_PANEL_REQUEST` drops the `"segment"` key (base body only).
  - `async def load_personas(settings, segment: str)` posts `json={**PERSONA_PANEL_REQUEST, "segment": segment}`.
  - Add a cached per-segment getter and inject it:

```python
def make_persona_source(settings: Settings):
    cache: dict[str, list[Persona]] = {}
    async def get_personas(segment: str) -> list[Persona]:
        if segment not in cache:
            cache[segment] = await load_personas(settings, segment)
        return cache[segment]
    return get_personas
```
  - In `main`: delete `personas = await load_personas(settings)`; build `persona_source = make_persona_source(settings)` once; pass `get_personas=persona_source` into `PipelineDeps` instead of `personas=personas`.

- [ ] **Step 4: Implement `pipeline.py`:**
  - Import: `from collections.abc import Awaitable, Callable`.
  - `PipelineDeps`: replace `personas: list[Persona]` with `get_personas: Callable[[str], Awaitable[list[Persona]]]`.
  - `_panel_for`:

```python
async def _panel_for(state: PipelineState, deps: PipelineDeps, brief: OrgBrief,
                     size: Any) -> PanelSelection:
    if brief.segment is None:
        # No segment => cannot match a real panel; abstain, never guess a pool.
        raise InsufficientPanelFit(size=size, available=0)
    personas = await deps.get_personas(brief.segment)
    pro = ProMatchInput(pro_id=brief.pro_id, features=dict(brief.match_feature_map()))
    return select_panel(pro, personas, size=size)
```
  - Both call sites become `panel = await _panel_for(state, deps, brief, 3)` (screen) and `... , 5)` (final). Their existing `except InsufficientPanelFit` blocks are unchanged.

- [ ] **Step 5: Run** `pytest tests/test_personas_load.py -v` — expect PASS.

---

### Task 3: Fix pipeline test fixtures for the new segment path

**Files:**
- Modify: `services/api/tests/conftest.py` (`FakeDeps` ~180)
- Modify: `services/api/tests/fixtures/n8n_context.json` (both orgs)
- Modify: `services/api/tests/test_pipeline.py` (~223)

**Interfaces:**
- Consumes: `PipelineDeps.get_personas` (Task 2). Fake returns the fixture `PERSONAS` regardless of segment.

- [ ] **Step 1:** In `n8n_context.json`, add `"segment": "1A"` to **both** org rows (matches the fixture personas' segment so panels form).

- [ ] **Step 2:** In `conftest.py` `FakeDeps.__init__`, replace `personas=PERSONAS` with:

```python
async def _get_personas(segment: str) -> list[Persona]:
    return PERSONAS
...
    get_personas=_get_personas,
```
(Define `_get_personas` as a module-level async fn or a local closure passed in.)

- [ ] **Step 3:** In `test_pipeline.py::test_unmatchable_pro_abstains_with_low_panel_fit`, replace the `deps.personas = [...]` mutation with a getter that returns a single-family pool:

```python
solo = [p for p in PERSONAS if p.family == "solo_operators"]
async def _solo(segment: str): return solo
deps.get_personas = _solo
```
(Single family → no distinct-family counterweight → `InsufficientPanelFit` → abstains; assertion unchanged.)

- [ ] **Step 4: Run** the full API suite:

Run: `cd services/api && .venv/bin/pytest -q`
Expected: PASS (all green, including `test_personas.py` which is untouched).

---

### Task 4: Final verification + single commit

- [ ] **Step 1:** `cd services/api && .venv/bin/pytest -q` — confirm green.
- [ ] **Step 2:** Sanity-grep no stale `deps.personas` / `personas=` remain: `grep -rn "deps.personas\|personas=PERSONAS\|await load_personas(settings)" src tests` — expect no hits.
- [ ] **Step 3:** One commit on `fable/production-build`:

```bash
git add -A && git commit -m "$(cat <<'EOF'
fix: segment-aware persona panels so matching stops abstaining at zero fit

Feed org-context-v2 `segment` into the Pro match input and pull the persona
pool for the Pro's segment instead of a hardcoded 2B pool. Real persona-cards
items only share `segment` with a Pro, so without it every fit was 0.0 and
every panel abstained with "only 0 available".

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

- **Spec coverage:** approved 4-phase plan → Phase 0 (verify) resolved from committed `test_personas_load.py`; Phase 1 = Task 1; Phase 2 = Task 2; Phase 3 (band crosswalk) **dropped** — proven unnecessary because `segment` is the sole shared key; Phase 4 (tests) = Tasks 3–4.
- **Placeholder scan:** none — segment wire field assumed `segment` (org-context-v2 convention; degrades to honest abstain if absent — no regression).
- **Type consistency:** `get_personas: Callable[[str], Awaitable[list[Persona]]]` used identically in worker inject, `PipelineDeps`, `_panel_for`, and both fake getters.
