# Pathfinder Waypoint — agent orientation

Read this first, then `docs/REPO-MAP.md`. `TODOS.md` is the one backlog — add work there, not in handoffs.

## Where we are (read this before suggesting anything)

We are **testing on V4**. Two Railway environments, two branches:

| Environment | Branch | What it's for |
|---|---|---|
| **Railway staging** | `V4-Improvements` | The test bed: new Context Layer + Context Workbench. We change things here and validate the output. Every push to V4 redeploys staging. |
| **Railway production** | `main` | The stable version, kept as the backup while V4 is validated. |

**The workflow (owner direction, 2026-09-25):** commit directly to `V4-Improvements` in its
dedicated worktree → push to deploy Railway staging → validate the output. Use a PR only when the
team decides V4 is ready to merge into `main`. Until then main is *intentionally* behind V4.

Do **not** propose merging V4 into main, merging main into V4, or "cleaning up" the gap between them.
That merge is the team's call at the end of testing, not a repo-hygiene task.

Other branches: `V2-Improvements` is a frozen docs archive (never merges). Deleted work lives in
`archive/*` tags. The repo, the primary checkout (`~/projects/pathfinder-waypoint-v2`), and the GitHub
repo are named "v2" — that's the product, **not** the `V2-Improvements` branch.

## Vocabulary — "staging" means two things

- **Staging environment** = the Railway deployment `pathfinder-waypoint-v2-staging.up.railway.app` (runs V4).
- **Staging context** = `context_source="staging"`, a per-run choice to build the Pro's brief from the
  Context Workbench pipeline. On V4, real test runs use Staging context; "Standard context" is the fallback.

Always say which one you mean.

## How a run gets context (V4)

- **Staging context:** Waypoint POSTs to `N8N_CONTEXT_URL_WORKBENCH` → n8n flow "Waypoint
  Context_URL_Workbench" (webhook `waypoint/context-v1`) answers 202 and calls back with rows →
  `staging_context.compile_staging_brief()` keeps only values the **active promotion bundle** has a rule
  for. A new Snowflake column reaches the model only if it has a promotion rule (or an explicit bypass
  like the contact `pro_uuid`). `contact_candidates` (carried from the same callback) is a
  sibling bypass, for the `plan` stage; it is also never promoted. (verified: code in this branch)
- **Standard context (fallback):** synchronous POST to `N8N_CONTEXT_URL` → "Waypoint Context_URL"
  (webhook `pathfinder-org-context`) → `n8n.ALLOWED_FIELDS` allowlist.
- `N8N_CONTEXT_URL_STAGING` is dead.

## Rules

- Commit and push V4 changes directly to `V4-Improvements` after local checks. Each push redeploys staging.
- Use a PR for the eventual V4 → `main` merge, not for routine V4 changes. Never push directly to `main`.
- PRs: `gh`, repo `liannbielicki/pathfinder-waypoint-v2` (GitHub, not GitLab).
- Never force-push. Never skip hooks.
- Don't commit to a branch that is checked out in someone else's worktree — put changes on a new branch.
- Backend checks: `cd services/api && .venv/bin/python -m pytest -q`, `ruff check src tests`, `mypy src`.
  **Never run `ruff format`.**
- Comments explain *why*. A `ponytail:` prefix marks a deliberate simplification with a named ceiling.
- Tag every claim you write in docs as verified (with evidence) or unverified. Don't guess which n8n
  flow or branch is live — check (see `docs/REPO-MAP.md` → "How to check").
