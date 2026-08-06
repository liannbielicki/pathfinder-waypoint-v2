# Environment

Use Python 3.14, Node.js 24 LTS, pnpm 11.4, and uv. These were the current stable production lines verified on 2026-08-06 from [Python releases](https://www.python.org/downloads/), [Node.js releases](https://nodejs.org/en/about/previous-releases), and [pnpm releases](https://github.com/pnpm/pnpm/releases). Copy `.env.example` to `.env` for local development; never commit `.env` or real credentials.

Railway owns every API and worker value in `.env.example` except `API_BASE_URL`. Vercel receives only `API_BASE_URL`, which points its `/api/*` rewrite at Railway and is not secret. Railway supplies `PORT` automatically.

Generate `SESSION_KEY` with `openssl rand -hex 32`. Replace every `replace-me` value before a live deployment. Keep all variable names unchanged: they are descriptive and shorter than 20 characters.

Before launch, verify startup fails clearly when a required value is missing and that logs, health responses, browser bundles, and Vercel contain no secret values.
