# Shared Workbench Catalogs Design

## Goal

Make immutable Context Workbench context and feature catalog versions shared,
durable Postgres records, and make Waypoint identify the exact active Staging
context with a human-readable date and time down to seconds.

## Current failure

Railway persists Workbench jobs and promotions in Postgres, but the web app stores
catalog versions in browser `localStorage`. A different browser therefore sees the
completed jobs in Railway but cannot list or reopen their catalog versions. The
Waypoint run form receives only `staging_context_available`, so it cannot identify
the active promotion, context catalog, feature catalog, or timestamp.

## Durable catalog model

Add `workbench_catalog_versions` with an immutable string ID, `kind` (`context` or
`feature`), display name, sanitized entries JSON, metadata JSON, and server
`created_at`. One table keeps the two version types under the same immutable-store
rules without duplicating persistence code. A conflicting write to an existing ID
fails; an identical write is idempotent.

The Alembic migration backfills distinct versioned context and feature catalogs
from sanitized `workbench_jobs.request` snapshots. Job `updated_at` is used when a
historical browser timestamp is unavailable or invalid. Browser-only versions
that were never submitted to a Workbench job cannot be recovered by the server;
the UI uploads any such local versions once when it first loads after deployment.

## API and UI

Authenticated catalog endpoints list versions, read one version, and create an
immutable version. Feature CSV validation persists the resulting feature version.
Saving an immutable context catalog persists it before selecting it. `localStorage`
keeps selected IDs and serves only as a one-time migration source, never as the
durable catalog source.

An authenticated active-promotion endpoint returns only safe metadata: promotion
ID, context catalog ID/name, feature catalog ID/name, included-variable count, and
the promotion timestamp. The Waypoint Staging option displays this summary using
the browser's timezone with year, month, day, hour, minute, second, and timezone.
If configuration exists but no active promotion exists, Staging is shown as not
ready rather than implying an unspecified context is available.

The Workbench lists shared Postgres catalog versions in its existing prior-catalog
selector. Opening an older version is read-only and never activates it. Promotion
remains an explicit final action.

## Boundaries

- No n8n workflow or Snowflake SQL changes.
- No raw organization values are stored in catalog versions.
- Existing PII sanitization remains the write boundary.
- No catalog is activated merely by saving or opening it.
- Existing active promotions continue working through backfilled IDs and metadata
  fallbacks.

## Verification

Backend tests cover migration/backfill, immutability, authenticated catalog APIs,
active-promotion metadata, and absence behavior. Frontend tests cover shared
catalog loading, one-time local upload, opening prior catalogs, and second-precise
active Staging display. Full backend and frontend checks run before handoff.
