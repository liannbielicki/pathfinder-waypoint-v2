# Pathfinder Frontend

## UI principles

1. **Truth before polish.** Running, waiting, degraded, failed, resumed, stopped, and complete are distinct visible states. Never imply work succeeded when a write or integration failed.
2. **The operator always knows what happens next.** Every screen exposes the current stage, the next allowed action, and why an action is unavailable.
3. **Async by default.** Starting work returns immediately. The UI polls or streams durable state; it never waits behind a long synchronous request.
4. **Evidence stays attached.** Candidates, persona reactions, scores, confidence, costs, decisions, measurement plans, and handoff receipts remain traceable to the run.
5. **No fabricated certainty.** Use real persona labels and provenance. Surface abstention, low panel fit, unavailable context, and no-action as legitimate outcomes.
6. **Safety is visible.** The operator can see audience lineage, guardrail results, kill state, cost state, and the exact handoff boundary. Pathfinder never sends.
7. **Accessible under pressure.** Keyboard access, visible focus, readable contrast, clear error copy, responsive layouts, and touch-safe controls are launch requirements.
8. **Production density without dashboard theater.** Prioritize the run lifecycle and decision evidence; omit decorative metrics and configuration surfaces that do not change an operator decision.

## Operator flow

Login → supplied audience lineage → start → observe → inspect winner or no-action → create LCM handoff → receipt.

## Required states

Queued, running, waiting, degraded, failed, resumed, stopped, complete, abstained, and no-action.

The detailed frontend scope and behavioral requirements live in `docs/specs/pathfinder-production-rebuild-design.md` and Task 10 of `docs/plans/pathfinder-waypoint-v2-implementation.md`.
