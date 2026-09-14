# Waypoint org context vs. Context Layer — comparison

Compares what **Waypoint currently pulls** (branch `V3-Improvements`; org-context contract
is unchanged from V2) against what the **Context Layer API** exposes.

Sources of truth:
- Waypoint: `services/api/src/waypoint/n8n.py` — `OrgBrief`, `CONTRACT_VERSION = "org-context-v2"`.
- Context Layer: [Confluence API doc](https://housecall.atlassian.net/wiki/spaces/ADMIN/pages/4383408295), `GET /api/context_layer/:org`.

---

## 1. What Waypoint pulls today (`OrgBrief`, contract `org-context-v2`)

Delivered by **n8n → Snowflake**, then filtered through a **closed allowlist** (`ALLOWED_FIELDS`)
that drops anything not listed — the PII guard. **Every value is a pre-bucketed band / state / grade
enum. No raw numbers, no free text, no identity.** ~45 fields:

**Firmographic / lifecycle**
`vertical`, `plan_tier`, `org_size_band`, `tenure_band`, `segment`, `lifecycle_stage`,
`health_grade`, `upsell_grade`, `churn_risk_state`, `mrr_band`, `platform_usage_band`

**Feature adoption state** (has/uses now — one `_state` enum each)
`feature_online_booking_state`, `feature_premium_reviews_state`, `feature_sales_proposal_state`,
`feature_service_agreements_state`, `feature_hcp_assist_state`, `feature_quickbooks_state`,
`feature_voip_state`, `feature_card_on_file_state`, `feature_time_tracking_state`,
`feature_flat_rate_pricing_state`, `wisetack_state`, plus `feature_adoption_band`,
`top_unused_paid_feature`, `plan_gap_band`

**Activity / behavior bands (28d windows)**
`jobs_created_28d_band`, `estimates_created_28d_band`, `invoices_sent_28d_band`,
`outreach_count_28d_band`, `payments_28d_band`, `marketing_campaigns_28d_band`,
`leads_created_28d_band`, `jobs_reviewed_28d_band`, `last_job_activity_band`,
`open_ar_band`, `ar_aging_band`, `ltv_score_band`

**Consent / engagement**
`sms_consent_state`, `email_consent_state`, `email_engagement_state`

**Recommendation (single focus)**
`recommended_focus`, `recommended_focus_value_band`, `recommended_focus_retention_lift_band`

---

## 2. What the Context Layer pulls

Aggregates **Salesforce + HCW + call transcriptions + Snowflake** (Unified Recommendations).
Returns **raw strings / lists / integers**, not bands. Two top-level blocks:

**`firmographics`**
`industry`, `segment`, `budget_range`, `company_goal` (enum: efficiency|growth),
`current_tools` (list), `tech_readiness`, `company_size_range`, `pain_points` (list)

**`features[]`** — one entry per pro↔feature relation (used / discussed on a call / DS-recommended)
`name` (Billing FAC key), `sentiment` (from call transcripts), `recommendation` (adopt/expand/reactivate),
`recommendation_pitch_rank` (int), `recommendation_self_serve_rank` (int),
`adopted` / `churned` / `days_since_last_use` (marked TODO in the doc — not yet populated)

Plus a top-level `churn` field (null in the example; undocumented).

---

## 3. Key differences

| Dimension | Waypoint (`org-context-v2`) | Context Layer |
| --- | --- | --- |
| **Source** | Snowflake only (via n8n) | Salesforce + HCW + **call transcripts** + Snowflake |
| **Value shape** | Bucketed bands/enums/grades | Raw strings, lists, integers |
| **PII posture** | Hard allowlist, no free text, no identity | Free text (`budget_range`, `pain_points`), identity-adjacent |
| **Feature model** | ~11 fixed `_state` flags, one per known feature | Open `features[]` list, any FAC feature, w/ sentiment + rank |
| **Recommendation** | 1 `recommended_focus` + value/retention band | Per-feature `recommendation` + pitch & self-serve ranks |
| **Qualitative signal** | None (bands only) | `sentiment`, `pain_points`, `tech_readiness`, `current_tools`, `company_goal` |
| **Freshness** | Snowflake batch cadence | Salesforce/HCW real-time; transcripts post-call; DS daily |
| **Refresh** | n8n push per run | On-demand GET |
| **Maturity** | In production, stable contract | Some fields TODO (`adopted`/`churned`/`days_since_last_use`) |

## 4. Which is better?

**They're complementary, and the Context Layer is the stronger source going forward** — for one
specific reason: it carries **qualitative, call-derived signal Waypoint structurally cannot have**
(sentiment, pain points, budget, stated goal, current tools, tech readiness) and a **richer
per-feature recommendation model** (pitch vs. self-serve ranks). That's exactly the context that
makes outreach copy specific instead of generic.

**But Waypoint's brief is better on two axes today** and shouldn't be dropped wholesale:
1. **Privacy/safety** — the band-only allowlist is a deliberate PII guard. Context Layer returns free
   text and identity-adjacent data; consuming it means re-applying a sanitizer, not trusting it raw.
2. **Behavioral breadth** — Waypoint's 28d activity/AR/payment/engagement bands have **no equivalent**
   in the Context Layer, which is firmographic + feature-centric.

**Recommendation:** treat Context Layer as an **enrichment layer on top of** the existing `OrgBrief`,
not a replacement. Pull firmographics + `features[]` (sentiment/ranks) to sharpen persona match and
copy; keep Waypoint's behavioral bands and its allowlist sanitizer as the privacy boundary. Wait for
`adopted`/`churned`/`days_since_last_use` to leave TODO before relying on Context Layer for adoption state.
