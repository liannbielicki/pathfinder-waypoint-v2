# Curated Context Evaluation Design

## Goal

Add one obvious **Test curated context** action after compilation so a user can see whether Waypoint produces better ideas with the curated context contract.

## User experience

After compilation, the Workbench shows one primary button: **Test curated context**. The test uses the organization ID already on the page and creates a durable background job. It does not modify a catalog, send outreach, or change production data.

The result shows:

- baseline ideas generated from the full scrubbed experimental source response;
- curated ideas generated from the deterministic packet produced by the approved catalog and selected feature catalog;
- input/output tokens, model latency, and context size for each arm;
- a collapsed view of each exact prompt and context packet; and
- a low-cost AI verdict naming `baseline`, `curated`, or `tie`, with a short reason and no more than three small context improvements.

## Data flow

1. Reuse the existing durable Workbench job endpoint in a new `evaluate` mode.
2. Fetch the selected organization's current experimental n8n/Snowflake data, plus Context Layer API data only when the original run selected it.
3. Apply the existing PII gate before any model call.
4. Build the baseline from the full scrubbed source payload.
5. Validate the saved catalog and deterministically call `compile_context` to build the curated organization packet with exact feature mappings and compact product cards.
6. Generate both arms with `build_evolve_prompt`, which delegates to the same `evolve_prompt` and `EVOLVE_SYSTEM` contract used by Waypoint. Both arms use identical prompt parameters; only the fenced organization context differs.
7. Ask `MODEL_FAST` (falling back to `claude-haiku-4-5`) to compare both inputs, exact prompts, outputs, and measured metrics. The judge returns strict JSON with a winner, reason, and at most three small suggested changes.

## Safety and honesty

- The evaluation is recommendation-only and never calls LCM or Iterable.
- All source values pass through the existing PII scrubber before any AI call.
- Baseline and curated calls use the same prompt contract, model, channels, journey window, and candidate count.
- The UI labels the fixed empty-history and no-historical-evidence values used by the Workbench; it does not claim to replay a full production evolve run.
- A failed arm or judge is shown explicitly and never converted into a favorable verdict.
- Existing compiled and saved results remain intact.

## Testing

- Backend tests prove source collection and PII handling precede both AI calls, the exact Waypoint prompt builder is used for both arms, curated context comes from `compile_context`, metrics are returned, and the judge uses the fast model.
- Frontend tests prove the single button appears only after compilation, starts one evaluation job, disables duplicate clicks, and renders the comparison and judge verdict.
- Existing backend and frontend suites remain green.
