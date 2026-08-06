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

The legacy repository and audit branch are reference-only. Do not copy their application structure into this repository.
