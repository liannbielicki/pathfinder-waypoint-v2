-- Org lifecycle segment counts: Trial, Pre-Enroll, Onboarding (0-3m), At-Risk.
-- Grain: org_uuid (not pros). Run daily/weekly/monthly/yearly as-is -- every
-- column is derived from CURRENT_DATE() and today's snapshot tables, so the
-- same query always reflects "as of now." To trend over time, insert the
-- output into a datazoo tracking table once a day (see note at bottom).
--
-- "Tier 1" note: analytics.main.plan_by_orgday.account_type is HCP's field
-- that categorizes an org's plan into 'Type 1', 'Type 3', or 'none' -- but
-- despite the name, "Type 3" (small/starter/large/extra large/medium/freeze)
-- is the ~65k-org population that matches what's referred to as "Tier 1
-- plans" (confirmed: Type 3 = 65,354 orgs total, vs Type 1/'lite' = 49,112).
-- If that's backwards from what you meant, flip TIER1_ACCOUNT_TYPE below.
--
-- Segment definitions (adjust thresholds here if the business definition
-- changes -- this is the only place they're encoded):
--   trial                  plan_tier = 'trial' (their CURRENT plan tier --
--                          already active-only, not "ever entered a trial".
--                          97% of these started within the last 14 days,
--                          i.e. inside the standard trial window)
--   pre_enroll             org exists, never trialed, never paid, no current
--                          plan (dim_organization defaults these to
--                          plan_tier = 'legacy'). Not restricted to Tier 1
--                          since these orgs haven't reached any plan tier yet.
--   onboarding_0_3m_tier1  Tier 1 (account_type = 'Type 3'), currently
--                          paying, 0-3 months since most recent enrollment
--   at_risk (tier 1)       Tier 1, currently paying, enrolled 3+ months, and
--                          flagged risky by EITHER next-bill churn risk
--                          (moderate-high/high band) OR health grade (D/F).
--                          Numeric churn score (0-100, churn_prob * 100) is
--                          banded in 10-point buckets separately below.
--   active_healthy_tier1   Tier 1, currently paying, enrolled 3+ months, not
--                          flagged at_risk
--
-- Source tables:
--   analytics.main.dim_organization              org spine (org_uuid, plan_tier, currently_paying, first_trial_start_date)
--   analytics.main.orginfo                        enrollment_date (most recent enrollment)
--   analytics.main.plan_by_orgday                 account_type (Tier 1 = 'Type 3'), latest date
--   production.churn.next_bill_churn_by_orgday    churn_prob (0-1 numeric score) + churn_risk band, latest date
--   production.customer_success.health_index_yesterday   agg_grade band (A-F), 1 row per org, refreshed daily

with base as (
    select
        org.org_uuid                                                        as org_uuid
        , org.organization_id                                               as organization_id
        , org.currently_paying                                              as currently_paying
        , org.plan_tier                                                     as plan_tier
        , org.first_trial_start_date                                        as first_trial_start_date
        , oi.enrollment_date                                                as enrollment_date
        , pbo.account_type                                                  as account_type
        , datediff('month', oi.enrollment_date, current_date())             as months_since_enrollment
    from analytics.main.dim_organization as org
    left join analytics.main.orginfo as oi
        on oi.organization_id = org.organization_id
    left join analytics.main.plan_by_orgday as pbo
        on pbo.organization_id = org.organization_id
        and pbo.date = (select max(date) from analytics.main.plan_by_orgday)
    where 1=1
        and org.excluded_org = 0
)
, tier1_paying as (
    -- "Tier 1" = account_type 'Type 3' -- see note above if this needs flipping
    select * from base
    where 1=1
        and account_type = 'Type 3'
        and currently_paying = 1
        and months_since_enrollment > 3
)
, risk as (
    select
        b.org_uuid                                     as org_uuid
        , ch.churn_prob                                 as churn_prob
        , coalesce(ch.churn_risk, 'unscored')           as churn_risk_band
        , coalesce(h.agg_grade, 'unscored')             as health_grade_band
    from tier1_paying as b
    left join production.churn.next_bill_churn_by_orgday as ch
        on ch.organization_id = b.organization_id
        and ch.date = (select max(date) from production.churn.next_bill_churn_by_orgday)
    left join production.customer_success.health_index_yesterday as h
        on h.organization_id = b.organization_id
)
, segmented as (
    select
        b.org_uuid                                     as org_uuid
        , case
            when b.plan_tier = 'trial' then 'trial'
            when b.plan_tier = 'legacy'
                and b.currently_paying = 0
                and b.first_trial_start_date is null then 'pre_enroll'
            when b.account_type = 'Type 3'
                and b.currently_paying = 1
                and b.months_since_enrollment between 0 and 3 then 'onboarding_0_3m_tier1'
            when b.account_type = 'Type 3'
                and b.currently_paying = 1
                and b.months_since_enrollment > 3 then 'active_3m_plus_tier1'
            else null
          end                                           as segment
    from base as b
)
select
    current_date()          as AS_OF_DATE
    , 'segment_count'        as METRIC
    , s.segment              as CATEGORY
    , null                   as BAND
    , count(distinct s.org_uuid) as ORG_COUNT
from segmented as s
where s.segment is not null
group by 1, 2, 3, 4

union all

select
    current_date()                                                as AS_OF_DATE
    , 'at_risk_band_tier1'                                         as METRIC
    , case
        when r.churn_risk_band in ('moderate-high', 'high')
            or r.health_grade_band in ('D', 'F') then 'at_risk'
        else 'active_healthy'
      end                                                          as CATEGORY
    , r.churn_risk_band || ' / ' || r.health_grade_band            as BAND
    , count(distinct r.org_uuid)                                   as ORG_COUNT
from risk as r
group by 1, 2, 3, 4

union all

-- numeric churn score, 0-100 scale (churn_prob * 100), banded in 10-point buckets
select
    current_date()                                                as AS_OF_DATE
    , 'churn_score_band_tier1'                                     as METRIC
    , null                                                         as CATEGORY
    , floor(r.churn_prob * 100 / 10) * 10 || '-'
        || (floor(r.churn_prob * 100 / 10) * 10 + 9)               as BAND
    , count(distinct r.org_uuid)                                   as ORG_COUNT
from risk as r
where r.churn_prob is not null
group by 1, 2, 3, 4

order by METRIC, CATEGORY, BAND;

-- To trend daily/weekly/monthly/yearly: create a datazoo table once, e.g.
--   create table datazoo.<your_schema>.org_lifecycle_segments_history (
--       as_of_date date, metric string, category string, band string, org_count number
--   );
-- then run this query daily via `insert into ... select * from (<query above>)`
-- (e.g. a Snowflake task, Airflow DAG, or a cron'd script). Trend by week/month/
-- year with `date_trunc('week'|'month'|'year', as_of_date)` over the history table.
