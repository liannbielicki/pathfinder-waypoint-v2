# Repo map

Durable orientation for anyone (human or AI) opening this repo. Kept identical on `main` and
`V4-Improvements`. **Last verified: 2026-09-24.** Update it in the same commit as any change it describes.

For the dated audit behind this map — evidence and the executed cleanup — read
`docs/2026-09-24-repo-audit-handoff.md`. Channel-selection findings live on branch
`feature/channel-selection` (`docs/2026-09-24-channel-selection-analysis.md`).

---

## Branches

| Branch | Status | Notes |
|---|---|---|
| `V4-Improvements` | **active, deployed** | Railway staging deploys it. ~47 commits ahead of main. |
| `main` | default, behind | GitHub default. Lacks V4's workbench/staging context. |
| `V2-Improvements` | frozen archive | Final docs push 2026-09-24. Never merges. See `ARCHIVED.md` there. |
| `origin/feat/feature-catalog-v2` | Liann's | Catalog work not on main or V4. Not ours to change. |
| `origin/V3-Improvements` | merged, stale | Fully contained in main + V4. Kept on the remote by choice. |

**Known conflict:** channel selection was built twice, both by Liann — on V4 (`ebb2b86` and follow-ups,
~Sep 15) and on main (`fb92bba`, Sep 22). Merging main into V4 will conflict in `RunStart.tsx`, `feasibility.py`,
`handoff.py`, `models.py`, `prompts.py`. Default resolution: keep V4's behavior.

### Archive tags (deleted branches — restore with `git checkout -b <name> <tag>`)

| Tag | What |
|---|---|
| `archive/v5-concept-quality-spike` | v5 spike: deterministic concept-quality gate before the persona panel |
| `archive/outcome-ingestion-campaign-scope` | Iterable export scoped by campaign, for the now-inactive n8n outcome flow |
| `archive/stash-codex-v4-workbench-2026-09-16` | an old V4 workbench stash; `git stash apply <tag>` |
| `v3-learning-loop` | V3 learning loop, merged into main |

### Worktrees

Primary checkout `~/projects/pathfinder-waypoint-v2` stays on **`main`**. Feature work goes in
worktrees under `.claude/worktrees/`, each on its own branch. Git won't check out one branch in two
worktrees, so the primary can't sit on V4 while a worktree does. Remove a worktree when its branch
merges.

---

## Runtime shape (V4)

- **API** (`services/api`, FastAPI) and **worker** (same image) on Railway; Postgres (Supabase);
  frontend `apps/web` (Next.js) on Vercel, which proxies `/api/*` to Railway.
- A **run** = a list of org IDs + a journey window + channels + a `context_source`. The worker, per Pro:
  fetches context → gates feasibility/consent → runs the evolve loop (generate ideas, critic, persona
  panel scoring) → ranks → war-games a follow-up → hands the winner to LCM (email/SMS) or the calls
  panel (call).
- Outcomes come back through in-app pollers (`amplitude_source.py`, `iterable_source.py`,
  `outcomes.py`) and feed `evidence.py`.

### Context paths

| | Staging context (**real runs**) | Standard context (fallback) |
|---|---|---|
| Env var | `N8N_CONTEXT_URL_WORKBENCH` (+ `CONTEXT_LAYER_*`) | `N8N_CONTEXT_URL` |
| n8n flow | "Waypoint Context_URL_Workbench", webhook `waypoint/context-v1` | "Waypoint Context_URL", webhook `pathfinder-org-context` |
| Shape | async: 202, then callback to `/api/context/staging/callback` | sync: 200 + JSON array |
| Filter | active **promotion bundle** (Postgres): rule = `source_key → canonical_key`, lineage-checked; unmatched values dropped silently | closed `ALLOWED_FIELDS` allowlist in `n8n.py` |
| Code | `staging_context.py`, `pipeline.py` (`context_source == "staging"`) | `n8n.py` `N8NContextClient` |

The run form defaults to Standard (`RunStart.tsx`) even though Staging is the real path.

### n8n flows (live instance; repo copies in `n8n/` on V4)

| Live workflow (id) | Active | Repo copy |
|---|---|---|
| Waypoint Context_URL_Workbench (`uagiNmDAitRAHHRv`) | yes | `n8n/waypoint-variable-audit-context-async-v1.json` — webhook path drifted (repo says `context-async-v1`) |
| Waypoint Context_URL (`64Cuwv6v20ANg5Yo`) | yes | `n8n/waypoint-context-snowflake-v1.json` — `Merge` node drifted |
| Waypoint - Snowflake pull (`hen9aPWGv5lC2FqE`, `pathfinder-audience-boundary`) | yes | none; no known caller |
| waypoint-morning-batch (`ibHXuZUPOA4zZxnh`) | no | none; holds Salesforce DNC SQL + LCM's Iterable SMS eligibility rule |
| Pathfinder Waypoint — outcome ingestion (`OWgWr0nsAza0iFPo`) | no | `docs/n8n/outcome-ingestion.workflow.json`; superseded by in-app pollers |
| Waypoint_Context_URL_Staging (`sZUPpk7F69pRits9`) | no | none; dead |

`n8n/waypoint-variable-audit-context-v1.json` has no live counterpart (dead copy).

---

## How to check (don't guess)

- **Which branch is deployed:** Railway → staging service → source branch (`V4-Improvements` as of 2026-09-24).
- **Live n8n flows:** `GET $N8N_URL/api/v1/workflows` with header `X-N8N-API-KEY: $N8N`
  (keys in Jake's vault; run as `vault run <cmd>`; never print them). The instance has ~300 workflows
  from many teams — filter by name.
- **Snowflake:** `snow sql -c hcp -q "<sql>"` (Okta SSO in the browser).
- **Branch containment:** `git branch -a --merged origin/main`, `git cherry -v <upstream> <branch>`.

---

## Docs layout

- `CLAUDE.md` + this file: orientation. Keep both short and current.
- `TODOS.md`: **the one backlog**, prioritized. Handoffs and plans link to it rather than keeping their own lists.
- `docs/2026-09-24-repo-audit-handoff.md`: dated audit, evidence, and the executed cleanup.
- `docs/superpowers/{specs,plans}/`: dated design/plan pairs. **Some are superseded** — e.g. the
  2026-09-17 per-run-context-source and railway-context-workbench specs still describe
  `N8N_CONTEXT_URL_STAGING`. Check the audit handoff §5.2 before trusting one.
- `docs/HUMAN-TASKS.md`: env var list is current; its "Launch status" section is stale.
