# Pathfinder Waypoint V2

Clean implementation repository for the Pathfinder production rebuild.

Start here:

1. `docs/specs/pathfinder-production-rebuild-design.md` — approved design
2. `FRONTEND.md` — UI principles, flows, states
3. `docs/plans/pathfinder-waypoint-v2-implementation.md` — implementation plan
4. `docs/environment.md` — environment ownership and setup
5. `docs/HUMAN-TASKS.md` — remaining human-owned actions before launch

## Layout

- `apps/web` — Next.js operator frontend (Vercel)
- `services/api` — FastAPI API and workers (Railway)
- `contracts/openapi.json` — committed API contract
- `docs/knowledge/` — preserved legacy audit artifacts (reference only)

## Local setup

Toolchain: Python 3.14 + uv, Node.js 24 LTS, pnpm 11.4 (see `docs/environment.md`).

```bash
# API
cd services/api
cp ../../.env.example .env   # then fill values
uv sync
uv run pytest -q

# Frontend
cd apps/web
pnpm install
pnpm dev
```

## Deployment

**Railway** (API + workers, all secrets):

```bash
# From services/api — one service for the API, one for the worker fleet,
# both from the same Dockerfile image (see railway.json / Procfile).
railway up
# Web process:    uvicorn waypoint.api:app --host 0.0.0.0 --port $PORT
# Worker process: python -m waypoint.worker   (runs WORKER_COUNT concurrent loops; one replica)
# Migrations:     uv run alembic upgrade head  (DATABASE_URL from Railway)
```

Set every variable from `.env.example` (except `API_BASE_URL`) in Railway.
Health check: `curl https://<railway-domain>/health` → `{"status": "ok"}`.

**Vercel** (frontend, no secrets):

```bash
# From apps/web. The only environment value is API_BASE_URL (not secret),
# consumed by the /api/* rewrite in next.config.ts.
vercel deploy --prod
```

**CI** runs on every push: API (ruff, mypy, pytest against Postgres 17) and
web (vitest, eslint, next build, Playwright) — see `.github/workflows/ci.yml`.

## Journey-window touch optimization

Each run now carries a `journey_window` (`churn_risk` | `onboarding` | `upsell`,
default `churn_risk`) from creation through to the LCM handoff — set on
`RunCreate`/`RunRow`, surfaced in `RunView` and the run-start UI
(`apps/web/src/components/RunStart.tsx`), and threaded into the evolve prompt
and the feasibility gate.

- **Pre-spend feasibility gate** (`waypoint/feasibility.py`, wired into
  `_stage_evolve` in `waypoint/pipeline.py`): `gate_pro()` blocks a Pro before
  any LLM/persona spend when the journey window contradicts the brief (e.g.
  `churn_risk` window but the Pro shows no churn signal) or no channel is
  contactable. A block abstains the Pro with `infeasible: <reason>` and the
  offending channel is suppressed as `infeasible_channel` rather than reaching
  candidate generation.
- **Touch outcome ingestion** (`POST /api/outcomes` in `waypoint/api.py`):
  accepts outcome rows keyed by the Waypoint winner ID, spelled either
  `recommendation_id` or `row_id` (an alias — the LCM handoff now carries this
  id as `row_id` in each Pathfinder Intake row, so outcome sources may echo
  either spelling back). Rows that match no `WinnerRow` are stored with
  `evidence_limitation = "unattributed: recommendation_id matches no winner"`
  instead of being dropped or guessed at. Resubmissions for the same
  `(recommendation_id, source)` merge in place — only non-`None` outcome flags
  overwrite, so a later partial update never erases a previously measured
  value.
- **Evidence-informed generation** (`waypoint/evidence.py`): `pattern_summaries`
  aggregates historical, attributable touch outcomes by `(channel, mechanism)`
  within a journey window into `returned_7d/14d/30d/90d` tri-state rates;
  `failed_mechanisms` flags mechanisms that recently failed or drove an
  unsubscribe for a specific Pro. Both feed `evidence_block`, which renders
  into `evolve_prompt` — with an explicit "no evidence, treat every idea as
  unproven" state when the evidence store is empty. `recently_failed`
  suppression in `_stage_evolve` rejects a mechanism a Pro already failed
  before spending a critic or persona call on it.
- **Cross-run persona reaction cache** (`persona_evals` table): reactions are
  cached under a `sha256(PROMPT_VERSION + persona panel + concept + channel)`
  key, so an identical concept/channel/panel combination across runs is
  scored once, not re-evaluated per run.
- **Return-to-app indicators** (`waypoint/measurement.py`): `app_return` (7-day)
  and `app_continued_use` (30-day) are Amplitude-sourced entries in the
  measurement catalog, explicitly marked contract-pending until the canonical
  event names are agreed (see TODOS.md).
- **Bounded war-game follow-up** (`waypoint/prompts.py` + `_stage_measure` in
  `waypoint/pipeline.py`): every winner gets an optional, non-blocking
  four-branch follow-up plan (`on_return`, `on_click_no_use`,
  `on_no_interaction`, `on_negative`) generated after the fact; `on_negative`
  is always forced to `{"action": "stop", "channel": "none"}` regardless of
  what the model returns. A winner ships with or without a follow-up plan.
  Both `run.journey_window` and `winner.evidence["follow_up"]` are stored and
  readable via the API, but **neither is forwarded to LCM**: the handoff now
  sends the fixed Pathfinder Intake row shape
  (`pro_uuid`, `theme`, `theme_category`, `org_id`, `row_id`) as one batch POST
  (`waypoint/api.py`, `waypoint/handoff.py`); extending that contract to carry
  `journey_window`/`follow_up` is pending confirmation with Allison.

**Still pending externally, not solvable inside this repo:**

- `row_id` (the Waypoint winner ID) rides through the Pathfinder Intake
  handoff already; LCM/Iterable still need to echo it back on outcome
  webhooks so outcomes can be joined to the exact recommendation that caused
  them (see TODOS.md — "Stable recommendation attribution across LCM and
  Iterable").
- The canonical Amplitude active-use event contract (event names, identity
  fields, timezone/horizon rules) is unresolved (see TODOS.md — "Canonical
  Amplitude active-use event contract").
- Until an outcome source actually posts to `/api/outcomes`, the evidence
  store is empty and generation runs honestly with the "no evidence" block —
  this is expected, not a bug.

The legacy repository and audit branch are reference-only. Do not copy their application structure into this repository.
