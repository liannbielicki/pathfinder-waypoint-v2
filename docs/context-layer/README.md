# Context Layer — pull-only integration notes

**Key:** `CONTEXT_LAYER` in vault (shared).
**PULL / READ-ONLY.** The only endpoint is `GET /api/context_layer/:organization_uuid`.
There is no write/push path — **never push anything to this key or endpoint.**

## Endpoints (from [Confluence API doc](https://housecall.atlassian.net/wiki/spaces/ADMIN/pages/4383408295))
| Env | Base URL |
| --- | --- |
| Staging | `https://staging.internal-success.housecall-internal-dev.com/pro-data` |
| Test | `https://api.internal-success.housecall-internal-test.com/pro-data` |

Auth: `Authorization: Bearer <key>`. 401 on missing/invalid.

## Live-pull status (2026-09-03) — ACCESS CONFIRMED (prod)
**Production base URL:** `https://api.internal-success.housecall-internal.com/pro-data`
(not in the docs — found by DNS pattern: it's the env-suffix-less sibling of the `-test` host).
The key is a **production** key; it returns **HTTP 200** here. Staging + test 401 because the prod
key is invalid there, and the API doc's "Environments" table only lists those two (mislabeled: prose
says "test and production", rows are Staging/Test).

**Resolved — the endpoint needs the `ORG_UUID`.** `pro_...` / numeric `organization_id` return an
empty 200 shell (identity resolution is not wired). Mapping the numeric id to its `ORG_UUID` via
`marts.customer_base.agg_current_customer` and pulling with that returns full data. Verified:
org `920744` -> `cc962bf1-13bb-4eea-bf66-f3adc9e22192` -> 14 features + firmographics (HTTP 200).
See `coverage-report.md`. Map first, then pull:
```
snow sql -c hcp --warehouse wh_consumer -q "select org_uuid from marts.customer_base.agg_current_customer where organization_id = <id>"
vault run bash docs/context-layer/scripts/pull.sh <org_uuid> prod
```

**Architecture (from [SPIKE](https://housecall.atlassian.net/wiki/spaces/ADMIN/pages/4298047673)):**
Context Layer lives in the **pro-data** service (Ruby `ContextLayer::FetchAll` / `ProData::Public::Api`),
with a **housecall-web proxy** (`ContextLayer` component, pro-authenticated on `organization_id`).
Identity resolution accepts `organization_id`, `lead_id`, or `pro_uuid` — so the `pro_...` id should
resolve directly; the Snowflake `ORG_UUID` is a fallback, not a hard requirement.

**Next step:** get the production base URL from the internal success team (doc author: Luiz Vasconcellos),
then run `vault run bash docs/context-layer/scripts/pull.sh <org> ...` (add a `prod` case to the script)
and fill in the coverage report. The pull is a read-only GET — zero write risk.
