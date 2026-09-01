# Plan: outcome ingestion pollers

Spec: docs/superpowers/specs/2026-08-31-outcome-ingestion-pollers-design.md

1. tables.py: add `PollCursorRow` (source PK, cursor JSONB, updated_at).
2. alembic/versions/0011_poll_cursors.py: create poll_cursors.
3. settings.py: ITERABLE_API_KEY, AMPLITUDE_API_KEY, AMPLITUDE_SECRET_KEY,
   AMPLITUDE_RETURN_EVENT, POLL_SECONDS. Update tests/test_settings.py.
4. src/waypoint/cursors.py: load_cursor/save_cursor helpers (shared).
5. src/waypoint/iterable_source.py: fetch smsSend/smsDelivered/smsUnsubscribe
   since cursor (24h cap), winner resolution by (run_id, pro_id), routing
   fail-closed mapping, register exposures + ingest outcomes,
   poll_if_enabled gate.
6. src/waypoint/amplitude_source.py: hourly export fetch (24-file cap, 1h
   lag), zip/gzip NDJSON parse, return-event filter, exposure-window match,
   ingest outcomes, poll_if_enabled gate.
7. worker.py: build clients when keys present (one startup log line when
   disabled); add iterable/amplitude loops alongside checkpoint_loop.
8. tests/test_pollers.py: cursor pagination, re-poll dedup, guardrail
   routing, missing-key disable, malformed-row skip, e2e validated winner.
9. Run `uv run pytest -m "not live"` (deselect known-failing load test).
