# Pathfinder Waypoint V2 — Human Tasks

## Launch status

The repository is **build-complete and blocked from production by missing
credentials and deployments**. Every fixture-backed gate passes on branch
`fable/production-build`: 88 API tests (ruff/mypy strict clean), the
200-Pro integrity load gate (`docs/verification/launch-report.md`), 35
frontend component tests, lint, production build, and 9 Playwright lifecycle
specs. No live credential was available during the build, so nothing has been
deployed and no live contract has been verified. Per the approved design, the
system is **not production-ready** until the live gates below pass — the
committed load gate proves orchestration integrity with deterministic model
and context fakes, not live-model capacity.

## Credentials and environment

A human must set these values (never their values in git). All names match
`.env.example`.

**Railway (API service and worker service — owns every secret):**

- `DATABASE_URL` — Supabase/Postgres connection string (asyncpg form)
- `LLM_API_KEY` — Anthropic API key
- `N8N_CONTEXT_URL`, `N8N_TOKEN` — existing n8n context webhook
- `PERSONA_URL`, `PERSONA_TOKEN` — persona snapshot service
- `HANDOFF_URL`, `HANDOFF_TOKEN` — Allison's LCM intake
- `RUN_COST_USD`, `DAY_COST_USD` — budget limits
- `WORKER_COUNT`, `KILL_SWITCH`, `MODEL_FAST`, `MODEL_DEEP`, `LOG_LEVEL`
- `APP_PASSWORD` — operator login
- `SESSION_KEY` — generate with `openssl rand -hex 32`

**Vercel (frontend — one non-secret value):**

- `API_BASE_URL` — the Railway origin, consumed by the `/api/*` rewrite

**Access work automation could not do:** create/link the Railway project, the
Vercel project, and the Supabase database; enable GitHub Actions on the repo
(CI is committed at `.github/workflows/ci.yml` but has never run on GitHub);
Docker was not installed on the build machine, so
`docker build -t waypoint-api services/api` is unverified.

## External contracts

- **n8n context flow**: confirm the deployed workflow accepts
  `POST {"pro_ids": [...]}` with a bearer token and returns the
  `org_context_v1` shape in `services/api/tests/fixtures/n8n_context.json`,
  including the optional matching fields (`segment`, `plan`, `tenure_bucket`,
  `org_size_bucket`, `trade_bucket`, `open_ar_band`) the rebuild added for
  persona matching. Proof: `cd services/api && N8N_CONTEXT_URL=… N8N_TOKEN=…
  LIVE_TEST_PRO=… uv run pytest tests/test_n8n_live.py -q -m live`.
- **Persona service**: confirm it serves
  `{"snapshot_version": …, "personas": [{persona_id, family, label, features}]}`
  as consumed by `waypoint.worker.load_personas` and shaped like
  `services/api/tests/fixtures/personas.json`, and that persona families give
  real Pros enough threshold-clearing cross-family matches (the 2+1/3+2 rules
  in `tests/test_personas.py` abstain otherwise).
- **Allison's LCM handoff**: confirm the intake accepts the payload in
  `tests/test_handoff.py` (`idempotency_key`, `pro_id`, `org_id`, `winner`,
  `score`, `measurement_plan`, `audience_lineage`), dedupes on
  `idempotency_key`, and that a 2xx response is a durable acceptance.
- **Supabase**: confirm the database accepts the committed Alembic schema:
  `cd services/api && DATABASE_URL=… uv run alembic upgrade head`.
- **Clean-audience schema**: confirm operators supply SQL-suppressed pro IDs
  plus `audience_query`/`audience_run` lineage exactly as `RunCreate` expects
  (`tests/test_persistence.py`); Waypoint does not re-apply DNC logic.

## Deployment

1. Railway: create one API service and one worker service from
   `services/api` (same Dockerfile; worker command
   `python -m waypoint.worker`, see `Procfile`), set all variables above.
   `WORKER_COUNT` is the number of concurrent claim→process loops the single
   worker process runs (each processes one Pro at a time, so N = N Pros in
   parallel). Keep the worker service at **one replica** — Railway replicas
   would multiply against `WORKER_COUNT`. Fleet-wide LLM concurrency stays
   capped at `MAX_IN_FLIGHT_LLM_CALLS` regardless, so setting `WORKER_COUNT`
   above that cap mostly adds loops that poll-wait for a slot while each still
   holds ~3 Postgres connections — size it to `MAX_IN_FLIGHT_LLM_CALLS` and
   mind `max_connections`.
2. Run migrations once: `uv run alembic upgrade head` with the production
   `DATABASE_URL`.
3. Vercel: deploy `apps/web` with `API_BASE_URL` pointing at Railway.
4. Health checks:
   - `curl https://<railway-domain>/health` → `{"status": "ok"}`
   - open the Vercel URL, sign in with `APP_PASSWORD`, confirm `/api/*`
     reaches Railway through the rewrite.
5. Verify startup fails loudly with a missing variable (delete one, redeploy,
   confirm the crash names it) and that no secret appears in logs, health
   responses, or the browser bundle.
6. Kill-switch drill: set `KILL_SWITCH=true` in Railway and restart either
   service — worker startup and every run creation apply it to the shared
   fleet row. Emergency fallback if no deploy is possible:
   `UPDATE fleet_control SET killed = true WHERE id = 1;` against Postgres.

**Known operational limits to accept or schedule:**

- The operator login is one shared password with no rate limiting or logout;
  acceptable behind the rewrite for an internal tool, but decide whether that
  stands for launch.
- `POST /api/runs/{id}/handoff` hands off all ready winners synchronously; a
  very large run could exceed the Vercel proxy timeout. Rows are durable and
  idempotent, so retrying the request is safe — but batch-handoff UX may need
  a queued job later.

## Production evidence

- **Live n8n contract check** — command above under External contracts;
  currently skipped for missing credentials.
- **Live-model 200-Pro capacity run** — the committed gate
  (`cd services/api && uv run pytest tests/test_load.py -q -m load`) proves
  0 duplicate claims, 0 duplicate handoffs, 0 lost checkpoints, 0 hidden
  failures, and no budget overshoot against real Postgres, but with model and
  context fakes (evidence: `docs/verification/launch-report.md`). Before
  claiming production readiness, run one production-shaped day (200 real
  Pros) on deployed Railway workers with real models and record latency,
  tokens, dollars, retries, and 429s alongside the same integrity queries.
- **Live persona snapshot fetch** — start one worker against the real
  `PERSONA_URL` and confirm panel selection succeeds for a real Pro.
- **Live LCM receipt** — one staging handoff producing a durable receipt row
  (`handoffs` table) and a confirmed row in Allison's tool.

## Product review

- Jake/Liann: the org-context brief was extended with **optional** allowlisted
  matching fields (`segment`, `plan`, `tenure_bucket`, `org_size_bucket`,
  `trade_bucket`, `open_ar_band`) because Pro-matched panels and calibration
  cells are impossible with the 5-field minimum in the plan. Same contract
  version, extras still rejected. Confirm the n8n workflow will emit them.
- Confirm the launch metric catalog in
  `services/api/src/waypoint/measurement.py` (5 indicators and their source
  keys) matches what the warehouse can actually report.
- Confirm the persona snapshot source of truth and its regeneration cadence
  (panels pin `snapshot_version` per run).
- Confirm the missing-pro presentation: a pro the n8n flow returns no brief
  for now gets a visible `abstained` outcome (`context missing`) and the run
  finishes `degraded` instead of silently dropping the pro.

## Deferred until after validation

Intentionally out of launch scope per the approved design — not defects:

- Cutover planning for the current workflow.
- Iterable outcome readback and compounding journey memory.
- Observational causal estimation (propensity weighting, AIPW); the live
  `0.05–0.95` propensity clip remains only a documented research starting
  point.

## Final next action

Provision the Railway project and set the `.env.example` variables
(`DATABASE_URL` and `LLM_API_KEY` first) — that single step unblocks
deployment, the live n8n contract check, the live persona fetch, the staging
LCM receipt, and the live-model capacity run.
