# Context Layer coverage report — how much can we actually pull?

**Question:** does a pull return *all* features for an org, or only a top few?

## Live status — MEASURED (prod, 2026-09-03)
Answer: **all related features, not a top-N.** Verified against a real org.

```
vault run bash docs/context-layer/scripts/pull.sh cc962bf1-13bb-4eea-bf66-f3adc9e22192 prod
```
Org `920744` (uuid `cc962bf1-13bb-4eea-bf66-f3adc9e22192`) returned HTTP 200 with **14 features** and
ranks running up to **25** — i.e. the full related set, not a shortlist. **The endpoint needs the
`ORG_UUID`, not the numeric `organization_id` / `pro_` id** (those return an empty shell; identity
resolution is not wired). Map via `marts.customer_base.agg_current_customer` (`organization_id -> org_uuid`).

### Deltas from the documented contract (observed)
- **Extra field:** each feature carries an undocumented `display_name` (e.g. `"Scheduling"`).
- **`adopted` / `churned` / `days_since_last_use`:** **absent entirely** (not even null) — the doc's
  TODO fields are not populated yet.
- **Firmographics are sparse:** this org had only `industry`, `segment`, `company_size_range`; the
  transcript-derived fields (`budget_range`, `company_goal`, `current_tools`, `tech_readiness`,
  `pain_points`) were null — expected when the org has no processed call transcripts.
- **`churn` top-level field:** not present in this response (doc example showed `null`).

## Per the documented contract
`GET /api/context_layer/:org` returns **all data available for the org in one payload** — there is
no pagination, no `limit`, no `top_n` parameter in the doc. So the endpoint's *intent* is **full
dump, not top-three.**

- **`firmographics`** — flat object, up to 8 fields. Any field with no data is `null`. So coverage
  per org = however many of the 8 have been populated by Salesforce/HCW/transcripts.
- **`features[]`** — a list, **one entry per pro↔feature relation** (feature is used, was discussed
  on a call, or DS recommends it). The doc's rule: a feature appears "whenever there is at least one
  relation … and at least one attribute besides `name` is present." **No documented cap** → expect
  the full set of related features, which will vary org to org (could be 2, could be dozens).

## Observed vs. doc
| Check | Doc says | Observed (org 920744) |
| --- | --- | --- |
| `firmographics` fields present (of 8) | up to 8, nulls for missing | 3 of 8 (industry, segment, company_size_range) |
| `features[]` count | uncapped, all relations | **14** (not top-N) |
| `features[]` ranks | ranks are relative | sparse, up to 25 (full reco universe) |
| `adopted`/`churned`/`days_since_last_use` | **TODO in doc** | **absent entirely** — not populated |
| `display_name` per feature | not documented | **present** (undocumented) |
| `churn` top-level field | null in doc example | not present in response |

## Answer
**All related features, not top three.** The contract is a full snapshot; a pull only looks "small"
when the org itself has few related features. This org returned 14. Firmographic richness varies per
org and depends on whether Salesforce/HCW/transcripts have been processed.
