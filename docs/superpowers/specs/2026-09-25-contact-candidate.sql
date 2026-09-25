-- waypoint_contact_candidate: one row per active admin of the org.
-- Row shape matches the workbench flow: {query_name, variable_name, value, metadata}.
-- Identifiers + booleans only; no phone/email values leave Snowflake.
with org as (select {{$json.body.organization_id}}::number as organization_id),
adm as (
  select d.pro_uuid, d.organization_id, d.founding_pro, d.created_dt,
         lower(trim(d.email)) as em,
         right(regexp_replace(d.mobile_number, '[^0-9]', ''), 10) as ph,
         coalesce(d.rank_by_phone, 1) > 1 as phone_shared
  from analytics.main.dim_service_pro d join org using (organization_id)
  where d.is_admin and not coalesce(d.archived, false)
),
acct as (
  select a.id from hcp_integrations.housecallpro_salesforce.account a, org
  where not a.isdeleted and trim(a.org_id__c) = to_varchar(org.organization_id)
),
sf_org as (   -- an org can map to 2-3 accounts: any-true, fail closed
  select boolor_agg(a.sms_opt_out__c) as sms_opt_out,
         boolor_agg(a.dnc_phone__c ilike '%dnc%' or a.dnc_phone__c ilike '%do not%') as dnc_phone_text,
         boolor_agg(nullif(trim(a.dnc_email__c), '') is not null) as dnc_email_present,
         count(*) as n_accounts
  from hcp_integrations.housecallpro_salesforce.account a join acct using (id)
),
sf_opp as (
  select boolor_agg(o.phone__c ilike '%dnc%' or o.phone__c ilike '%do not%') as opp_dnc
  from hcp_integrations.housecallpro_salesforce.opportunity o join acct on acct.id = o.accountid
  where not o.isdeleted
),
sf_con as (   -- contact-level, matched to an admin by email or 10-digit phone
  select adm.pro_uuid,
         boolor_agg(c.point_of_contact__c) as is_poc,
         boolor_agg(c.donotcall) as sf_donotcall,
         boolor_agg(c.hasoptedoutofemail) as sf_email_opted_out,
         boolor_agg(c.smagicinteract__smsoptout__c) as sf_sms_opted_out
  from adm
  join hcp_integrations.housecallpro_salesforce.contact c
    on not c.isdeleted
   and c.accountid in (select id from acct)
   and (lower(trim(c.email)) = adm.em
        or (length(adm.ph) = 10
            and right(regexp_replace(coalesce(c.mobilephone, c.phone), '[^0-9]', ''), 10) = adm.ph))
  group by 1
),
gu as (
  select lower(trim(email)) as em,
         max(opt_out_from_iterable) = 1 as gu_iterable,
         greatest(max(opt_out_from_salesforce), max(opt_out_from_pardot),
                  max(opt_out_from_braze), max(opt_out_from_autopilot)) = 1 as gu_other
  from analytics.main.global_email_unsubscribes
  where lower(trim(email)) in (select em from adm)
  group by 1
),
reco_org as (
  select r.* from production.reco.channel_recommendations_org r join org using (organization_id)
)
select 'waypoint_contact_candidate' as query_name,
       'contact_candidate' as variable_name,
       object_construct_keep_null(
         'pro_uuid', adm.pro_uuid,
         'founding_pro', adm.founding_pro = 1,
         'created_dt', adm.created_dt,
         'has_phone', length(adm.ph) = 10,
         'phone_shared', adm.phone_shared,
         'is_poc', coalesce(sf_con.is_poc, false),
         'sf_contact_matched', sf_con.pro_uuid is not null,
         -- RECO pro grain (NULL when RECO has no row for this Pro)
         'recommended_action', p.recommended_action,
         'p_email', p.p_email, 'p_sms', p.p_sms, 'p_call', p.p_call,
         'pct_email', p.resp_pct_in_channel_email,
         'pct_sms', p.resp_pct_in_channel_sms,
         'pct_call', p.resp_pct_in_channel_call,
         'email_optout_suppressed', p.email_optout_suppressed,
         'reco_scoring_date', p.scoring_date,
         -- RECO org grain (repeated on every row)
         'org_best_channel', ro.org_best_channel,
         'email_contact_pro', ro.email_contact_pro,
         'sms_contact_pro', ro.sms_contact_pro,
         'call_contact_pro', ro.call_contact_pro,
         'org_scoring_date', ro.scoring_date,
         -- Salesforce org grain
         'sf_n_accounts', coalesce(sf_org.n_accounts, 0),
         'sf_sms_opt_out', coalesce(sf_org.sms_opt_out, false),
         'sf_dnc_phone', coalesce(sf_org.dnc_phone_text, false) or coalesce(sf_opp.opp_dnc, false),
         'sf_dnc_email_present', coalesce(sf_org.dnc_email_present, false),
         -- Salesforce contact grain
         'sf_donotcall', coalesce(sf_con.sf_donotcall, false),
         'sf_email_opted_out', coalesce(sf_con.sf_email_opted_out, false),
         'sf_sms_opted_out', coalesce(sf_con.sf_sms_opted_out, false),
         -- global email unsubscribes, split by source
         'global_unsub_iterable', coalesce(gu.gu_iterable, false),
         'global_unsub_other', coalesce(gu.gu_other, false)
       )::variant as value,
       object_construct('source_table', 'waypoint.contact_candidate',
                        'sources', array_construct('analytics.main.dim_service_pro',
                          'production.reco.channel_recommendations',
                          'production.reco.channel_recommendations_org',
                          'hcp_integrations.housecallpro_salesforce.account/opportunity/contact',
                          'analytics.main.global_email_unsubscribes')) as metadata
from adm
left join production.reco.channel_recommendations p
  on p.pro_uuid = adm.pro_uuid and p.organization_id = adm.organization_id
left join reco_org ro on ro.organization_id = adm.organization_id
left join sf_con on sf_con.pro_uuid = adm.pro_uuid
left join gu on gu.em = adm.em
cross join sf_org
cross join sf_opp
order by adm.pro_uuid;
