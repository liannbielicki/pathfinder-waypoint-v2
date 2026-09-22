# Findings

- The generator reopens `channel=none` even though the shared Channel type already excludes it; handoff omits unknown channels and can therefore inherit downstream SMS.
- Round one currently uses the refine prompt with no champion, only ranker top/tied candidates are persona-screened, and reaction scores lack finite/range/exact-ID validation.
- Persona fit treats a segment-only match as complete coverage and can abstain when the available pool lacks threshold matches; final selection does not prefer unseen personas.
- Outcome evidence counts source rows rather than logical touches, and warm-start similarity can reach 1.0 from one shared segment field.
- Ranking, warm-start, and some model evidence are already persisted but stripped from the API; the smallest UI change is an additive compact summary in WinnerReview.
- Standard context does not currently expose the staged `suggested_channel`; the implementation must use one allowlisted contract and not infer unavailable external data.
- The attached service excerpt contains successful Anthropic `200` responses, not provider errors. Its failure signal is the Standard context client receiving async `202 Accepted` responses.
- The tracked synchronous and asynchronous n8n workflows both claimed `waypoint/context-v1`; a Standard job sent to the async route was immediately requeued and retriggered the expensive flow for every attempt.
- Exceptions outside the per-job `run_job` guard escaped the worker loop; plain `asyncio.gather` then terminated the entire agent process instead of restarting only the failed loop.
- Alembic has one head and the V4 model-tier constraint is represented in ORM metadata. `alembic check` still reports older unrelated metadata drift for timestamps, indexes, and `touch_outcomes.routing`; this work does not expand into that pre-existing migration cleanup.
