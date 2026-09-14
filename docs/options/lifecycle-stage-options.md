# Lifecycle-stage options working document

**Status:** Working draft for async review  
**Audience:** Jake, Leanne, Dan, Greg, Shelby, Hammer, and the supporting data science team  
**Purpose:** Decide where to test the Waypoint/recommendation approach first, whether more than one lifecycle stage can be tested in parallel, and what inputs are needed to make the test credible.

## 1. Decision to make

We need to choose the narrowest lifecycle scope in which the model can produce a fair, measurable test. The current candidates are:

1. **Trial and pre-enroll** — improve conversion from pre-funnel interest into an active user/customer.
2. **Onboarding** — improve early product adoption after enrollment.
3. **At-risk pros** — intervene when a current pro shows evidence of declining health or engagement.

The immediate strategic choice is also between two input paths:

| Option | Description | Near-term benefit | Main risk |
|---|---|---|---|
| **Option 1: Waypoint-selected inputs** | Jake and Leanne select the data made available to the auto-researcher/Waypoint, define the target metric, and evaluate whether the resulting recommendation loop improves outcomes. | Gives the team control over the inputs while ContextLayer is still incomplete or evolving. | We may spend effort rebuilding a recommendation table or learning system that ContextLayer will eventually provide. |
| **Option 2: ContextLayer-first** | Consume the inputs already available in ContextLayer and build the loop on top of that system. | Avoids duplicating infrastructure and tests the long-term architecture sooner. | Current ContextLayer coverage may not include the fields or outcomes needed for a useful test. |

**Working recommendation:** Use Option 1 for the initial validation if it can be kept narrow and reversible, while documenting the input contract so the work can later consume ContextLayer. This is a validation path, not a commitment to maintain a parallel data product. The two options should converge as ContextLayer becomes more complete.

## 2. Candidate lifecycle stages

### A. Trial and pre-enroll

**Why consider it**

- It may address the “leaky bucket” before a prospect becomes a user.
- Greg and Shelby identified this as a potentially high-impact area.
- There may be fewer overlapping customer communications than in onboarding, although this needs to be verified.

**Core question**

Can the model use the smaller amount of pre-enroll information effectively enough to change conversion behavior?

**Proposed success metric**

- **Primary:** Incremental conversion to the agreed trial/enrollment milestone within a fixed window after eligibility.
- **Secondary:** Time from eligibility to trial/enrollment; activation of the first meaningful product behavior; downstream quality of converted accounts.
- **Guardrails:** No material increase in low-quality enrollments, sales workload, opt-outs, complaints, or contact frequency.

**Important caveat**

The model will have less direct behavioral history for a lead than for an existing customer. Dan should help determine whether the available prospect and firmographic data is sufficient, and whether the target should be enrollment, qualified enrollment, or a later activation milestone.

### B. Onboarding

**Why consider it**

- It is a visible lifecycle stage with clear product-use goals.
- It may offer meaningful early signals before long-term retention is observable.

**Why it is currently the weakest first test**

- Sales outreach, existing cadences, onboarding programs, and other communications create substantial confounding.
- The initial test was already constrained by the amount of activity happening in this stage.
- A model may appear ineffective because its treatment is competing with or being masked by other interventions.

**Proposed success metric if retained**

- **Primary:** Incremental completion of the agreed onboarding activation milestone within a fixed window.
- **Secondary:** Time to first value, completion of critical setup steps, early product usage, and 30-day retention proxy.
- **Guardrails:** No increase in support burden, sales escalation, duplicate outreach, or negative customer experience.

**Open issue**

The same “go use the product” objective may appear in trial and onboarding. Before choosing trial over onboarding, we should identify which communications, ownership, and eligibility differences make one stage cleaner to test.

### C. At-risk pros

**Why consider it**

- Retention is strategically important and may have the largest potential business impact.
- A health-score drop or comparable event provides a concrete trigger for model evaluation.
- At-risk pros may receive fewer competing communications than people in onboarding.

**Core question**

Can a targeted, event-triggered recommendation improve recovery without creating unnecessary outreach to pros who would have recovered anyway?

**Proposed success metric**

- **Primary:** Incremental recovery from the agreed at-risk state within a fixed window, measured by return to healthy status and/or meaningful product use.
- **Secondary:** Reduced churn or downgrade risk, return to key product behaviors, and sustained health at 30/60/90 days.
- **Guardrails:** Contact fatigue, support escalation, discount leakage, false-positive intervention, and deterioration among untreated or over-contacted pros.

**Important caveat**

Retention outcomes take longer to observe and may require a larger sample. We should define an early leading indicator so the team can learn before the final retention outcome matures.

## 3. Can multiple stages run in parallel?

### Feasible in principle

Running trial/pre-enroll and at-risk in parallel is technically plausible because they have different eligibility rules, triggers, populations, and outcome windows. Onboarding is also separable operationally, but its communication overlap makes it a weaker candidate for a concurrent causal test.

### Conditions for a credible parallel test

Parallel testing should proceed only if we can establish:

- Non-overlapping or explicitly prioritized eligibility rules.
- A stable control/holdout definition for each stage.
- Stage-specific treatment attribution, so an outcome is tied to the recommendation that caused the exposure.
- A contact policy for pros who move between stages during the test.
- Enough volume in each population to detect a meaningful effect.
- No cross-stage intervention that contaminates control groups.
- Separate primary metrics and decision thresholds; one stage should not be declared successful because another stage performs well.

### Options

| Testing approach | Advantages | Drawbacks | Working view |
|---|---|---|---|
| **One stage first** | Cleanest attribution; concentrates data and operating attention. | Slower learning across the lifecycle; may overfit the system to one population. | Best if data or team capacity is limited. |
| **Trial/pre-enroll + at-risk in parallel** | Tests the highest-priority growth and retention use cases at once; reduces calendar time. | Requires separate metric definitions, controls, and operational ownership. | Recommended parallel configuration if minimum sample and data quality thresholds are met. |
| **All three in parallel** | Broadest learning and fastest coverage. | Onboarding confounding could make interpretation difficult and consume scarce attention. | Not recommended for the first validation. |
| **Waypoint vs. data science model via ABC test** | Directly tests whether Waypoint beats the existing model or improves it. | Needs aligned eligibility, treatment definitions, attribution, and sufficient sample. | Preferred comparison where an existing model is already production-ready enough to serve as a fair comparator. |

### Recommended sequencing

1. Validate the data contract and metric definitions for trial/pre-enroll and at-risk.
2. Run a bounded ABC or holdout test where the existing data science model is a comparator, baseline, or input—not all three at once.
3. Keep onboarding as a follow-up unless the team can show that its communication environment is sufficiently controlled.
4. Reassess after the first readout whether the system should consume more ContextLayer inputs or be expanded to another stage.

## 4. Inputs needed from the group

| Owner | Decisions/input needed | Data or evidence requested | Why it matters |
|---|---|---|---|
| **Dan** | Confirm whether trial/pre-enroll is a viable modeling population; define the business milestone that represents a good conversion. | Available lead/prospect fields, source and freshness, eligibility volume, conversion funnel definitions, and known exclusions. | Establishes whether limited pre-enroll data is enough and prevents optimizing to a shallow or low-quality conversion. |
| **Greg** | Confirm priority between leaky-bucket conversion and retention; identify operational constraints in the pre-enroll motion. | Current funnel baselines, existing campaigns/cadences, sales touchpoints, ownership, and any known measurement gaps. | Determines where the highest-value and cleanest initial test sits. |
| **Shelby** | Validate which stage has the cleanest communication environment and whether at-risk pros are under-contacted. | Current outreach inventory by stage, audience overlap, contact-frequency rules, and examples of competing onboarding interventions. | Identifies confounding and avoids testing where treatment effects will be masked. |
| **Hammer** | Confirm the decision framework, acceptable parallel-test scope, and what evidence is sufficient to continue or stop. | Required review date, minimum business impact, tolerance for false positives, capacity for follow-up, and preferred sequencing. | Converts the discussion into an explicit go/no-go decision rather than an open-ended build. |

## 5. Shared data and model requirements

Regardless of lifecycle stage, the first test needs:

- A stable person/pro identifier and lifecycle-stage assignment.
- A timestamped eligibility event and a timestamped treatment/recommendation event.
- The input snapshot available to the model at decision time.
- A record of the recommendation shown, accepted, rejected, or ignored.
- Control/holdout assignment and any competing interventions received.
- A clearly defined primary outcome with an observation window.
- Early diagnostic signals for learning before the final outcome matures.
- Outcome attribution that can be returned to the recommendation record.
- A path to compare or ingest the relevant existing data science models.

The first version should avoid requiring every possible field. The model should receive the smallest validated input set that can support the agreed metric, with missingness and data freshness visible in the readout.

## 6. Questions to resolve asynchronously

1. For trial/pre-enroll, what exact event counts as success: trial start, enrollment, qualified enrollment, first meaningful use, or another milestone?
2. For onboarding, which existing communications and experiments would contaminate a control group?
3. For at-risk, what event makes a pro eligible, and what is the earliest credible recovery signal?
4. Can the same pro be eligible for both trial/onboarding and at-risk during the test? If yes, which intervention has priority?
5. Is the existing data science model the benchmark, an input to Waypoint, or both in separate arms?
6. What minimum sample size, expected effect size, and test duration are required for each stage?
7. Which inputs are already available in ContextLayer, and which would Option 1 temporarily provide?
8. What result would justify stopping the effort as not worth the investment?

## 7. Provisional recommendation

Start with **trial/pre-enroll and at-risk as the two candidate stages**, and only run both in parallel if the data, controls, and ownership checks pass. Treat **onboarding as a follow-up** unless its competing communication environment can be demonstrated to be clean enough for attribution.

Use **Option 1** as a narrowly scoped validation path, with an explicit migration path to ContextLayer. The goal is not to build a permanent duplicate recommendation system. The goal is to answer, with credible evidence:

- whether Waypoint can improve a defined lifecycle outcome;
- whether it can beat or use the existing data science work;
- whether the effect is large enough to justify further investment; and
- which data and operating conditions are required for the approach to work.

If the team cannot agree on a measurable outcome, clean eligibility/control design, and minimum data contract, that is evidence to pause before adding more model or infrastructure complexity.
