# Context Workbench handoff

## Goal

The Context Workbench is a local inspection and authoring tool for building a safe, compact AI context layer for Waypoint. It is experimental: catalog drafts are not runtime-approved automatically.

## Branch and workspace

- Repository: `/Users/jakefassora/projects/pathfinder-waypoint-v3-outcome-pollers`
- Branch: `V3-Improvements`
- Do not modify the separate V2 worktree or its unrelated dirty files.
- Do not commit or print secrets. Keep `services/api/.env` local and untracked.

## Start locally

Backend:

```bash
cd /Users/jakefassora/projects/pathfinder-waypoint-v3-outcome-pollers/services/api
set -a
source .env
set +a
PYTHONPATH=src /Users/jakefassora/projects/pathfinder-waypoint-v2/services/api/.venv/bin/python scripts/run_workbench.py
```

Frontend:

```bash
cd /Users/jakefassora/projects/pathfinder-waypoint-v3-outcome-pollers/apps/web
pnpm dev
```

Open `http://localhost:3000/context-workbench`.

## Runtime flow

1. Fetch Context Layer, Snowflake via n8n, or both.
2. Normalize the source payloads.
3. Run the deterministic, app-side PII exclusion gate.
4. Build the machine-oriented context payload.
5. Run baseline/proposed prompts and parse candidate JSON.

Raw provider values are withheld from the trace before the PII gate. Credentials never enter the browser request.

## Authoring flow

Authoring uses scrubbed variable inventory plus the optional feature catalog CSV. It produces only machine-oriented catalog metadata:

```text
key
canonical_key
value_category
related_features
usefulness_rank
aggregate_prompt
```

`related_features` must contain only exact feature keys from the uploaded catalog. `aggregate_prompt` is generated only for high-usefulness variables and requests cohort-level statistics across comparable Pros; it must not ask Claude to calculate an individual Pro’s position.

The authoring loop requeues missing keys when Claude returns a partial batch. Progress is calculated against the full scrubbed inventory.

## Catalog versions

- Select **Fresh variable inventory** to ignore saved entries.
- Select a named version to resume from its saved entries.
- Selecting a saved version restores the prompt that created it.
- Saving stores the name, prompt, entries, created timestamp, and updated timestamp in browser local storage.
- Runtime uses the selected saved version after it is saved; drafts remain experimental until deliberately promoted.

## Design rules

- Prefer compact key/value structures over narrative prose at runtime.
- Do not guess time windows or feature relationships from opaque names.
- Keep deterministic transformations, feature mappings, eligibility, consent, PII removal, and statistics outside the LLM.
- Use the LLM for interpreting approved evidence and generating hypotheses, not for inventing facts or product relationships.
- Unknown, missing, stale, and conflicting values must remain distinguishable.

## Verification

```bash
cd /Users/jakefassora/projects/pathfinder-waypoint-v3-outcome-pollers/services/api
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/jakefassora/projects/pathfinder-waypoint-v2/services/api/.venv/bin/python -m pytest -p no:cacheprovider tests/test_workbench.py -q

cd /Users/jakefassora/projects/pathfinder-waypoint-v3-outcome-pollers/apps/web
pnpm lint
pnpm test --run
```

Do not treat passing tests as proof that external Context Layer, n8n, Snowflake, or Anthropic credentials are configured correctly; those require a live, non-secret-bearing smoke test.
