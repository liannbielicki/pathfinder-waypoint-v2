# Variable Audit — pro_3be6e3fb554642da830a286684632c94 (Mountain HVAC)

Parts: 1 (MARTS org snapshot) · 2 (MARTS event-level rollups) · 3 (PRODUCTION models) · Addendum (churn/health/usage consolidated).

---

## Identity resolution

```sql
select *
from marts.sales.int__sf_account_pro_org_bridge
where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94'
limit 5
```
→ `organization_id = 889901`, `org_uuid = 1c80faf4-4cc4-461e-86a8-1d131eb3cdff`, name **Mountain HVAC**.

MARTS has no pro-grain "everything" table — `pro_uuid` only appears as a key inside event-level `communication.detail_*` tables. Part 1 uses the org-grain table instead.

---

## Part 1 — Org Snapshot (`marts.customer_base.agg_current_customer`, 221 vars)

```sql
show tables like '%CURRENT_CUSTOMER%' in database marts;

select column_name, ordinal_position, data_type, comment
from marts.information_schema.columns
where table_schema = 'CUSTOMER_BASE'
  and table_name = 'AGG_CURRENT_CUSTOMER'
order by ordinal_position;

select array_construct(*) as row_arr
from marts.customer_base.agg_current_customer
where organization_id = 889901;
```

| Variable | Value | Description |
|---|---|---|
| ORGANIZATION_ID | 889901 | Unique HCP organization id. |
| CORE_SAAS_PLAN_LEVEL | Basic | Core SaaS plan tier the org is on. |
| PLAN_FAMILY | ["Core SaaS"] | Product plan families the org is subscribed to. |
| PLAN_SUBFAMILY | ["Standard"] | Sub-tier within each plan family. |
| ADDITIONAL_FEATURES | NULL | Any add-on features beyond the base plan. |
| SAAS_MRR | 79.00 | Monthly recurring revenue from core SaaS subscription. |
| FEATURE_MRR | 0.00 | Monthly recurring revenue from paid add-on features. |
| TOTAL_GROSS_MRR_USD | 79.00 | Total gross MRR across SaaS + features. |
| ACCOUNT_TYPE | Type 3 | Current billing/account classification. |
| TYPE_3_ENROLLMENT_DATE | 2026-07-01 | Date the org enrolled as a Type 3 (paying) account. |
| ENROLLMENT_DATE | 2026-07-01 | Date the org first enrolled. |
| ORG_TENURE | 3 | Months since org enrollment. |
| ACCOUNT_TYPE_TENURE | 3 | Months since current account type began. |
| AFFILIATION | NULL | Partner/affiliate program the org is tied to, if any. |
| SALESFORCE_LEAD_ID | 00QPD00000XFTrV2AX | Linked Salesforce lead record. |
| SALESFORCE_ACCOUNT_ID | 001PD00000gW8K1YAK | Linked Salesforce account record. |
| ORG_UUID | 1c80faf4-4cc4-461e-86a8-1d131eb3cdff | Org's UUID. |
| ORGANIZATION_NAME | Mountain HVAC | Org's business name. |
| SEGMENT_DECLARED | 1B | Customer segment the org self-reported. |
| SEGMENT_ACTUAL | 1A | Customer segment as measured by actual usage. |
| INDUSTRY_PRO_DATA | Heating & Air Conditioning | Org's declared industry/trade. |
| ACTUAL_ORG_SIZE_PRO_DATA | 1 | Measured org size (headcount proxy). |
| DECLARED_ORG_SIZE_PRO_DATA | 2 | Org's self-reported size bracket. |
| ORGANIZATION_TAG | pro | Internal tag classifying the account. |
| IS_FRANCHISE | 0 | Whether the org is part of a franchise. |
| FRANCHISE_TYPE | NULL | Type of franchise relationship, if any. |
| FRANCHISE_PARENT_ORGANIZATION_ID | NULL | Parent org id if part of a franchise group. |
| ACH_PERCENT_FEE | 0.0100 | Org's effective ACH payment processing fee rate. |
| INVOICE_PERCENT_FEE | 0.0299 | Org's effective invoice payment fee rate. |
| CARD_PRESENT_PERCENT_FEE | 0.0259 | Org's effective card-present transaction fee rate. |
| CARD_NOT_PRESENT_PERCENT_FEE | 0.0349 | Org's effective card-not-present transaction fee rate. |
| INSTANT_PAYOUT_PERCENT_FEE | 0.0100 | Org's effective instant-payout fee rate. |
| PRIMARY_CONTACT_NAME | Karla Bravo LaPointe | Primary named contact for the org. |
| BEST_EMAIL | mymountainhvac@gmail.com | Best-known email for the org. |
| BEST_PHONE | 4083518919 | Best-known phone number for the org. |
| CITY | Rohnert Park | Org's city. |
| STATE | CA | Org's state. |
| ZIP | 94928 | Org's zip code. |
| COUNTRY | US | Org's country. |
| ACTIVE_APPLE_ORG_FLAG | 0 | Whether the org uses the Apple-distributed app. |
| HAS_OPEN_RETENTION_CASE | 0 | Whether a retention/save case is currently open. |
| SALES_MOTION | Sales | How the org was acquired (sales-assisted vs. self-serve). |
| RE_ENROLLMENT_FLAG | 0 | Whether this is a repeat/win-back enrollment. |
| MARKETING_CHANNEL_SUPER_GROUP | Marketing - Unpaid | Top-level acquisition channel category. |
| MARKETING_CHANNEL_SUB_GROUP | Organic - Web - Direct | Mid-level acquisition channel category. |
| MARKETING_CHANNEL | organicdirect | Specific acquisition channel. |
| ENROLLMENT_ELTV | 18569.39 | Estimated lifetime value at enrollment. |
| SUPERPRO_FLAG | 0 | Whether the org is flagged as a "superpro" power user. |
| VEHICLE_TRACKING_ATTACHED | 0 | Whether Vehicle Tracking is attached. |
| PAYROLL_ATTACHED | 0 | Whether Payroll is attached. |
| PIPELINE_ATTACHED | 0 | Whether Pipeline is attached. |
| PROFIT_RHINO_ATTACHED | 0 | Whether Profit Rhino pricing is attached. |
| SERVICE_AGREEMENTS_ATTACHED | 0 | Whether Service Agreements is attached. |
| SALES_PROPOSAL_ATTACHED | 0 | Whether Sales Proposal is attached. |
| WEBSITE_ATTACHED | 0 | Whether the Website product is attached. |
| VOIP_ATTACHED | 0 | Whether VoIP/phone product is attached. |
| HCP_ASSIST_ATTACHED | 0 | Whether HCP Assist is attached. |
| T1_CARD_NOT_PRESENT_COUNT | 0 | Card-not-present payments, last 1 day. |
| T1_CARD_NOT_PRESENT_AMOUNT | 0.00 | Card-not-present payment $, last 1 day. |
| T1_CARD_PRESENT_COUNT | 0 | Card-present payments, last 1 day. |
| T1_CARD_PRESENT_AMOUNT | 0.00 | Card-present payment $, last 1 day. |
| T1_PAID_INVOICE_ONLINE_COUNT | 0 | Invoices paid online, last 1 day. |
| T1_PAID_INVOICE_ONLINE_AMOUNT | 0.00 | Invoices paid online $, last 1 day. |
| T1_CARD_READER_COUNT | 0 | Card reader payments, last 1 day. |
| T1_CARD_READER_AMOUNT | 0.00 | Card reader payment $, last 1 day. |
| T1_TAP_TO_PAY_COUNT | 0 | Tap-to-pay payments, last 1 day. |
| T1_TAP_TO_PAY_AMOUNT | 0.00 | Tap-to-pay payment $, last 1 day. |
| T7_CARD_NOT_PRESENT_COUNT | 1 | Card-not-present payments, last 7 days. |
| T7_CARD_NOT_PRESENT_AMOUNT | 2120.00 | Card-not-present payment $, last 7 days. |
| T7_CARD_PRESENT_COUNT | 0 | Card-present payments, last 7 days. |
| T7_CARD_PRESENT_AMOUNT | 0.00 | Card-present payment $, last 7 days. |
| T7_PAID_INVOICE_ONLINE_COUNT | 0 | Invoices paid online, last 7 days. |
| T7_PAID_INVOICE_ONLINE_AMOUNT | 0.00 | Invoices paid online $, last 7 days. |
| T7_CARD_READER_COUNT | 0 | Card reader payments, last 7 days. |
| T7_CARD_READER_AMOUNT | 0.00 | Card reader payment $, last 7 days. |
| T7_TAP_TO_PAY_COUNT | 0 | Tap-to-pay payments, last 7 days. |
| T7_TAP_TO_PAY_AMOUNT | 0.00 | Tap-to-pay payment $, last 7 days. |
| T28_CC_COUNT | 9 | Credit card payments, last 28 days. |
| T28_CC_AMOUNT | 8038.50 | Credit card payment $, last 28 days. |
| T28_ACH_COUNT | 0 | ACH payments, last 28 days. |
| T28_ACH_AMOUNT | 0.00 | ACH payment $, last 28 days. |
| T28_RDC_SUCCESS_COUNT | 0 | Successful remote deposit capture (check scan) payments, last 28 days. |
| T28_RDC_SUCCESS_AMOUNT | 0.00 | RDC payment $, last 28 days. |
| T28_CHECK_COUNT | 16 | Check payments recorded, last 28 days. |
| T28_CHECK_AMOUNT | 14787.50 | Check payment $, last 28 days. |
| T28_CASH_COUNT | 1 | Cash payments recorded, last 28 days. |
| T28_CASH_AMOUNT | 2120.00 | Cash payment $, last 28 days. |
| T28_OTHER_CC_COUNT | 0 | Other credit card payments, last 28 days. |
| T28_OTHER_CC_AMOUNT | 0.00 | Other credit card payment $, last 28 days. |
| T28_ETRANSFER_COUNT | 0 | E-transfer payments, last 28 days. |
| T28_ETRANSFER_AMOUNT | 0.00 | E-transfer payment $, last 28 days. |
| T28_WARRANTY_WORK_COUNT | 0 | Payments recorded as warranty work, last 28 days. |
| T28_WARRANTY_WORK_AMOUNT | 0.00 | Warranty work payment $, last 28 days. |
| T28_OTHER_COUNT | 1 | Payments recorded under "other" method, last 28 days. |
| T28_OTHER_AMOUNT | 2120.00 | "Other" method payment $, last 28 days. |
| T28_INSTAPAY_COUNT | 0 | InstaPay (instant payout) transactions, last 28 days. |
| T28_INSTAPAY_AMOUNT | 0.00 | InstaPay $, last 28 days. |
| T28_ACH_COUNT_INSTANT | 0 | Instant ACH transactions, last 28 days. |
| T28_ACH_AMOUNT_INSTANT | 0.00 | Instant ACH $, last 28 days. |
| T28_CARD_NOT_PRESENT_COUNT | 0 | Card-not-present payments, last 28 days. |
| T28_CARD_NOT_PRESENT_AMOUNT | 0.00 | Card-not-present payment $, last 28 days. |
| T28_CARD_PRESENT_COUNT | 0 | Card-present payments, last 28 days. |
| T28_CARD_PRESENT_AMOUNT | 0.00 | Card-present payment $, last 28 days. |
| T28_PAID_INVOICE_ONLINE_COUNT | 1 | Invoices paid online, last 28 days. |
| T28_PAID_INVOICE_ONLINE_AMOUNT | 2120.00 | Invoices paid online $, last 28 days. |
| T28_CARD_READER_COUNT | 0 | Card reader payments, last 28 days. |
| T28_CARD_READER_AMOUNT | 0.00 | Card reader payment $, last 28 days. |
| T28_TAP_TO_PAY_COUNT | 0 | Tap-to-pay payments, last 28 days. |
| T28_TAP_TO_PAY_AMOUNT | 0.00 | Tap-to-pay payment $, last 28 days. |
| T90_CARD_NOT_PRESENT_COUNT | 0 | Card-not-present payments, last 90 days. |
| T90_CARD_NOT_PRESENT_AMOUNT | 0.00 | Card-not-present payment $, last 90 days. |
| T90_CARD_PRESENT_COUNT | 0 | Card-present payments, last 90 days. |
| T90_CARD_PRESENT_AMOUNT | 0.00 | Card-present payment $, last 90 days. |
| T90_PAID_INVOICE_ONLINE_COUNT | 1 | Invoices paid online, last 90 days. |
| T90_PAID_INVOICE_ONLINE_AMOUNT | 2120.00 | Invoices paid online $, last 90 days. |
| T90_CARD_READER_COUNT | 0 | Card reader payments, last 90 days. |
| T90_CARD_READER_AMOUNT | 0.00 | Card reader payment $, last 90 days. |
| T90_TAP_TO_PAY_COUNT | 0 | Tap-to-pay payments, last 90 days. |
| T90_TAP_TO_PAY_AMOUNT | 0.00 | Tap-to-pay payment $, last 90 days. |
| T12M_TOTAL_PAYMENT_AMOUNT | 16907.50 | Total payment volume, trailing 12 months. |
| T12M_CC_AMOUNT | 2120.00 | Credit card payment $, trailing 12 months. |
| T12M_ACH_AMOUNT | 0.00 | ACH payment $, trailing 12 months. |
| T12M_RDC_SUCCESS_AMOUNT | 0.00 | RDC payment $, trailing 12 months. |
| T12M_CHECK_AMOUNT | 0.00 | Check payment $, trailing 12 months. |
| T12M_CASH_AMOUNT | 0.00 | Cash payment $, trailing 12 months. |
| T12M_OTHER_CC_AMOUNT | 31336.28 | Other credit card payment $, trailing 12 months. |
| T12M_ETRANSFER_AMOUNT | 17501.16 | E-transfer payment $, trailing 12 months. |
| T12M_WARRANTY_WORK_AMOUNT | 0.00 | Warranty work payment $, trailing 12 months. |
| T12M_INSTAPAY_AMOUNT | 2120.00 | InstaPay $, trailing 12 months. |
| T12M_ACH_AMOUNT_INSTANT | 0.00 | Instant ACH $, trailing 12 months. |
| CURRENT_WISETACK_STATUS | NULL | Org's current Wisetack financing enrollment status. |
| CURRENTLY_OFFERING_WISETACK | 0 | Whether the org currently offers Wisetack financing to customers. |
| T28_PIPE_OFFERS | 0 | Pipeline financing offers made, last 28 days. |
| T28_PIPE_ACCEPTS | 0 | Pipeline financing offers accepted, last 28 days. |
| T28_STRIPE_OFFERS | 0 | Stripe financing offers made, last 28 days. |
| T28_STRIPE_ACCEPTS | 0 | Stripe financing offers accepted, last 28 days. |
| T1_CUSTOMERS_CREATED | 0 | New end-customers created, last 1 day. |
| T1_CUSTOMER_SERVICE_AGREEMENTS_CREATED | 0 | Service agreements created, last 1 day. |
| T1_CUSTOMER_SERVICE_AGREEMENTS_SENT | 0 | Service agreements sent, last 1 day. |
| T1_ESTIMATES_CREATED | 0 | Estimates created, last 1 day. |
| T1_ESTIMATES_COMPLETED | 0 | Estimates completed/accepted, last 1 day. |
| T1_JOBS_CREATED | 5 | Jobs created, last 1 day. |
| T1_JOBS_COMPLETED | 0 | Jobs completed, last 1 day. |
| T1_INVOICES_SENT | 0 | Invoices sent, last 1 day. |
| T1_INVOICES_PAID | 0 | Invoices paid, last 1 day. |
| T1_JOBS_REVIEWED | 0 | Jobs that received a customer review, last 1 day. |
| T1_MARKETING_CAMPAIGNS_SENT | 0 | Marketing campaigns sent, last 1 day. |
| T1_PAYROLLS_SUCCESSFULLY_PROCESSED | 0 | Payroll runs processed, last 1 day. |
| T1_SALES_PROPOSALS_CREATED | 0 | Sales proposals created, last 1 day. |
| T1_SALES_PROPOSALS_SENT | 0 | Sales proposals sent, last 1 day. |
| T1_QUICKBOOKS_ONLINE_ENGAGEMENTS | 0 | QuickBooks Online sync events, last 1 day. |
| T1_QUICKBOOKS_DESKTOP_ENGAGEMENTS | 0 | QuickBooks Desktop sync events, last 1 day. |
| T7_CUSTOMERS_CREATED | 1 | New end-customers created, last 7 days. |
| T7_CUSTOMER_SERVICE_AGREEMENTS_CREATED | 0 | Service agreements created, last 7 days. |
| T7_CUSTOMER_SERVICE_AGREEMENTS_SENT | 0 | Service agreements sent, last 7 days. |
| T7_ESTIMATES_CREATED | 0 | Estimates created, last 7 days. |
| T7_ESTIMATES_COMPLETED | 0 | Estimates completed/accepted, last 7 days. |
| T7_JOBS_CREATED | 9 | Jobs created, last 7 days. |
| T7_JOBS_COMPLETED | 3 | Jobs completed, last 7 days. |
| T7_INVOICES_SENT | 3 | Invoices sent, last 7 days. |
| T7_INVOICES_PAID | 0 | Invoices paid, last 7 days. |
| T7_JOBS_REVIEWED | 0 | Jobs that received a review, last 7 days. |
| T7_MARKETING_CAMPAIGNS_SENT | 0 | Marketing campaigns sent, last 7 days. |
| T7_PAYROLLS_SUCCESSFULLY_PROCESSED | 0 | Payroll runs processed, last 7 days. |
| T7_SALES_PROPOSALS_CREATED | 0 | Sales proposals created, last 7 days. |
| T7_SALES_PROPOSALS_SENT | 0 | Sales proposals sent, last 7 days. |
| T7_QUICKBOOKS_ONLINE_ENGAGEMENTS | 0 | QuickBooks Online sync events, last 7 days. |
| T7_QUICKBOOKS_DESKTOP_ENGAGEMENTS | 0 | QuickBooks Desktop sync events, last 7 days. |
| T28_CUSTOMERS_CREATED | 1 | New end-customers created, last 28 days. |
| T28_CUSTOMER_SERVICE_AGREEMENTS_CREATED | 0 | Service agreements created, last 28 days. |
| T28_CUSTOMER_SERVICE_AGREEMENTS_SENT | 0 | Service agreements sent, last 28 days. |
| T28_ESTIMATES_CREATED | 0 | Estimates created, last 28 days. |
| T28_ESTIMATES_COMPLETED | 0 | Estimates completed/accepted, last 28 days. |
| T28_JOBS_CREATED | 102 | Jobs created, last 28 days. |
| T28_JOBS_COMPLETED | 18 | Jobs completed, last 28 days. |
| T28_INVOICES_SENT | 18 | Invoices sent, last 28 days. |
| T28_INVOICES_PAID | 17 | Invoices paid, last 28 days. |
| T28_JOBS_REVIEWED | 0 | Jobs that received a review, last 28 days. |
| T28_MARKETING_CAMPAIGNS_SENT | 0 | Marketing campaigns sent, last 28 days. |
| T28_PAYROLLS_SUCCESSFULLY_PROCESSED | 0 | Payroll runs processed, last 28 days. |
| T28_SALES_PROPOSALS_CREATED | 0 | Sales proposals created, last 28 days. |
| T28_SALES_PROPOSALS_SENT | 2 | Sales proposals sent, last 28 days. |
| T28_QUICKBOOKS_ONLINE_ENGAGEMENTS | 0 | QuickBooks Online sync events, last 28 days. |
| T28_QUICKBOOKS_DESKTOP_ENGAGEMENTS | 0 | QuickBooks Desktop sync events, last 28 days. |
| T1_LEADS_CREATED | 0 | New leads created, last 1 day. |
| T7_LEADS_CREATED | 0 | New leads created, last 7 days. |
| T28_LEADS_CREATED | 0 | New leads created, last 28 days. |
| T1_JOBS_SCHEDULED | 5 | Jobs scheduled, last 1 day. |
| T7_JOBS_SCHEDULED | 7 | Jobs scheduled, last 7 days. |
| T28_JOBS_SCHEDULED | 89 | Jobs scheduled, last 28 days. |
| T28_CALLS | 6 | Phone calls logged, last 28 days. |
| T28_EMAILS_SENT | 14 | Emails sent to the org, last 28 days. |
| T28_EMAILS_OPENED | 14 | Emails opened by the org, last 28 days. |
| T28_EMAILS_CLICKED | 0 | Emails clicked by the org, last 28 days. |
| T28_SMS_MESSAGES | 1 | SMS messages exchanged, last 28 days. |
| T28_IN_APP_MESSAGES | 0 | In-app messages sent, last 28 days. |
| T28_PUSH_NOTIFICATIONS | 0 | Push notifications sent, last 28 days. |
| T28_CHAT_CONVERSATIONS | 3 | Chat/support conversations, last 28 days. |
| T28_ZOOM_MEETINGS | 0 | Zoom meetings held, last 28 days. |
| T28_DEMOS_ATTENDED | 0 | Product demos attended, last 28 days. |
| T28_SALES_COMMUNICATIONS | 1 | Communications from Sales function, last 28 days. |
| T28_MARKETING_COMMUNICATIONS | 15 | Communications from Marketing function, last 28 days. |
| T28_CUSTOMER_SUCCESS_COMMUNICATIONS | 8 | Communications from Customer Success function, last 28 days. |
| T28_UNKNOWN_FUNCTION_COMMUNICATIONS | 0 | Communications with unattributed sending function, last 28 days. |
| T1_ORG_SIZE | 1 | Measured org size (users/techs), last 1 day snapshot. |
| T7_ORG_SIZE | 1 | Measured org size, last 7 day snapshot. |
| T28_ORG_SIZE | 1 | Measured org size, last 28 day snapshot. |
| T1_OPEN_INVOICES | 0 | Open/unpaid invoices, last 1 day. |
| T7_OPEN_INVOICES | 0 | Open/unpaid invoices, last 7 days. |
| T28_OPEN_INVOICES | 7 | Open/unpaid invoices, last 28 days. |
| T28_ONBOARDING_CASES_OPENED | 0 | Onboarding support cases opened, last 28 days. |
| T28_ONBOARDING_CASES_CLOSED | 0 | Onboarding support cases closed, last 28 days. |
| CC_PERCENT_FEE | 0.0299 | Effective credit card percent fee (org override > package > default). |
| CC_FIXED_FEE | 0.00 | Effective credit card fixed fee. |
| CC_CARD_PRESENT_PERCENT_FEE | 0.0259 | Effective card-present percent fee. |
| CC_CARD_NOT_PRESENT_PERCENT_FEE | 0.0349 | Effective card-not-present percent fee. |
| CC_INVOICE_PERCENT_FEE | 0.0299 | Effective invoice percent fee. |
| CC_INSTANT_PAYOUT_PERCENT_FEE | 0.0100 | Effective instant payout percent fee. |
| CC_ACH_FIXED_FEE | 0.00 | Effective ACH fixed fee. |
| CC_ACH_PERCENT_FEE | 0.0100 | Effective ACH percent fee. |
| NEXT_BILLING_DATE | 2026-10-01 | Next Core SaaS billing date. |

---

## Part 2 — Communication / Event-Level Rollups (pro-grain, all history)

```sql
select table_schema, table_name, column_name
from marts.information_schema.columns
where lower(column_name) like '%pro_uuid%'
order by table_schema, table_name;

select 'DETAIL_CALLS' as tbl, count(*) as n, min(call_date) as min_dt, max(call_date) as max_dt
from marts.communication.detail_calls where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94'
union all
select 'DETAIL_CHAT_CONVERSATIONS', count(*), min(conversation_created_date), max(conversation_created_date)
from marts.communication.detail_chat_conversations where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94'
union all
select 'DETAIL_COMMUNICATION_LIFECYCLE', count(*), min(comm_date), max(comm_date)
from marts.communication.detail_communication_lifecycle where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94'
union all
select 'DETAIL_DEMO_TRANSACTION', count(*), min(demo_action_date), max(demo_action_date)
from marts.communication.detail_demo_transaction where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94'
union all
select 'DETAIL_EMAILS', count(*), min(message_date), max(message_date)
from marts.communication.detail_emails where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94'
union all
select 'DETAIL_IN_APP_MESSAGES', count(*), min(message_date), max(message_date)
from marts.communication.detail_in_app_messages where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94'
union all
select 'DETAIL_PUSH_NOTIFICATIONS', count(*), min(message_date), max(message_date)
from marts.communication.detail_push_notifications where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94'
union all
select 'DETAIL_SMS', count(*), min(message_date), max(message_date)
from marts.communication.detail_sms where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94'
union all
select 'DETAIL_ZOOM', count(*), min(call_date), max(call_date)
from marts.communication.detail_zoom where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94';

select object_construct(*) as latest_call
from marts.communication.detail_calls
where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94'
qualify row_number() over (order by call_start_dt desc) = 1;
-- same pattern used for chat, demo, sms, email, in-app, push

select
    count(*) as total_emails
  , sum(emails_sent_flag) as emails_sent
  , sum(emails_opened_flag) as emails_opened
  , sum(emails_clicked_flag) as emails_clicked
  , sum(emails_bounced_flag) as emails_bounced
  , sum(emails_unsubscribed_flag) as emails_unsubscribed
from marts.communication.detail_emails
where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94';
-- same pattern used for sms, calls, chat, demo

select count(*) as n, max(response_dt) as last_response, max_by(score, response_dt) as last_score
from marts.customer_success.detail_nps_surveys
where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94';

select count(*) as n from marts.communication.detail_workflow_entries
where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94';

select count(*) as n from marts.communication.detail_workflow_progress
where pro_uuid = 'pro_3be6e3fb554642da830a286684632c94';
```

| Variable | Value | Description |
|---|---|---|
| CALLS_TOTAL_COUNT | 35 | Total logged calls involving this pro, all history. |
| CALLS_FIRST_DATE | 2026-05-14 | Date of the earliest logged call. |
| CALLS_LAST_DATE | 2026-08-12 | Date of the most recent logged call. |
| CALLS_CONNECTED_COUNT | 9 | Calls that connected (someone picked up). |
| CALLS_CONTACTED_COUNT | 6 | Calls that reached the actual contact. |
| CALLS_CONVERSATION_COUNT | 3 | Calls that became a real two-way conversation. |
| CALLS_ABANDONED_COUNT | 0 | Calls abandoned before being answered. |
| CALLS_DNC_FLAG_COUNT | 0 | Calls flagged as Do-Not-Call violations. |
| CALLS_INCOMING_COUNT | 0 | Calls that were inbound (pro-initiated). |
| CALLS_AVG_DURATION_SEC | 30.0 | Average call duration in seconds. |
| CALLS_LAST_DISPOSITION | 3. Left Voicemail | Outcome code of the most recent call. |
| CALLS_LAST_SENTIMENT | NULL | Sentiment score/label of the most recent call, if captured. |
| CALLS_LAST_FUNCTION | customer success | Business function that made the most recent call. |
| CALLS_LAST_DIRECTION | outgoing | Direction of the most recent call. |
| CHATS_TOTAL_COUNT | 7 | Total support/chat conversations, all history. |
| CHATS_FIRST_DATE | 2026-08-05 | Date of the earliest chat conversation. |
| CHATS_LAST_DATE | 2026-08-14 | Date of the most recent chat conversation. |
| CHATS_AVG_RATING | NULL | Average customer satisfaction rating left on chats (none rated yet). |
| CHATS_SNOOZED_COUNT | 0 | Chats that were snoozed at least once. |
| CHATS_SOLOED_COUNT | 4 | Chats handled solo (single agent, no handoff). |
| CHATS_LAST_STATUS | closed | Status of the most recent chat conversation. |
| CHATS_LAST_TAGS | #Max-PRO, Reassign: Fintech | Tags applied to the most recent chat conversation. |
| DEMOS_TOTAL_COUNT | 11 | Total demo-related transaction rows, all history. |
| DEMOS_CREATED_COUNT | 6 | Demos created. |
| DEMOS_ATTENDED_COUNT | 5 | Demos actually attended. |
| DEMOS_MISSED_COUNT | 0 | Demos missed (no-show). |
| DEMOS_CANCELLED_COUNT | 0 | Demos cancelled. |
| DEMOS_WON_COUNT | 4 | Demos that resulted in a "won" outcome. |
| DEMOS_LAST_ACTION | Demo Created | Most recent demo lifecycle action. |
| DEMOS_LAST_STATUS | Attended | Status of the most recent demo record. |
| DEMOS_LAST_RECORD_TYPE | Fintech Coaching | Category of the most recent demo. |
| EMAILS_TOTAL_COUNT | 80 | Total marketing/lifecycle emails sent to this pro, all history. |
| EMAILS_SENT_COUNT | 80 | Emails successfully sent. |
| EMAILS_OPENED_COUNT | 45 | Emails opened. |
| EMAILS_CLICKED_COUNT | 0 | Emails clicked. |
| EMAILS_BOUNCED_COUNT | 0 | Emails that bounced. |
| EMAILS_UNSUBSCRIBED_COUNT | 0 | Emails after which the pro unsubscribed. |
| EMAILS_FIRST_DATE | 2026-05-14 | Date of the earliest email. |
| EMAILS_LAST_DATE | 2026-09-02 | Date of the most recent email. |
| EMAILS_LAST_CAMPAIGN | 09_26_enrolled_ga_en_inactive_audience_e1 | Campaign of the most recent email. |
| EMAILS_LAST_OPENED_FLAG | 1 | Whether the most recent email was opened. |
| SMS_TOTAL_COUNT | 12 | Total SMS messages exchanged with this pro, all history. |
| SMS_SENT_COUNT | 12 | SMS successfully sent. |
| SMS_DELIVERED_COUNT | 12 | SMS successfully delivered. |
| SMS_DELIVERY_FAILED_COUNT | 0 | SMS that failed to deliver. |
| SMS_HAS_RESPONSE_COUNT | 6 | SMS threads where the pro responded. |
| SMS_OPT_OUT_COUNT | 0 | SMS after which the pro opted out. |
| SMS_INCOMING_COUNT | 0 | SMS messages initiated by the pro (inbound). |
| SMS_FIRST_DATE | 2026-05-18 | Date of the earliest SMS. |
| SMS_LAST_DATE | 2026-08-14 | Date of the most recent SMS. |
| SMS_LAST_CAMPAIGN | 07_26_enrolled_ga_top_50_last_chance_reminder_sms | Campaign of the most recent SMS. |
| SMS_LAST_DELIVERED_FLAG | 1 | Whether the most recent SMS delivered successfully. |
| IN_APP_MESSAGES_TOTAL_COUNT | 1 | Total in-app messages received, all history. |
| IN_APP_MESSAGES_LAST_DATE | 2026-07-24 | Date of the only/most recent in-app message. |
| IN_APP_MESSAGES_LAST_OPENED_FLAG | 1 | Whether the most recent in-app message was opened. |
| IN_APP_MESSAGES_LAST_CLICKED_FLAG | 1 | Whether the most recent in-app message was clicked. |
| PUSH_NOTIFICATIONS_TOTAL_COUNT | 1 | Total push notifications received, all history. |
| PUSH_NOTIFICATIONS_LAST_DATE | 2026-07-19 | Date of the only/most recent push notification. |
| PUSH_NOTIFICATIONS_LAST_OPENED_FLAG | 0 | Whether the most recent push notification was opened. |
| ZOOM_MEETINGS_TOTAL_COUNT | 0 | Total Zoom meetings, all history (none recorded). |
| COMMUNICATION_LIFECYCLE_TOTAL_COUNT | 141 | Total unified cross-channel communication events, all history. |
| COMMUNICATION_LIFECYCLE_FIRST_DATE | 2026-05-14 | Date of the earliest unified communication event. |
| COMMUNICATION_LIFECYCLE_LAST_DATE | 2026-09-02 | Date of the most recent unified communication event. |
| NPS_SURVEYS_TOTAL_COUNT | 0 | NPS surveys sent to this pro, all history (none sent yet). |
| NPS_LAST_SCORE | NULL | Score from the most recent NPS response (none to date). |
| NPS_LAST_RESPONSE_DATE | NULL | Date of the most recent NPS response (none to date). |
| WORKFLOW_ENTRIES_TOTAL_COUNT | 10 | Total distinct marketing/lifecycle workflow entries (sessions). |
| WORKFLOW_PROGRESS_STEPS_TOTAL_COUNT | 26 | Total distinct workflow campaign steps reached across all sessions. |

Note: `MESSAGE_TEXT`, `EMAIL_SUBJECT`, `CAMPAIGN_NAME`, `TAG_NAMES` carry actual message copy — internal-use only per Snowflake terms.

## Known gaps

- No pro-grain "everything" table exists in MARTS — Part 2 rollups were built ad hoc.
- Zoom = 0 rows for this pro; demo table shows 5 attended demos, so worth a sanity check.
- NPS = 0 rows — never surveyed (or not yet resolvable to this `pro_uuid`).
- `CALL_SENTIMENT` and chat rating columns exist but are null for this pro.
- Part 1 (org) and Part 2 (pro) are different grains — org may have more than one pro.

---

## Part 3 — PRODUCTION Views

Scope: CHURN, CUSTOMER_SUCCESS, LTV, RECO, MARKETING, SALES, SALESFORCE_PIPES, FINTECH, LEAD_MATCHING, SALES_OPPORTUNITIES, PRODUCT. Excluded: Greenhouse, AI_USAGE, APPLE_SUBSCRIPTION, ANALYSIS, and `_COPY`/`_HIST`/`_BKP_*`/dated-snapshot tables.

```sql
select table_schema, table_name, column_name
from production.information_schema.columns
where lower(column_name) like '%pro_uuid%' or lower(column_name) = 'organization_id'
order by table_schema, table_name;

select count(*) n from production.churn.cancellation_risk where organization_id = 889901;
-- repeated for every candidate table in the agreed schemas

select object_construct(*) v from production.customer_success.health_index_yesterday where organization_id = 889901;

select object_construct(*) v from production.churn.next_bill_churn_by_orgday where organization_id = 889901
qualify row_number() over (order by date desc) = 1;

select array_agg(object_construct(*)) v from production.customer_success.plan_changes where organization_id = 889901;
```

### Broken / inaccessible views
- `production.fintech.ach_instrumentation` — ambiguous column `CHARGE_TYPE`
- `production.sales.dim_salesforce_lead` — numeric overflow error
- `production.sales.leads_reporting` — ambiguous column `TOTAL_OUTGOING_CALLS`
- `production.product.job_inbox_opportunity` — unsupported subquery in view
- `production.marketing.fact_customer_email_attribution` — no `touch_date` column, not re-pulled
- `production.reco.total_list_reco_by_orgday`, `total_list_reco_with_churn`, `sales_opportunities.cc_usage` — count(*) returned rows, detail pull returned 0; flaky, unresolved

### RECO schema

`production.reco.channel_recommendations` (pro-grain):

| Variable | Value | Description |
|---|---|---|
| RECOMMENDED_ACTION | email | Best channel to contact this pro through. |
| BEST_CHANNEL_BY_PCT | email | Channel with highest in-channel response %. |
| P_EMAIL / P_SMS / P_CALL | 0.227 / 0.129 / 0.193 | Modeled probability of engagement by channel. |
| P_RESPONSE_RAW_EMAIL / SMS / CALL | 0.538 / 0.335 / 0.478 | Raw response rate by channel. |
| RESP_PCT_IN_CHANNEL_EMAIL / SMS / CALL | 0.970 / 0.515 / 0.811 | Share of all responses attributable to that channel. |
| P_OPTOUT_EMAIL | 0.102 | Probability this pro opts out if emailed. |
| EMAIL_OPTOUT_SUPPRESSED | false | Whether email is currently suppressed due to opt-out. |
| EMAIL_FATIGUE_TIER / SMS_FATIGUE_TIER / CALL_FATIGUE_TIER | 1 / 0 / 0 | Messaging-fatigue tier per channel. |
| CALL_PRIORITY / CALL_UPLIFT | 0.080 / 0.080 | Modeled value of prioritizing a call. |
| BEST_CHEAP | 0.227 | Best low-cost-channel score. |
| IS_ADMIN | 1 | Whether this pro is an org admin. |
| IS_FOUNDING_PRO | 1 | Whether this pro was the founding/first user on the org. |
| ORG_SIZE_BIN | 1 | Org size bucket used by the model. |
| SEGMENT_ACTUAL | 1A | Customer segment used by the model. |
| SCORING_DATE | 2026-09-08 | Date this recommendation was scored. |

`production.reco.product_family_reco` (org-grain, reco_date 2026-09-07):

| Variable | Value | Description |
|---|---|---|
| TOP_RECO | SMS Number | Model's single top product recommendation. |
| FEATURES_USED | Jobs (41), Scheduling (22), Recurring Jobs (16), Invoicing (15), Price book (13), global_search (11), Job cost (9), Team Chat (8), attachments (7), Sales proposal (6), Estimate (5), Customers (4), Pipeline (3), Reporting (3), Advanced Checklists (2), + 10 features used once | Feature usage counts feeding the reco. |
| RECO_ALGORITHM | unified_cf_with_category_limits_v2 | Algorithm/model name. |

`production.reco.sequenceflow_reco` (org-grain, reco_date 2026-09-07):

| Rank | Feature | Type |
|---|---|---|
| 1 | travel and time on jobs | Feature |
| 2 | video uploads mobile | Feature |
| 3 | review requests | Feature |
| 4 | card on file | Add-On |
| 5 | ach payments | Add-On |
| 6 | sms | Feature |
| 7 | customers portal | Feature |
| 8 | employee clock in/out on mobile | Feature |
| 9 | get started | Feature |
| 10 | progress invoicing | Feature |

`production.reco.unified_recommendations` (org-grain, reco_date 2026-09-07):

| Rank | Feature | Rec type | Unified score | Expected value ($) | Incremental revenue ($) | Peer adoption rate |
|---|---|---|---|---|---|---|
| 1 | Customer review management | adopt | 0.835 | 150.39 | 221.56 | 76.8% |
| 2 | SMS Number | adopt | 0.755 | 242.95 | 482.78 | 50.3% |
| 3 | Employee time tracking | adopt | 0.496 | 49.30 | 110.78 | 58.2% |
| 4 | Service agreements | reactivate | 0.274 | 11.58 | 44.23 | 60.0% |
| 5 | Online booking | reactivate | 0.209 | 7.33 | 36.47 | 35.5% |
| 6 | visual_price_book | adopt | 0.054 | 5.04 | 103.97 | 16.9% |
| 7 | Flat rate pricing | adopt | 0.043 | 3.99 | 103.97 | 10.2% |
| 8 | Advanced Checklists | expand | 0.001 | 0.17 | 220.82 | 40.7% |

### CHURN schema

| Variable | Value | Description |
|---|---|---|
| CHURN_ROOT_CAUSES_PREDICTIONS.CHURN_PROBA | 0.6086 | Short-horizon churn-root-cause model's probability. |
| LONG_TERM_CHURN_PREDICTIONS.CHURN_PROBA | 0.7610 | Long-term churn model's probability. |
| NEXT_BILL_CHURN.CHURN_PROB | 0.121 | Probability of churning before next bill. |
| NEXT_BILL_CHURN.CHURN_RISK | low | Categorical risk label for next-bill churn. |
| NEXT_BILL_CHURN.VERSION | 2024-03-13 | Model version. |

Top SHAP drivers, short-horizon churn model (`CHURN_ROOT_CAUSES_EXPLANATIONS`, base_value -3.11, ~200 features total):

| Feature | Force | Value |
|---|---|---|
| jobs_zero_months_12m | +1.029 | 9 |
| total_discounts_applied | +0.487 | $20,932 |
| composite_growth_score | +0.373 | 20 |
| estimates | +0.313 | 5 |
| jobs_paid_ratio_30d | +0.220 | 0.19 |
| recurring_job | +0.215 | 155 |
| days_since_last_chat | +0.194 | 25 |
| jobs_completed_90d | -0.177 | 55 |
| had_price_decrease | +0.165 | 1 |
| org_size_max | +0.133 | 2 |

Top SHAP drivers, long-term churn model (`LONG_TERM_CHURN_EXPLANATIONS`, base_value -3.22):

| Feature | Force | Value |
|---|---|---|
| activity_last_180_excl_90d | +0.449 | 0 |
| estimates | +0.356 | 5 |
| observation_month | -0.352 | 0 |
| days_since_last_billing_failure | +0.313 | 8 |
| org_size_max | +0.291 | 2 |
| total_discounts_applied | +0.256 | $20,932 |
| jobs_zero_months_12m | +0.232 | 9 |
| composite_growth_score | +0.223 | 20 |
| jobs_paid_ratio_30d | +0.189 | 0.19 |
| promo_ends_next_30d_flag | +0.167 | 1 |

Top SHAP drivers, next-bill churn model (`NEXT_BILL_CHURN_EXPLANATIONS`, scoring date 2026-09-08, ~69 features total):

| Measure | Force | Value |
|---|---|---|
| NUM_OPT__DAYS_IN_FAILED_STATE_NEW | +1.821 | 37.0 |
| CAT__BILLING_STATUS_HISTORICAL | +0.513 | failed |
| NUM_OPT__DAYS_SINCE_LAST_CHAT | +0.634 | nan (never chatted) |
| NUM__JOB_INVOICE_SENT_SCORE_NEW | +0.323 | 0.0 |
| NUM__JOB_CREATED_SCORE_NEW | -0.177 | 94.09 |
| NUM__JOB_PAID_SCORE_NEW | -0.129 | 56.73 |
| NUM__DAYS_SINCE_ENROLLMENT | +0.151 | 68 |
| NUM__JOB_CREATED_FROM_ESTIMATE_SCORE_NEW | +0.112 | 0.0 |
| CAT__PLAN_TIER | -0.045 | starter |
| NUM__CUMULATIVE_DISCOUNT_PERCENT_NEW | +0.035 | 0.30 |

`production.churn.retention_briefs.OUTREACH_SPEECH` (generated retention-call script, verbatim, PROMPT_HASH `dee9084a...`):
> **Primary risk** — Inactive 9 of last 12 months, only 19% of jobs in the last 30 days resulted in a paid transaction.
> **Why it's urgent** — 61% 30-day churn risk with near-zero job activity as baseline.
> **Key risk signals** — 9/12 months with zero jobs; $20,900+ in discounts applied; composite growth score of 20.
> **In their favor** — 22 customers active in the last 31–90 days; activity depth ratio of 1.54.
> **Suggested question** — *"When you think about how your business has changed since you signed up, what's been the biggest thing getting in the way of running jobs through the platform consistently?"*

### CUSTOMER_SUCCESS schema

`production.customer_success.health_index_yesterday` (bizdate 2026-09-07):

| Variable | Value | Description |
|---|---|---|
| AGG_GRADE / AGG_GRADE_SIMPLIFIED | B / Green | Overall health grade. |
| AGG_SCORE | 72.13 | Overall health score (0–100). |
| BILLING_GRADE / BILLING_SCORE | B / 71.66 | Billing-health sub-grade/score. |
| REASON_BILLING_GRADE | "successful on most recent bill; 20-40% lifetime failed bills; over 75% discounted" | Why the billing grade is what it is. |
| JOB_ACTIVITY_GRADE / JOB_ACTIVITY_SCORE | B / 70 | Job-activity sub-grade/score. |
| REASON_JOB_ACTIVITY_GRADE | "active 30-45% of days l14; activity increased by 5-15%" | Why the job-activity grade is what it is. |
| PLAN_GRADE / PLAN_SCORE | B / 74.80 | Plan-fit sub-grade/score. |
| REASON_PLAN_GRADE | "Basic; 62-91; good plan fit; 0-1 orgsize; Mechanical; Recent Downsell" | Why the plan grade is what it is. |
| UPSELL_HEALTH_GRADE / UPSELL_SCORE | Bronze / 27.52 | Upsell-readiness grade/score. |
| HAS_OPEN_CANCEL_REQUEST | 0 | Whether a cancellation request is currently open. |
| PRIOR_CANCEL_REQUEST_COUNT | 0 | Number of past cancellation requests. |
| SCORING_MODEL_VERSION | 2026-08-19 | Model version. |

`production.customer_success.plan_changes` (3 rows):

| Date | Event | Gross MRR change |
|---|---|---|
| 2026-07-01 | Enrollment onto "large" tier | $0 → $567 |
| 2026-08-31 | Downsell to "starter" tier | $567 → $152 |
| 2026-09-01 | Feature downsell (most recent) | $152 → $79 |

`production.customer_success.retention_by_orgday` (latest, 2026-09-07):

| Variable | Value | Description |
|---|---|---|
| CHURN | 0 | Not churned as of this day. |
| JOB_CREATED_ACTIVE / JOB_SCHEDULED_ACTIVE | 1 / 1 | Job created/scheduled today. |
| JOB_COMPLETED_ACTIVE | 0 | No job completed today. |
| INVOICE_SENT_ACTIVE / ESTIMATE_CREATED_ACTIVE | 0 / 0 | No invoice sent or estimate created today. |
| CC_ACTIVE | 0 | No credit-card transaction today. |
| CC_60_ATTACH_BINARY | 1 | Credit card attached within 60 days of enrollment. |

`production.customer_success.sa_revenue_cohorts_and_payback_period` (Instapay Attach, attached 2026-07-13):

| Variable | Value | Description |
|---|---|---|
| FIRST_ENGAGEMENT_SCORE / SECOND_ENGAGEMENT_SCORE | Red 2 / Red | Engagement grade at first/second checkpoint. |
| REVENUE / TRANSACTIONS | $0 / 0 | Revenue and transaction count from this attach. |
| MONTHS_ATTACH_TO_REVENUE | 0 | Months from attach to first revenue (none yet). |

`production.customer_success.touches_and_conversions`: 245 total CS touches, 2025-09-28 → 2026-09-02 (predates the org's 2026-07-01 enrollment date — likely includes pre-enrollment/lead-stage touches).

### LTV schema

`production.ltv.lead_ltv_v2_enrollment`:

| Variable | Value | Description |
|---|---|---|
| LEAD_LTV | $18,569.39 | 5-year projected lifetime value. |
| LTV_SAAS / LTV_FINTECH / LTV_ADDONS | $5,505 / $9,744 / $3,320 | LTV split by revenue source. |
| RETENTION_RATE (month 1 / month 60) | 93.6% / 50.0% | Modeled retention curve endpoints. |

Full month-by-month and year-by-year LTV arrays (60-month, 5-year) exist per revenue source in the raw JSON.

### MARKETING schema

`production.marketing.marketing_attribution_analytics` (2 touches):

| Touch | Date | Channel |
|---|---|---|
| 1 (first) | 2026-05-14 | chatgpt.com (organic direct), pre-enrollment |
| 2 (last) | 2026-08-14 | inbound call, UTM campaign "magenta" |

`production.marketing.touch_matrix`: single-touch attribution — `organicdirect`, "Only Attribution", 2026-05-14.

### SALES / SALESFORCE_PIPES schemas

`production.sales.promotions` (latest, 2026-09-01):

| Variable | Value |
|---|---|
| PROMOTION_ACTION | promo_redeemed |
| PROMOTION_CATEGORY | Clearing Billing Debt |
| PROMOTION_TYPE | marketing center campaigns flat rate |
| PROMOTION_AMOUNT | $9 |
| SALES_PERSON | Jessica Benally |

`production.salesforce_pipes.salesforce_account_metrics`:

| Variable | Value | Description |
|---|---|---|
| ACCOUNT_RISK_CATEGORY__C | high_risk | Salesforce-side account risk tier. |
| ACCOUNT_RISK_SCORE__C | 0.185 | Composite account risk score. |
| ACCOUNT_RISK_REASONS__C | (24 weighted reasons — full breakdown below) | Weighted breakdown of what's driving the account risk score. |
| LIT_CHURN_PROBABILITY__C / LIT_CHURN_RISK__C | 0.121 / low | Same next-bill score, surfaced to reps. |
| ADD_ON_RECOMMENDATIONS__C | ACH Attach, Payroll, Marketing Campaigns | Add-on products reps are shown. |
| FEATURE_RECOMMENDATIONS__C | travel and time on jobs, video uploads mobile, review requests | Feature recs shown to reps. |
| FEATURE_USAGE__C | Jobs (39), Scheduling (20), Invoicing (15), ... | Feature usage summary string. |
| ELTV__C | $13,387.06 | Lead-level LTV shown to reps. |
| NUMBER_OF_UNSCHEDULED_JOBS__C | 11 | Jobs without a schedule assigned. |
| SCHEDULED_JOBS_NEXT_30_DAYS__C | 6 | Jobs scheduled in the next 30 days. |
| TRAILING_7_JOBS_COMPLETED_TREND__C / TRAILING_7_JOBS_SCHEDULED_TREND__C | DOWN / UP | Week-over-week trend direction. |
| ESTIMATES_CREATED_T7_TREND__C | DOWN | Estimate creation trend. |
| WISETACK_LENDER_STATUS__C | PENDING | Wisetack financing approval status. |
| QUICKBOOKS__C | FALSE | QuickBooks integration status (disconnected). |
| SMS_NUMBER__C | FALSE | Whether SMS Number is provisioned. |
| SUPER_PRO__C | FALSE | Superpro flag. |
| STRIPE_INSTANT_LIFETIME_GMV__C / STRIPE_STANDARD_LIFETIME_GMV__C | $2,120 / $2,120 | Lifetime GMV by Stripe account type. |
| LAST_LOGIN_DATE__C | 2026-08-31 | Last product login. |
| LAST_SUCCESSFUL_BILLING__C | 2026-09-01 | Last successful billing charge. |

`ACCOUNT_RISK_REASONS__C` full breakdown (24 weighted reasons, positive weight = pushes risk score up, negative = pushes it down):

| Reason | Weight | Description |
|---|---|---|
| Failing Business: Financial Health | 0.91 | Largest single driver of this org's risk score — financial-health signals are pushing risk up hardest. |
| Account Risk | 0.63 | A general account-risk sub-signal, second-largest contributor. |
| Failing Business: Deposits and Completed Jobs | 0.28 | Low deposit/completed-job volume is contributing to the risk score. |
| Business Health: First Invoice Age | 0.14 | How long it took this org to send its first invoice is contributing to the risk score. |
| Communication Activity | 0.13 | Level of communication activity is contributing to the risk score. |
| Failing Business: Processing History | 0.10 | Payment-processing history is contributing to the risk score. |
| Account Age and Company Size | 0.06 | Org's age and declared size are contributing slightly to the risk score. |
| Reputation | 0.05 | Reputation signal is contributing slightly to the risk score. |
| Failed Billing | 0.03 | Failed billing attempts are contributing marginally to the risk score. |
| External Scores | 0.01 | Third-party/external scoring data is contributing marginally to the risk score. |
| Account Maturity | 0.00 | Account maturity has no measurable effect on the risk score for this org. |
| Dispute Ratio | 0.00 | Dispute ratio has no measurable effect on the risk score for this org. |
| Fraud Risk Score | 0.00 | Fraud risk score has no measurable effect on the risk score for this org. |
| Business Health: Job Schedule | 0.00 | Job-scheduling health has no measurable effect on the risk score for this org. |
| Castle address mismatch | 0.00 | Address-verification mismatch has no measurable effect on the risk score for this org. |
| Instapay Ratio | 0.00 | InstaPay usage ratio has no measurable effect on the risk score for this org. |
| Engagement with Marketing Emails and CS Calls | 0.00 | Marketing/CS engagement has no measurable effect on the risk score for this org. |
| GMV Acceleration | 0.00 | GMV growth rate has no measurable effect on the risk score for this org. |
| Failed Payment | 0.00 | Failed payments have no measurable effect on the risk score for this org. |
| Failing Business: Dispute History | -0.00 | Dispute history is negligibly pulling the risk score down. |
| Payment Efficiency | -0.01 | Payment efficiency is pulling the risk score down slightly. |
| Enrollment | -0.02 | Enrollment status/recency is pulling the risk score down slightly. |
| Financial Health | -0.05 | A second, separate financial-health signal is pulling the risk score down. |
| Processing History | -0.17 | Processing history (distinct from the "Failing Business" version above) is pulling the risk score down the most of any factor. |

### Not pulled in this pass
FINTECH daily-activity tables (`consumer_financing_instrumentation`, `consumer_financing_v2`, `credit_card_activity`, `expense_card_instrumentation`, `fintech_revenue`, `moving_sum_gmv_by_orgday`, `payroll_instrumentation_billing`, `payroll_instrumentation_payee_volume`, `risk_ops_activity`, `stripe_connected_account_loss_collection_day`) — 25–88 rows each, daily time series, not pulled in detail. `production.reco.account_reco_top5` and `channel_recommendations_org` returned 0 rows on detail pull despite earlier existence checks.

---

## Addendum — health / churn / usage models, consolidated

```sql
select table_schema, table_name, comment
from production.information_schema.tables
where table_name ilike any ('%health%','%engagement%','%usage%','%adoption%','%receptiv%','%churn%','%risk%','%activity%')
order by table_schema, table_name;

select object_construct(*) v from production.payments_churn.payments_churn_score where org_id = 889901
qualify row_number() over (order by date desc) = 1;
-- 0 rows

select object_construct(*) v from production.payments_churn.payments_churn_score_new where org_id = 889901
qualify row_number() over (order by date desc) = 1;
-- 0 rows

select organization_id, bizdate, plan_tier, segment_actual, months_enrolled, total_mrr, features
from production.ltv.ltv_feature_adoption_scores
where organization_id = 889901
qualify row_number() over (order by bizdate desc) = 1;
```

| Source | Metric | Value | Description |
|---|---|---|---|
| `churn.churn_root_causes_predictions` | CHURN_PROBA | 0.6086 | Short-horizon churn model's probability. |
| `churn.long_term_churn_predictions` | CHURN_PROBA | 0.7610 | Long-horizon churn model's probability. |
| `churn.next_bill_churn` | CHURN_PROB / CHURN_RISK | 0.121 / low | Probability of not renewing at next bill. |
| `salesforce_pipes.salesforce_account_metrics` | LIT_CHURN_PROBABILITY__C / RISK__C | 0.121 / low | Same next-bill score, rep-facing. |
| `salesforce_pipes.salesforce_account_metrics` | ACCOUNT_RISK_SCORE__C / CATEGORY | 0.185 / high_risk | Separate composite account-risk model. |
| `customer_success.health_index_yesterday` | AGG_SCORE / AGG_GRADE | 72.13 / B (Green) | Overall product-health score. |
| `customer_success.health_index_yesterday` | JOB_ACTIVITY_SCORE / GRADE | 70 / B | Usage sub-score. |
| `customer_success.health_index_yesterday` | BILLING_SCORE / GRADE | 71.66 / B | Billing-health sub-score. |
| `customer_success.health_index_yesterday` | UPSELL_SCORE / GRADE | 27.52 / Bronze | Upsell-readiness sub-score. |
| `reco.channel_recommendations` | P_EMAIL / P_SMS / P_CALL | 0.227 / 0.129 / 0.193 | Modeled receptivity by channel. |
| `reco.channel_recommendations` | EMAIL_FATIGUE_TIER | 1 (of 3) | Messaging-fatigue tier for email. |
| `payments_churn.payments_churn_score(_new)` | — | no data | No payments-specific churn score exists for this org. |

`production.ltv.ltv_feature_adoption_scores` (latest bizdate 2026-09-07, months_enrolled=2.2, total_mrr=$144.70) — per-feature churn/retention-lift model:

| Feature | Churn prob. with | Churn prob. without | Retention lift (pp) | Incremental revenue ($) | Description |
|---|---|---|---|---|---|
| payroll | 0.1565 | 0.3907 | +23.41 | 689.47 | Adopting payroll is modeled to cut this org's churn probability by 23.41pp, the largest lift of any feature. |
| cc_payments | 0.3665 | 0.5477 | +18.12 | 533.56 | Adopting credit-card payments is modeled to cut churn probability by 18.12pp. |
| sms_number | 0.3138 | 0.4778 | +16.40 | 482.78 | Adopting an SMS number is modeled to cut churn probability by 16.40pp. |
| review_requests | 0.3445 | 0.4198 | +7.52 | 221.56 | Adopting review requests is modeled to cut churn probability by 7.52pp. |
| checklists | 0.3631 | 0.4381 | +7.50 | 220.82 | Adopting checklists is modeled to cut churn probability by 7.50pp. |
| website | 0.3103 | 0.3412 | +3.09 | 90.84 | Adopting the website product is modeled to cut churn probability by 3.09pp. |
| profit_rhino | 0.3558 | 0.3649 | +0.91 | 26.70 | Adopting Profit Rhino pricing is modeled to cut churn probability by 0.91pp. |
| quickbooks | 0.5321 | 0.3495 | 0.00 | 0 | Model shows churn probability higher with QuickBooks than without; no retention lift or revenue attributed — likely unscored due to low usage signal, not a confirmed negative effect. |
| service_agreements | 0.5084 | 0.3642 | 0.00 | 0 | Model shows churn probability higher with Service Agreements than without; no retention lift attributed. |
| employee_time_tracking | 0.4802 | 0.4074 | 0.00 | 0 | Model shows churn probability higher with Employee Time Tracking than without; no retention lift attributed. |
| property_profile | 0.7310 | 0.4051 | 0.00 | 0 | Model shows churn probability much higher with Property Profile than without; no retention lift attributed. |
| online_booking | 0.4408 | 0.3355 | 0.00 | 0 | Model shows churn probability higher with Online Booking than without; no retention lift attributed. |
| customer_intake_flow | 0.5415 | 0.3277 | 0.00 | 0 | Model shows churn probability higher with Customer Intake Flow than without; no retention lift attributed. |
| flat_rate_pricing | 0.3975 | 0.3635 | 0.00 | 0 | Model shows churn probability higher with Flat Rate Pricing than without; no retention lift attributed. |
| visual_price_book | 0.3975 | 0.3635 | 0.00 | 0 | Same churn probabilities as flat_rate_pricing (likely correlated/bundled features); no retention lift attributed. |
| voip | 0.6251 | 0.3311 | 0.00 | 0 | Model shows churn probability much higher with VoIP than without; no retention lift attributed. |
| hcp_assist | 0.3671 | 0.2956 | 0.00 | 0 | Model shows churn probability higher with HCP Assist than without; no retention lift attributed. |
| hcp_cam | 0.4537 | 0.3764 | 0.00 | 0 | Model shows churn probability higher with HCP Cam than without; no retention lift attributed. |
| vehicle_gps | 0.8764 | 0.3902 | 0.00 | 0 | Model shows churn probability much higher with Vehicle GPS than without; no retention lift attributed. |
| accounting | 0.3977 | 0.3880 | 0.00 | 0 | Model shows churn probability marginally higher with Accounting integration than without; no retention lift attributed. |
| conquer | 0.8970 | 0.3603 | 0.00 | 0 | Model shows churn probability much higher with Conquer than without; no retention lift attributed. |
| ach_payments | 0.5435 | 0.3938 | 0.00 | 0 | Model shows churn probability higher with ACH Payments than without; no retention lift attributed. |
| consumer_financing | 0.6240 | 0.3429 | 0.00 | 0 | Model shows churn probability higher with Consumer Financing than without; no retention lift attributed. |

Note: every feature below `profit_rhino` shows 0.00pp retention lift despite some showing large with/without probability swings (e.g. conquer, vehicle_gps) — this pattern (real probability delta, zero lift/revenue) indicates the model has too little usage data on this org for these features to be scored, not that they were tested and found to have no effect.
