-- Rich Action Console — Audience Boundary extract
-- Verified against live Snowflake (wh_consumer) on 2026-06-24.
--
-- Source of truth: marts.customer_base.agg_current_customer
--   - Governed MARTS layer, 1 row per CURRENT customer org.
--   - Carries ORG_UUID (exact, unique, fully populated) alongside ORGANIZATION_ID,
--     so no org_id -> org_uuid mapping join is required. This is the column the
--     Rich Action Console drawer must display as evidence.
--
-- This query produces one row per org with the exact org_uuid plus the banded
-- categorical factors the audience drawer filters on. Bands are derived here
-- (the source columns are raw numerics / attach flags, NOT pre-banded), so the
-- factor vocabulary lives in this SQL, not in the warehouse.
--
-- Phase-one runtime path: load this result into Supabase table
-- "Pathfinder_action_audience_index" and set
-- PATHFINDER_ACTION_CONSOLE_AUDIENCE_SOURCE=supabase.
--
-- Offline fallback: save result to the path named by
-- PATHFINDER_ACTION_CONSOLE_AUDIENCE_PATH (or data/fixtures/action_console/ for
-- local demo).

use warehouse wh_consumer;

select
      acc.org_uuid                                          as ORG_UUID          -- exact evidence id (display)
    , acc.organization_id                                   as ORG_ID            -- secondary/debug context
    , acc.segment_actual                                    as CELL              -- 16-box examples include 2A and 4A
    -- plan tier (Basic / Essentials / MAX family)
    , case
        when acc.core_saas_plan_level = 'Basic'                 then 'basic'
        when acc.core_saas_plan_level = 'Essentials'            then 'essentials'
        when acc.core_saas_plan_level in ('MAX','MAX+','MAX++') then 'max'
        else 'other'
      end                                                   as PLAN
    -- lifecycle: tenure band in months (agg_current_customer is CURRENT customers).
    -- NOTE (open decision): the design spec's "lifecycle 15-30" examples are
    -- days-since-enrollment, which targets onboarding orgs. ENROLLMENT_DATE is
    -- also available if the console should scope to new enrollments instead.
    , case
        when acc.org_tenure is null then 'unknown'
        when acc.org_tenure <= 3    then '0-3m'
        when acc.org_tenure <= 12   then '4-12m'
        when acc.org_tenure <= 36   then '13-36m'
        else '37m+'
      end                                                   as LIFECYCLE_BUCKET
    -- current usage: jobs created in last 28 days
    , case
        when coalesce(acc.t28_jobs_created, 0) = 0 then 'none'
        when acc.t28_jobs_created <= 5             then 'low'
        when acc.t28_jobs_created <= 20            then 'medium'
        else 'high'
      end                                                   as USAGE_BAND
    -- CSR AI / HCP Assist attach. NOTE: this is the subscription ATTACH flag.
    -- Usage-grade CSR-AI signal lives in analytics.main.fact_hcp_assist_and_csr_ai_calls.
    , iff(coalesce(acc.hcp_assist_attached, 0) = 0, 'inactive', 'active') as CSR_AI_STATUS
    -- accounts-receivable: open invoices the PRO sent THEIR customers (NOT the
    -- pro's own subscription dunning). Renamed from the plan's "billing_status"
    -- to avoid conflating AR with subscription billing health (see phase-2 note).
    , case
        when coalesce(acc.t28_open_invoices, 0) = 0 then 'none'
        when acc.t28_open_invoices <= 5             then 'low'
        else 'high'
      end                                                   as OPEN_AR_BAND
    -- payment processing activity (HCP card payments in last 28d)
    , iff(coalesce(acc.t28_cc_count, 0) > 0, 'active', 'inactive') as CC_STATUS
    -- team size (org headcount proxy)
    , case
        when coalesce(acc.t28_org_size, 0) <= 1 then 'solo'
        when acc.t28_org_size <= 5              then 'small_team'
        else 'larger_team'
      end                                                   as TEAM_MEMBER_STATUS
    -- recent outreach the org received across channels in last 28d
    , iff(
        coalesce(acc.t28_calls,0) + coalesce(acc.t28_emails_sent,0)
        + coalesce(acc.t28_sms_messages,0) + coalesce(acc.t28_customer_success_communications,0) > 0,
        'recent', 'none'
      )                                                     as RECENT_OUTREACH_STATUS
    -- churn-risk signals (useful as audience factors and for ranking)
    , iff(coalesce(acc.has_open_retention_case, 0) = 1, 'open', 'none') as RETENTION_CASE_STATUS
    , acc.enrollment_eltv                                   as ENROLLMENT_ELTV
    -- banded numerics the factor library can surface (Task 12 discovery targets)
    , case when coalesce(acc.t7_jobs_created,0)=0 then 'none'
           when acc.t7_jobs_created<=3 then 'low' else 'high' end       as JOBS_CREATED_7D_BAND
    , case when coalesce(acc.t28_estimates_created,0)=0 then 'none'
           when acc.t28_estimates_created<=5 then 'low' else 'high' end as ESTIMATES_CREATED_28D_BAND
    , case when coalesce(acc.t28_invoices_sent,0)=0 then 'none'
           when acc.t28_invoices_sent<=5 then 'low' else 'high' end     as INVOICES_SENT_28D_BAND
    , 'agg_current_customer'                                as EVIDENCE_SOURCE
    , current_timestamp()                                  as OBSERVED_AT
  from marts.customer_base.agg_current_customer acc
 where 1=1
   and acc.org_uuid is not null
   and acc.segment_actual rlike '^[0-9]+[A-Z]$'   -- valid 16-box cells only
;

-- ──────────────────────────────────────────────────────────────────────────
-- PHASE-2 factors (verified to EXIST but at a different grain — pro/event level,
-- requiring a pro_uuid -> organization_id roll-up before joining here):
--
--   online_booking_status  feature_by_orgday.BOOKING_WIDGET   (org-day attach; take latest row per org)
--   login_recency          analytics.main.fact_password_login_activity
--                            (LOGIN_FLAG, SEGMENT_TIMESTAMP, PRO_UUID) -> max(login ts) per org
--   mobile_app_status      analytics.staging.stg__platform_pro_day.PLATFORM
--                            (or fact_app_downloads_funnel_events) -> any ios/android activity per org
--   subscription_billing   true dunning/past-due state is NOT in agg_current_customer
--                            (NEXT_BILLING_DATE exists; failed-payment status needs a billing source)
--
-- These are intentionally excluded from the verified core above so this query
-- runs as-is. Add them as LEFT JOINs once the pro->org roll-ups are validated.
