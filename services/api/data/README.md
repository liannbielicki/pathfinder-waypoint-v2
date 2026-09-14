# HCP feature catalog

`hcp_feature_catalog.csv` is the one file that tells Waypoint what an HCP feature is, why a Pro would want it, what it relates to, and where to send them.

Two readers, no code change needed to keep either working as long as the first seven columns stay in this order:

- **Runtime** (`src/waypoint/catalog.py`) reads `feature`, `description`, and `works_on`. It emits one line per feature the Pro's brief references and trims `description` to its first sentence.
- **Context workbench authoring lab** reads the whole file as text so Claude can name real feature keys in `related_features` when drafting the variable catalog.

## Columns

Per-feature columns are filled only on the first row of a feature. CTA continuation rows leave them blank. List columns use `;` with no spaces.

| Column | Meaning |
| --- | --- |
| `feature` | Primary key. Matches the brief's `feature_<key>_state` field name for the features the brief carries. Never rename. |
| `description` | One or two sentences. First sentence says what it does; second names the closest thing it is not. Runtime uses the first sentence only. |
| `cta_id` | Optional stable id for the link row. |
| `label` | Link text a Pro would see. |
| `url` | Destination. Pro-facing pages only. |
| `works_on` | `web`, `ios`, or `mobile` when QA confirmed the link renders there. `broken`, `no_cta`, `not_applicable` are sentinels, not channels. |
| `notes` | QA history for the link row. |
| `display_name` | Product name as HCP shows it. |
| `aliases` | Other names for the same feature: OCL slug, FAC key, step slug, the brief field name, sub-feature keys. Must not equal any primary key. |
| `why_it_matters` | One sentence, under 30 words, stating the business outcome for the Pro. Sourced, never invented. Blank when no source exists. |
| `related_features` | Up to three primary keys from this file. |
| `plans` | Plan names verbatim from the OCL sheet's `PLAN_NAMES`. `Add-on` or `Non Core SaaS` for paid extras. Blank when unknown. |

## Inclusion rule

A feature gets a row when all of these hold:

1. It backs a `feature_<key>_state` field in the brief, or it is live in the OCL registry, or it is Tier 1 or 2 in the OCL prioritization tab.
2. A Pro can adopt or activate it because of an outreach message.
3. It is not already covered by an existing row. Sub-features go in `aliases`.

Core objects every Pro touches (`jobs`, `customers`, `employees`) are included because the brief carries activity bands for them and the authoring lab needs a key to map those variables to.

Nothing here about permissions toggles (`rbac_*`), nav promotions (`promote_*`), internal tooling, or deprecated keys.

## Adding a row

1. Pick the key. Use the OCL or FAC key unless the brief already names the feature differently.
2. Copy `display_name`, `plans`, and every other known name into `aliases` from the OCL sheet.
3. Write `description` and `why_it_matters` from the sheet's value statement or master-list description. Cite the source cell in the commit message.
4. Run `PYTHONPATH=src python -m pytest tests/test_catalog.py`. The tests pin the header, reject unknown `related_features`, reject alias collisions, and reject em dashes.

## Known gaps

- **No QA'd links for the eight newest features.** `price_book`, `csr_phone`, `estimates`, `marketing_health_score`, `job_costing`, `jobs`, `customers`, and `employees` ship as `no_cta`. Someone with a Pro login should add a URL row for each, following the `online_booking` pattern of a primary row plus confirmed replacement.
- **`why_it_matters` is blank for `invoicing`, `payment_processing`, and `instapay`.** The OCL sheet has no value statement for them. The `online_payments` entry describes a navigation toggle, not a benefit.
- **`hcp_assist` was previously described as an AI drafting tool.** It is the 24/7 human call-answering service. CSR AI is `csr_phone`. If `feature_hcp_assist_state` in Snowflake actually tracks CSR AI, move the brief field name from `hcp_assist` aliases to `csr_phone` aliases.
- **n8n display names are unverified.** Aliases come from the brief fields, OCL slugs, and sheet display names. If the live n8n flow emits a different string for a feature, add it to `aliases`.
- **`works_on` is link QA, not capability.** It says where a URL rendered, not whether the feature works on that surface. Do not read it as app-vs-web capability.

## Sources

- OCL readiness and prioritization sheet: `https://docs.google.com/spreadsheets/d/1nGIxPJF6SEvuF6nnO8y6mjPtX_lIyOeKhb5V74QZRvs`
- Research on dynamic sourcing: `docs/research/2026-09-02-hcp-feature-context-dynamic-sourcing.md`
- Plan for this version: `docs/superpowers/plans/2026-09-14-feature-catalog-v2.md`
