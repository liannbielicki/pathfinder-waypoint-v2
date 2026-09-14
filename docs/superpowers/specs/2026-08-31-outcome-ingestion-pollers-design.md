# Outcome ingestion pollers (Iterable + Amplitude, no n8n)

Date: 2026-08-31
Branch: feature/outcome-ingestion-pollers (off V3-Improvements @ 6c58e0b)

## Goal

Waypoint reads send/delivery events from Iterable and return events from
Amplitude directly, feeding the existing (tested) receiving side:
`exposures.register` and `outcomes.ingest`. No n8n anywhere in the path.

## Architecture

Two poller modules, each a `checkpoint_loop`-style timed asyncio loop in
worker.py, gated by `fleet_control.learning_killed`, session-per-tick,
failure = log + retry next tick. Durable per-source cursors in a new
`poll_cursors` table (migration 0011). A missing API key disables that
poller with one startup log line; the worker runs fine with zero keys.

### iterable_source.py (source string "iterable")

- Polls Iterable's export API (`GET /api/export/data.json`, `Api-Key`
  header) for SMS data types since the cursor, bounded to 24h per tick.
- `smsSend` → `ExposureIn`: exposure_id = Iterable messageId (idempotent),
  recommendation_id = the winner resolved from (run_id, pro_id) — LCM batch
  id IS the run id (event dataFields), Iterable userId IS the pro_uuid.
  send_status "confirmed", sent_at = event createdAt.
- Routing: "route-to-pro" only when the event's metadata says so (dataFields
  routing claim); guardrail markers in campaign/template names map to a
  non-evidence value; undeterminable → "" (fails closed).
- `smsDelivered` → TouchOutcomeIn(delivered=True), `smsUnsubscribe` →
  TouchOutcomeIn(unsubscribed=True), keyed by exposure_id = messageId.
- Cursor: `{"since": <iso>}`, advanced to the window end only after a
  successful ingest.

### amplitude_source.py (source string "amplitude")

- Polls Amplitude Export API (`GET /api/2/export?start=..&end=..`, basic
  auth api_key:secret_key; response is a zip of gzipped NDJSON) hour by
  hour, bounded to 24 hourly files per tick, stopping one hour behind now
  (export lag safety).
- Keeps only events whose event_type == settings.AMPLITUDE_RETURN_EVENT
  (default "session_start"), keyed by user_id = pro_uuid.
- For each confirmed exposure of that pro whose 30d window contains the
  event, emits TouchOutcomeIn(exposure_id=<exposure>, source="amplitude",
  first_return_at=<event time>). Earliest event per pro per batch is used;
  `outcomes.ingest` already keeps the min first_return_at per
  (recommendation_id, source), so re-polls are idempotent.
- Horizons stay derived (send + first_return_at); the checkpoint sweep
  resolves negatives. This module never asserts returned_*.

### Cursors

`poll_cursors(source text PK, cursor jsonb, updated_at timestamptz)` —
migration 0011 (down_revision "0010"). First run initializes the cursor to
now − 24h (bounded backfill), never unbounded.

### Settings

ITERABLE_API_KEY (SecretStr | None), AMPLITUDE_API_KEY / AMPLITUDE_SECRET_KEY
(SecretStr | None), AMPLITUDE_RETURN_EVENT (str, "session_start"),
POLL_SECONDS (float, 300). test_settings.py name list + length bound updated
(AMPLITUDE_RETURN_EVENT is 22 chars).

### Call-volume floor

Both sources fetch only once MIN_WINDOW (3h) of NEW window has accumulated;
smaller windows return immediately with zero HTTP calls and the cursor
untouched. This caps external calls at 24/day (Iterable, 8 windows x 3 data
types) + 8/day (Amplitude, one multi-hour export per window) = ~32/day,
independent of POLL_SECONDS. 3h keeps total ingest lag inside the checkpoint
sweep's 6h GRACE, so no horizon can be swept negative before its return
event arrives.

## Error handling

- Malformed API rows: logged and skipped, never crash the tick.
- HTTP failure: tick aborts before cursor advance; next tick retries the
  same window.
- Kill switch: checked per tick (same read as `sweep_if_enabled`).

## Testing (pytest + pytest-httpx, real Postgres fixtures)

Pagination across ticks via cursor; dedup on re-poll; guardrail send →
routing ≠ route-to-pro; missing key disables poller; malformed response →
logged skip; end-to-end mocked Iterable send + Amplitude return → winner
validation_status "validated".

## Assumptions needing confirmation

1. Iterable export dataTypeName values: "smsSend", "smsDelivered",
   "smsUnsubscribe".
2. LCM stamps the batch/run id into the send event's dataFields under
   "run_id" / "batch_id" / "batchId" (checked in that order), and the
   routing claim under dataFields "routing".
3. Guardrailed test sends are identifiable by "guardrail" in the campaign
   or template name when no explicit routing claim exists.
4. Amplitude user_id is exactly the pro_uuid Waypoint stores on exposures.
