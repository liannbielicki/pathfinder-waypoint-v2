# Claude build instructions

Build Pathfinder Waypoint V2 in this repository.

Read, in order:

1. `docs/specs/pathfinder-production-rebuild-design.md`
2. `FRONTEND.md`
3. `docs/plans/pathfinder-waypoint-v2-implementation.md`
4. `docs/OPEN-INPUTS.md`
5. `docs/knowledge/README.md`

Execute the implementation plan task by task using test-first development and the Superpowers workflow. Do not copy the legacy repository tree. Port only the explicitly preserved contracts, prompts, rubric, calibration artifact, scoring behavior, and integration-boundary knowledge in `docs/knowledge/`.

Use fixtures while an external system is unavailable. When a real n8n, persona, LCM, deployment, or authentication contract is required, stop that task and report the exact missing input; never invent a production contract.

Do not begin cutover work, Iterable readback, or causal propensity estimation. Do not declare production readiness until the complete suite and the 200 Pros/day production-shaped load gate pass.
