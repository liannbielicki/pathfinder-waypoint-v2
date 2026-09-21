# Context Window Curation Design

## Goal

Improve AI-authored context metadata so Waypoint receives fewer redundant or low-signal metrics while every discovered source variable remains visible for review.

## Policy

- Prefer T28 as the standard recent-behavior window.
- Treat T30 as acceptable when T28 is unavailable or materially different.
- Default T1 and T7 variants to `exclude` because they are usually too noisy or too similar to distinguish durable behavior.
- Default T90 variants to `deprioritize`. Include T90 only when it adds a materially different long-term trend, baseline, or rare high-value signal that T28 cannot provide.
- Compare variables within the same metric family. Include count and amount variants together only when frequency and financial magnitude would independently change the diagnosis or recommendation.
- Otherwise retain the single most decision-useful representative and exclude redundant variants.
- When names and types do not provide enough evidence to select a representative confidently, use `deprioritize` and explain the uncertainty rather than inventing a distinction.

## Implementation

Add one shared prompt-policy constant in `services/api/src/waypoint/workbench_api.py`. Include it in initial batch authoring, single-variable retry, and low-confidence revision prompts. This is prompt-only: no variables are deleted, no deterministic time-window parser is added, and the catalog schema remains unchanged.

## Verification

Regression tests capture each prompt path and assert that the same T28/T1/T7/T90 and metric-family rules are present. Existing Workbench, API, and web suites must remain green.
