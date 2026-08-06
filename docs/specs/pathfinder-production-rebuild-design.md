# Pathfinder Production Rebuild Design

Date: 2026-08-06  
Status: Ready for Jake and Liann's final design review  
Implementation target: production-ready initial release, capable of 200 Pros/day

## UI principles

The frontend is a rebuild, not a port of the legacy 5,660-line HTML file. Its governing frontend file starts with these principles before describing components or flows:

1. **Truth before polish.** Running, waiting, degraded, failed, resumed, stopped, and complete are distinct visible states. Never imply work succeeded when a write or integration failed.
2. **The operator always knows what happens next.** Every screen exposes the current stage, the next allowed action, and why an action is unavailable.
3. **Async by default.** Starting work returns immediately. The UI polls or streams durable state; it never waits behind a long synchronous request.
4. **Evidence stays attached.** Candidates, persona reactions, scores, confidence, costs, decisions, measurement plans, and handoff receipts remain traceable to the run.
5. **No fabricated certainty.** Use real persona labels and provenance. Surface abstention, low panel fit, unavailable context, and no-action as legitimate outcomes.
6. **Safety is visible.** The operator can see audience lineage, guardrail results, kill state, cost state, and the exact handoff boundary. Pathfinder never sends.
7. **Accessible under pressure.** Keyboard access, visible focus, readable contrast, clear error copy, responsive layouts, and touch-safe controls are launch requirements.
8. **Production density without dashboard theater.** Prioritize the run lifecycle and decision evidence; omit decorative metrics and configuration surfaces that do not change an operator decision.

The frontend will be a dedicated Next.js application hosted on Vercel. A concise `FRONTEND.md` in the implementation repository will preserve these principles as its first section, followed by flows, states, components, and behavioral tests.

## Launch scope

The initial production release includes:

- Vercel-hosted operator UI with async run lifecycle, explicit failures, cost, kill, candidate evidence, winner review, and LCM handoff receipts.
- Railway-hosted Python 3.14 API and horizontally scalable worker fleet.
- Supabase/Postgres as the sole durable source of truth for runs, candidates, winners, handoffs, jobs, measurement plans, and fleet control.
- The existing n8n flow as the Snowflake credential, batching, and pre-aggregation boundary.
- One metered LLM gateway with client reuse, retries, backoff, pricing, prompt caching, rate control, and usage persistence.
- One resumable loop with leased queue claims, checkpoints, idempotency, honest stop reasons, and no canned recommendation fallback.
- Pro-matched persona evaluation using a three-person screen and five-person final check.
- Typed proposal-specific measurement plans stored with every winner.
- Connected, receipt-producing handoff to Allison's LCM tool. Pathfinder performs zero sends.
- Measured proof that the system sustains 200 Pros/day before launch.

## System architecture

### Frontend — Vercel

The Next.js frontend uses one operator-oriented flow:

1. Select or upload the supplied clean audience.
2. Validate identifiers and display audience query/run lineage.
3. Start the run and return immediately.
4. Observe queue, worker, candidate, cost, and failure state.
5. Inspect the winning recommendation, matched personas, confidence, rationale, and measurement plan.
6. Create the LCM handoff and display its durable receipt.

The browser uses the public Railway API through a committed Vercel rewrite, avoiding frontend secrets. The UI contains no direct Snowflake, Supabase, model-provider, n8n, or Iterable credentials.

### API and workers — Railway

The Railway service exposes authenticated run, status, kill, evidence, and handoff endpoints. Workers claim jobs with Postgres leases and `SKIP LOCKED`, checkpoint every durable stage, and share fleet-wide kill, rate, and cost controls.

Every worker executes the same contract:

`claim → guard → context → generate → critics → 3-person screen → search → 5-person final check → score/no-action → measurement plan → persist → handoff`

No worker owns unique in-memory truth. A crash or deployment resumes from Postgres without duplicating candidates, charges, or handoffs.

### Context — existing n8n flow

The rebuild reuses the existing n8n/Snowflake flow rather than creating a second warehouse path. The implementation records and verifies its payload contract, batching behavior, allowed fields, PII posture, fact semantics, and production parity.

Raw Snowflake rows remain ephemeral. Only the versioned, allowlisted, condensed brief crosses the AI boundary. Durable storage contains derived run artifacts, never raw warehouse context.

### Durable data

The minimum durable model contains:

- `runs`: lifecycle, audience lineage, configuration version, cost, and stop state.
- `jobs`: lease, attempts, checkpoint, retry state, and worker ownership.
- `candidates`: structured recommendation, critics, persona evidence, score, and cost.
- `winners`: canonical selection or no-action result.
- `measurements`: one or two typed leading indicators, direction, source key, and window.
- `handoffs`: idempotency key, LCM payload, response, and receipt.
- `fleet_control`: kill state, shared budget, rate state, and reservations.

## Persona matching

Matching uses permitted organizational, lifecycle, product-usage, financial-context, and behavioral features. It never uses identity, contact data, or protected traits.

- Three-person screen: the two closest qualifying personas plus the nearest qualifying match from a different persona family or behavioral dimension.
- Five-person final check: the three closest qualifying personas plus two such counterweights.
- Every counterweight must clear the same Pro-fit threshold. It is related to the Pro, not a generic dissenting persona.
- Persona snapshot version, match features, fit score, family, and rationale are persisted.
- If enough qualifying matches do not exist, the system reports low panel fit or abstains. It does not invent representativeness.

## Measurement contract

After selecting the winner, the loop defines the one or two leading indicators that best express the proposal's mechanism. Each indicator must map to a typed contract containing:

- metric key and human-readable label;
- expected direction;
- source system key;
- attribution window;
- proposal mechanism and rationale.

For an invoicing proposal, `invoices_sent` can be a leading indicator. Churn risk remains the primary long-term outcome. The launch build stores the plan but does not connect Iterable readback.

## Audience and sending boundary

The supplied audience is treated as clean because its SQL query already applies DNC and other suppression rules. Pathfinder validates identifiers and preserves query/run lineage but does not duplicate consent or suppression logic.

Allison's LCM tool owns final copy, personalization, and sending. Its Iterable path provides the downstream DNC failsafe. Pathfinder ends after creating an idempotent handoff artifact and durable receipt.

## Production capacity

Two hundred Pros/day is a hard launch gate, not a future aspiration. The initial architecture includes:

- leased queue claims and horizontal workers;
- batch context retrieval;
- shared model clients and connection reuse;
- fleet-wide rate limits, cost reservation, and kill state;
- bounded retries with backoff and idempotency;
- stage-level latency, token, dollar, retry, 429, and external-service telemetry.

A production-shaped load run must sustain 200 Pros/day without double claims, duplicate handoffs, lost checkpoints, budget overshoot, or hidden failures.

## Configuration

Runtime values live in Railway. Names are descriptive and shorter than 20 characters. The initial vocabulary is:

- `DATABASE_URL`
- `LLM_API_KEY`
- `N8N_CONTEXT_URL`
- `N8N_TOKEN`
- `PERSONA_URL`
- `PERSONA_TOKEN`
- `HANDOFF_URL`
- `HANDOFF_TOKEN`
- `RUN_COST_USD`
- `DAY_COST_USD`
- `WORKER_COUNT`
- `KILL_SWITCH`
- `MODEL_FAST`
- `MODEL_DEEP`
- `APP_PASSWORD`
- `SESSION_KEY`
- `LOG_LEVEL`

The frontend contains no secrets. Vercel holds one non-secret routing value, `API_BASE_URL`, so its rewrite can reach the Railway origin; every secret remains in Railway.

## Failure behavior

- Missing configuration prevents startup with a specific error.
- Unavailable context, storage, model, or handoff dependencies produce explicit retryable or terminal states.
- Failed durable writes never become apparent success.
- Queue leases expire safely and permit idempotent recovery.
- Shared kill state stops new claims and prevents the next paid model call.
- Cost reservation occurs atomically before paid work.
- Unsupported persona panels, missing measurement mappings, and weak scores can resolve to abstention or no-action.

## Verification

Launch acceptance requires:

- behavioral UI tests for every lifecycle and failure state;
- contract tests for the existing n8n payload and Snowflake fact semantics;
- property and fixture tests for calibration, scoring, abstention, and no-action;
- tests proving the `2+1` and `3+2` persona compositions and counterweight fit threshold;
- crash/resume, lease expiry, idempotency, and two-worker claim tests;
- side-by-side build fixtures for IDs, payloads, gates, scoring, persistence, and handoff receipts;
- a production-shaped 200 Pros/day load run with recorded latency, tokens, dollars, retries, and 429s.

Side-by-side validation is a build-time correctness tool. Workflow cutover and rollout phasing are explicitly deferred until after the rebuilt system passes its tests.

## Deferred improvements

The following are not launch dependencies:

- Iterable outcome readback and compounding journey memory.
- Observational causal estimation, propensity weighting, AIPW, and statistical claims about realized churn reduction.
- Cutover strategy for the current workflow.

The live `0.05–0.95` propensity clip remains documented only as a research starting point for a future causal study with sufficient readback data, a defined estimand, and a named statistical owner.

## Closed decisions

1. The loop creates one or two proposal-specific leading indicators and stores a typed measurement plan.
2. Persona evaluation uses a three-person screen and five-person final check.
3. The existing n8n/Snowflake flow is reused and contract-tested.
4. Side-by-side parity validates the build; cutover planning happens later.
5. The initial system must prove 200 Pros/day before launch.
6. Pathfinder trusts the SQL-suppressed audience and does not duplicate DNC logic.
7. The operator UI is rebuilt as a dedicated frontend governed by UI principles.
8. The frontend is hosted on Vercel; API and workers run on Railway; runtime variables live on Railway and use short descriptive names.
9. Persona panels are Pro-matched with related, threshold-clearing counterweights.
10. Iterable readback and causal propensity estimation are deferred improvements.
