# Pathfinder Waypoint — agent orientation

Read this first, then `docs/REPO-MAP.md`. `TODOS.md` is the one backlog — add work there, not in
handoffs. Keep all three files identical on `main` and `V4-Improvements`.

## Where the code is

| Branch | What it is |
|---|---|
| `V4-Improvements` | **The live code.** Railway staging deploys it; every push redeploys staging. Work here (in a worktree, on a branch). |
| `main` | GitHub default branch. **Behind V4** — no Context Workbench, no Staging context, no `n8n/` folder. The primary checkout sits here. |
| `V2-Improvements` | Frozen docs archive. Never merges to main. See its `ARCHIVED.md`. |

Unmerged work that was deleted is kept as `archive/*` tags. The repo, the primary checkout directory
(`~/projects/pathfinder-waypoint-v2`), and the GitHub repo are all named "v2" — that is the product name,
**not** the `V2-Improvements` branch.

## Vocabulary — "staging" means two things

- **Staging environment** = the Railway deployment `pathfinder-waypoint-v2-staging.up.railway.app`.
- **Staging context** = `context_source="staging"`, a per-run choice to build the Pro's brief from the
  Context Workbench pipeline. **Real runs use Staging context.** "Standard context" is the fallback.

Always say which one you mean.

## How a run gets context (V4)

- **Staging (real runs):** Waypoint POSTs to `N8N_CONTEXT_URL_WORKBENCH` → n8n flow "Waypoint
  Context_URL_Workbench" (webhook `waypoint/context-v1`) answers 202 and calls back with rows →
  `staging_context.compile_staging_brief()` keeps only values the **active promotion bundle** has a rule
  for. A new Snowflake column reaches the model only if it has a promotion rule (or an explicit bypass
  like the contact `pro_uuid`).
- **Standard (fallback):** synchronous POST to `N8N_CONTEXT_URL` → "Waypoint Context_URL" (webhook
  `pathfinder-org-context`) → `n8n.ALLOWED_FIELDS` allowlist.
- `N8N_CONTEXT_URL_STAGING` is dead.

## Rules

- Never push to `main`; open a PR (`gh`, repo `liannbielicki/pathfinder-waypoint-v2` — GitHub, not GitLab).
- Never force-push. Never skip hooks. Ask before pushing `V4-Improvements` (it redeploys staging).
- Don't commit to a branch that is checked out in someone else's worktree — put changes on a new branch.
- Backend checks: `cd services/api && .venv/bin/python -m pytest -q`, `ruff check src tests`, `mypy src`.
  **Never run `ruff format`.**
- Comments explain *why*. A `ponytail:` prefix marks a deliberate simplification with a named ceiling.
- Tag every claim you write in docs as verified (with evidence) or unverified. Don't guess which n8n
  flow or branch is live — check (see `docs/REPO-MAP.md` → "How to check").
