# Pathfinder Waypoint V2

Clean implementation repository for the Pathfinder production rebuild.

Start here:

1. `docs/specs/pathfinder-production-rebuild-design.md` — approved design
2. `FRONTEND.md` — UI principles, flows, states
3. `docs/plans/pathfinder-waypoint-v2-implementation.md` — implementation plan
4. `docs/environment.md` — environment ownership and setup

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
# Worker process: python -m waypoint.worker   (scale to WORKER_COUNT replicas)
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

The legacy repository and audit branch are reference-only. Do not copy their application structure into this repository.
