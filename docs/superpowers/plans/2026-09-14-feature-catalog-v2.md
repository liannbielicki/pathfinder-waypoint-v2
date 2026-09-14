# Feature Catalog v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 26-feature `hcp_feature_catalog.csv` with a shorter, verified, alias-aware catalog that Jake can upload to the context workbench authoring lab and that the V3 runtime keeps loading unchanged.

**Architecture:** Same CSV, same path, same seven columns in the same order so `catalog.py` and `workbench.load_catalog` need no code change. Five columns appended on the right. Two non-feature rows dropped, two wrong descriptions fixed, five features added. One test module extended so the file cannot drift silently.

**Tech Stack:** CSV, Python stdlib `csv`, pytest. No new dependencies.

**Spec:** This plan. Requirements come from the 2026-09-10 Jake/Liann check-in transcript, the OCL readiness sheet (`1nGIxPJF6SEvuF6nnO8y6mjPtX_lIyOeKhb5V74QZRvs`), and `docs/research/2026-09-02-hcp-feature-context-dynamic-sourcing.md`.

## Global Constraints

- File stays at `services/api/data/hcp_feature_catalog.csv`. Do not rename or move it.
- Base branch is `V4-Improvements` (the workbench moved there on 2026-09-14; PR #6 merged the V3 learning loop to `main` without it). The catalog CSV, `catalog.py`, and `test_catalog.py` are identical on `main` and `V4-Improvements`, so this PR will also merge to `main` cleanly.
- Existing columns stay in this exact order: `feature,description,cta_id,label,url,works_on,notes`. New columns append after `notes`: `display_name,aliases,why_it_matters,related_features,plans`.
- Existing primary keys do not change. The brief's `feature_<key>_state` fields resolve by exact match on `feature`.
- Multi-row convention holds: per-feature columns are filled only on the first row of a feature. CTA continuation rows leave them blank.
- List columns (`aliases`, `related_features`, `plans`) use `;` as separator, no spaces around it.
- `related_features` values must be primary keys present in the file, at most three.
- `why_it_matters` must trace to a named source: the sheet's value statement, the sheet's master-list description, or the existing CSV description. If no source exists, leave blank. Never invent.
- No em dashes anywhere in the file. Plain hyphens only. No marketing adjectives.
- New features that have no QA'd URL get one row with `works_on=no_cta`, empty `cta_id`, `label`, `url`.
- Runtime is Python 3.12 in `services/api/.venv`. Run tests with `PYTHONPATH=src .venv/bin/python -m pytest`.

## Decisions made so the executor does not have to

| Question | Decision | Why |
| --- | --- | --- |
| Rename Waypoint keys to OCL keys? | No. Keep keys, put OCL/FAC key in `aliases`. | Renaming touches the brief contract and every recorded run. Aliases solve the lookup problem with zero code. |
| Keep `top_unused_paid_feature` and `generic_fallback` rows? | Drop both. | Neither is a feature. Nothing outside `catalog.py` reads `cta_id`, and `catalog.py` only resolves keys that appear in the brief. They are noise in the authoring prompt. |
| Add adoption rules (Attached/Activated/Engaged)? | No. | Nothing consumes them. Jake asked for label, why it matters, related features. |
| Add `plans`? | Yes. | Verbatim from the sheet, zero effort, and the research doc's biggest named gap. |
| Which features to add? | `price_book`, `csr_phone`, `estimates`, `marketing_health_score`, `job_costing`. | Each is either live in OCL or Tier 1-2 in the prioritization tab, is adoptable by an existing Pro via outreach, and is not already a row. |
| Which to cut? | Only the two non-feature rows. | Every other existing row either backs a brief field or is a plausible recommendation target. Cutting further would need outcome evidence we do not have. |
| Separate slim CSV for the authoring lab? | No. One file. | Jake's uploader takes any CSV. Two files drift. |

## Open items for Liann (cannot be resolved from the repo)

1. **CTA URLs for the five new features.** I cannot click links. They ship as `no_cta`. Someone with a Pro login should add a QA'd URL row for each, following the `online_booking` pattern.
2. **`hcp_assist` in the brief.** The current CSV describes it as an AI drafting feature. The sheet's master list says HCP Assist is a 24/7 live call-answering service with human agents, and CSR AI Phone is the separate AI product (`csr_phone`). This plan rewrites the description to match the master list. If `feature_hcp_assist_state` in Snowflake actually tracks CSR AI, the alias should move.
3. **n8n display names.** Jake said display names come from the n8n flow. The repo export is known stale. Aliases here come from the brief fields, OCL slugs, and sheet display names. If n8n emits a different string, add it to `aliases`.

---

### Task 1: Branch and failing tests

**Files:**
- Modify: `services/api/tests/test_catalog.py`

**Interfaces:**
- Consumes: `waypoint.catalog.CATALOG_PATH`, `waypoint.n8n.OrgBrief`
- Produces: five tests that Tasks 2 through 4 make pass

- [ ] **Step 1: Branch off V4-Improvements**

```bash
cd /Users/liannbielicki/pathfinder-waypoint-v2
git checkout V4-Improvements && git pull --ff-only
git checkout -b feat/feature-catalog-v2
```

- [ ] **Step 2: Append the tests**

Append to `services/api/tests/test_catalog.py`:

```python
import csv

from waypoint.catalog import CATALOG_PATH

NEW_COLUMNS = ["display_name", "aliases", "why_it_matters", "related_features", "plans"]


def _rows() -> list[dict[str, str]]:
    with CATALOG_PATH.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _first_rows() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in _rows():
        out.setdefault(row["feature"], row)
    return out


def test_catalog_has_new_columns_in_order():
    with CATALOG_PATH.open(newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    assert header == ["feature", "description", "cta_id", "label", "url", "works_on", "notes", *NEW_COLUMNS]


def test_every_brief_feature_state_resolves():
    keys = {
        name[len("feature_") : -len("_state")]
        for name in OrgBrief.model_fields
        if name.startswith("feature_") and name.endswith("_state")
    } | {"wisetack"}
    missing = keys - set(CATALOG)
    assert not missing, f"brief features absent from catalog: {sorted(missing)}"


def test_related_features_are_closed_set():
    keys = set(_first_rows())
    for feature, row in _first_rows().items():
        related = [r for r in row["related_features"].split(";") if r]
        assert len(related) <= 3, feature
        assert set(related) <= keys, f"{feature}: {set(related) - keys}"
        assert feature not in related, feature


def test_aliases_do_not_collide_with_keys():
    first = _first_rows()
    keys = set(first)
    seen: dict[str, str] = {}
    for feature, row in first.items():
        for alias in filter(None, row["aliases"].split(";")):
            assert alias not in keys, f"{feature}: alias {alias} is also a primary key"
            assert alias not in seen, f"alias {alias} on both {seen[alias]} and {feature}"
            seen[alias] = feature


def test_no_non_feature_rows_and_no_em_dashes():
    first = _first_rows()
    assert "top_unused_paid_feature" not in first
    assert "generic_fallback" not in first
    for row in _rows():
        for column in ("description", "why_it_matters", "display_name"):
            assert "—" not in row[column], f"em dash in {row['feature']}.{column}"
```

- [ ] **Step 3: Run and confirm they fail**

```bash
cd services/api && PYTHONPATH=src .venv/bin/python -m pytest tests/test_catalog.py -q
```

Expected: `test_catalog_has_new_columns_in_order` fails on header, `test_related_features_are_closed_set` and `test_aliases_do_not_collide_with_keys` fail with `KeyError`, `test_no_non_feature_rows_and_no_em_dashes` fails on `top_unused_paid_feature`. `test_every_brief_feature_state_resolves` passes already.

- [ ] **Step 4: Commit**

```bash
git add services/api/tests/test_catalog.py
git commit -m "test: catalog v2 shape, closed related_features, alias uniqueness"
```

---

### Task 2: Schema, cuts, and description fixes on existing rows

**Files:**
- Modify: `services/api/data/hcp_feature_catalog.csv`

**Interfaces:**
- Produces: 24 features, 12 columns, every existing row carrying `display_name`, `aliases`, `plans`; `why_it_matters` and `related_features` still blank (Task 3)

- [ ] **Step 1: Add the five header columns and blank cells on every row**

```bash
cd services/api && .venv/bin/python - <<'EOF'
import csv
from pathlib import Path
p = Path("data/hcp_feature_catalog.csv")
rows = list(csv.DictReader(p.open(newline="", encoding="utf-8")))
new = ["display_name", "aliases", "why_it_matters", "related_features", "plans"]
fields = ["feature", "description", "cta_id", "label", "url", "works_on", "notes", *new]
rows = [r for r in rows if r["feature"] not in {"top_unused_paid_feature", "generic_fallback"}]
with p.open("w", newline="", encoding="utf-8") as h:
    w = csv.DictWriter(h, fieldnames=fields)
    w.writeheader()
    for r in rows:
        w.writerow({**{k: "" for k in new}, **r})
EOF
```

- [ ] **Step 2: Rewrite the two wrong descriptions**

Edit the first row of each feature. Source in parentheses.

`hcp_assist` (sheet master list, row `hcp_assist`):
> HCP Assist is a 24/7 live call-answering service: trained agents answer the Pro's calls around the clock, take customer information, and can book jobs or estimates into the Pro's calendar. It is a human service, not the CSR AI phone agent.

`flat_rate_pricing` (sheet near-ready tab, row `flat_rate_pricing`):
> Lets a Pro build consistent, cost-based service prices by combining labor rates, material costs, and margin into a fixed price the technician quotes on site. Distinct from the Price Book, which is the catalog of services those prices live in.

- [ ] **Step 3: Audit the other 22 descriptions against the sheet**

For each remaining feature, compare the CSV description's first sentence to the matching sheet value statement or master-list description listed in the table under Task 3. Change a description only if it names a capability the sheet says the feature does not have. Note each change in the commit message. Expected outcome: zero to three changes. Do not rewrite for style.

- [ ] **Step 4: Fill `display_name`, `aliases`, `plans` on the first row of each feature**

Plan strings are verbatim from the sheet's Plans column. `core4` below means `Core SaaS Basic;Core SaaS Essentials;Core SaaS MAX;Core SaaS MAX+`.

| feature | display_name | aliases | plans |
| --- | --- | --- | --- |
| online_booking | Online booking | booking_widget;online_booking.scheduling;feature_online_booking_state | core4 |
| sales_proposal | Sales Proposal Tool | feature_sales_proposal_state | Core SaaS MAX;Core SaaS MAX+ |
| service_agreements | Service Plans | service_plans;create_a_service_plan;feature_service_agreements_state | Core SaaS MAX;Core SaaS MAX+ |
| hcp_assist | HCP Assist | feature_hcp_assist_state | Add-on |
| quickbooks | QuickBooks integration | quickbooks_desktop;feature_quickbooks_state | Core SaaS Essentials;Core SaaS MAX;Core SaaS MAX+ |
| voip | Voice | feature_voip_state | Add-on |
| card_on_file | Card on file | feature_card_on_file_state | core4 |
| time_tracking | Time tracking | time_tracking_by_tech;employee_time_tracking;payroll_time_tracking;feature_time_tracking_state | core4 |
| flat_rate_pricing | Flat rate pricing | feature_flat_rate_pricing_state | Core SaaS Essentials;Core SaaS MAX;Core SaaS MAX+ |
| premium_reviews | Reviews | reviews;review_requests;receive_an_external_review;feature_premium_reviews_state | core4 |
| wisetack | Consumer Financing | consumer_financing | core4 |
| scheduling_dispatching | Scheduling | scheduling;dispatching;scheduling.manage;scheduling.maps | core4 |
| invoicing | Invoices | invoices;invoices_lite | core4 |
| payment_processing | Online Payments | online_payments;connect_bank_account | core4 |
| instapay | Instapay | (blank) | (blank) |
| marketing_campaigns | Campaigns | campaigns;campaigns.included;sms_campaigns;postcards;create_automated_campaign | core4 |
| two_way_texting | SMS Text messaging | sms_number;inbox;register_for_sms_texts | core4 |
| customer_portal | Customer Portal | customers.portal;enable_customer_portal | core4 |
| leads_pipeline | Pipeline | sales_management;estimates_kaban_board;jobs.inbox | Add-on |
| recurring_jobs | Recurring jobs | jobs.recurring | (blank) |
| inventory_materials | Material tracking | jobs.material_tracking;price_book.materials | (blank) |
| reporting_analytics | Reporting | reporting;advanced_reporting;tags_report | core4 |
| mobile_field_app | Housecall Pro mobile app | native_mobile_app | (blank) |
| automated_reminders | Job reminders | appointment_reminders;customer_notifications | (blank) |

- [ ] **Step 5: Run the tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_catalog.py -q
```

Expected: all pass except `test_related_features_are_closed_set` still passes trivially (blank lists). Every test green.

- [ ] **Step 6: Commit**

```bash
git add data/hcp_feature_catalog.csv
git commit -m "feat(catalog): v2 columns, drop non-feature rows, fix hcp_assist and flat_rate_pricing"
```

---

### Task 3: `why_it_matters` and `related_features` on existing rows

**Files:**
- Modify: `services/api/data/hcp_feature_catalog.csv`

**Interfaces:**
- Produces: first row of each of the 24 features carries a one-sentence `why_it_matters` traced to a source, and zero to three `related_features`

- [ ] **Step 1: Write `why_it_matters` from the named source**

One sentence, under 30 words, states the business outcome for the Pro. Condense; do not copy marketing copy verbatim. Blank where the source column says none.

| feature | source | related_features |
| --- | --- | --- |
| online_booking | sheet near-ready value statement | scheduling_dispatching;customer_portal |
| sales_proposal | sheet near-ready value statement | flat_rate_pricing;invoicing |
| service_agreements | sheet shipped value statement | recurring_jobs;card_on_file |
| hcp_assist | sheet master list description | online_booking;voip |
| quickbooks | sheet near-ready value statement | invoicing;payment_processing |
| voip | sheet master list description | two_way_texting;hcp_assist |
| card_on_file | sheet near-ready value statement | payment_processing;service_agreements |
| time_tracking | sheet near-ready value statement (time_tracking_by_tech) | scheduling_dispatching;reporting_analytics |
| flat_rate_pricing | sheet near-ready value statement | sales_proposal;inventory_materials |
| premium_reviews | sheet shipped value statement (reviews) | marketing_campaigns;automated_reminders |
| wisetack | sheet near-ready value statement (consumer_financing) | payment_processing;sales_proposal |
| scheduling_dispatching | sheet near-ready value statement (scheduling) | mobile_field_app;automated_reminders |
| invoicing | none | payment_processing;card_on_file |
| payment_processing | none | card_on_file;invoicing |
| instapay | none | payment_processing |
| marketing_campaigns | sheet shipped value statement (campaigns.included) | premium_reviews;two_way_texting |
| two_way_texting | sheet shipped value statement (sms_number) | automated_reminders;marketing_campaigns |
| customer_portal | sheet shipped value statement (customers.portal) | online_booking;payment_processing |
| leads_pipeline | sheet master list description (sales_management) | sales_proposal;online_booking |
| recurring_jobs | existing CSV description | service_agreements;scheduling_dispatching |
| inventory_materials | sheet master list description (jobs.material_tracking) | flat_rate_pricing |
| reporting_analytics | sheet near-ready value statement (reporting) | time_tracking |
| mobile_field_app | sheet master list description (native_mobile_app) | scheduling_dispatching;time_tracking |
| automated_reminders | sheet master list description (appointment_reminders) | two_way_texting;scheduling_dispatching |

- [ ] **Step 2: Run the tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_catalog.py -q
```

Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add data/hcp_feature_catalog.csv
git commit -m "feat(catalog): why_it_matters and related_features for existing features"
```

---

### Task 4: Five new features

**Files:**
- Modify: `services/api/data/hcp_feature_catalog.csv`

**Interfaces:**
- Produces: 29 features total. Each new feature is one row with `works_on=no_cta`.

- [ ] **Step 1: Append one row per feature**

All five: `cta_id`, `label`, `url` blank; `works_on` = `no_cta`; `notes` = `No QA'd CTA yet. Add a URL row once someone with a Pro login confirms it renders.`

| feature | display_name | aliases | description source | why_it_matters source | related_features | plans |
| --- | --- | --- | --- | --- | --- | --- |
| price_book | Price book | price_book.estimate_templates;visual_price_book;price_book.pricing_forms | sheet near-ready value statement | same | flat_rate_pricing;sales_proposal;inventory_materials | core4 |
| csr_phone | CSR AI Phone | csr_chat;enable_call_forwarding_with_phone_carrier | sheet shipped value statement | same | hcp_assist;online_booking;voip | Non Core SaaS |
| estimates | Estimates | estimates.manage;estimates.web_signatures;create_an_estimate | sheet shipped value statement | same | sales_proposal;invoicing;price_book | core4 |
| marketing_health_score | Marketing Health Score | connect_gbp;view_marketing_health_score | sheet shipped value statement | same | premium_reviews;marketing_campaigns | (blank) |
| job_costing | Job costing | job_cost;jobs.costing | sheet master list description (jobs.costing) | same | invoicing;time_tracking;inventory_materials | core4 |

Description: one or two sentences, first sentence states what the feature does, second sentence names the closest thing it is not, matching the existing rows' style. No em dashes.

- [ ] **Step 2: Run the whole backend suite, not just the catalog tests**

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

Expected: everything green. `test_feasibility_suffix_omits_sentinel_works_on` still passes because `payment_processing` is untouched.

- [ ] **Step 3: Smoke the two loaders**

```bash
PYTHONPATH=src .venv/bin/python -c "
from waypoint.catalog import CATALOG
from waypoint.workbench import load_catalog
assert len(CATALOG) == 29 and len(load_catalog()) == 29
print(CATALOG['csr_phone'].description)
print(load_catalog()['price_book'])"
```

Expected: `29` features from both, the printed description is the new text, and `load_catalog` still returns `description`, `use_case`, `eligibility` keys.

- [ ] **Step 4: Commit**

```bash
git add data/hcp_feature_catalog.csv
git commit -m "feat(catalog): add price_book, csr_phone, estimates, marketing_health_score, job_costing"
```

---

### Task 5: Column guide for Jake

**Files:**
- Create: `services/api/data/README.md`

- [ ] **Step 1: Write the guide**

Under 60 lines. Sections, in order:

1. **What this file is.** One paragraph. Runtime reads `feature`, `description`, `works_on`. The workbench authoring lab reads the whole file as text. Both keep working if the first seven columns stay in order.
2. **Columns.** One line each for all twelve. State the `;` separator and the first-row-only convention.
3. **Inclusion rule.** A feature gets a row if it backs a `feature_<key>_state` field in the brief, or it is live in OCL, or it is Tier 1-2 in the OCL prioritization tab, and a Pro can adopt it because of an outreach message.
4. **Adding a row.** Four steps: pick the key, add aliases from the OCL sheet, source `why_it_matters` and cite it in the commit, run `pytest tests/test_catalog.py`.
5. **Known gaps.** The three open items from this plan's header, plus the list of features whose `why_it_matters` is blank and why.
6. **Sources.** Link the OCL sheet and the research doc.

No em dashes.

- [ ] **Step 2: Commit and open the PR against V4-Improvements**

```bash
git add services/api/data/README.md
git commit -m "docs(catalog): column guide, inclusion rule, known gaps"
git push -u origin feat/feature-catalog-v2
gh pr create --base V4-Improvements --title "Feature catalog v2: aliases, why_it_matters, plans, five OCL features" --body "$(cat <<'EOF'
Replaces the 26-feature CSV with 29 verified features and five new columns. No code change; both loaders unchanged.

- Fixes two wrong descriptions (hcp_assist was described as an AI drafting tool; flat_rate_pricing was described as the Price Book).
- Drops the two non-feature rows.
- Adds display_name, aliases, why_it_matters, related_features, plans, sourced from the OCL readiness sheet.
- Adds price_book, csr_phone, estimates, marketing_health_score, job_costing with no_cta rows pending URL QA.
- Tests pin the header order, closed related_features, alias uniqueness, and every brief feature resolving.

Open items are in services/api/data/README.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review

- **Coverage.** Transcript asks: label (display_name, Task 2), why it matters (Task 3), related features (Task 3), expandable (README inclusion rule, Task 5), shorter by cutting useless rows (Task 2 drops two, and the decisions table explains why no more), naming mismatch (aliases, Task 2, test in Task 1). Research doc's gaps: value prop (why_it_matters), plan (plans). App-vs-web capability is out of scope and stays as `works_on`.
- **Placeholders.** None. Every row has a named source or an explicit blank.
- **Consistency.** Column names match between the test in Task 1, the writer in Task 2, and the README in Task 5. Feature count 29 matches Task 4's smoke check: 26 minus 2 plus 5.
