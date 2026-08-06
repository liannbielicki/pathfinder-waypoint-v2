# Pathfinder LLM Prompt & Cost Reference Artifacts

Verbatim, copy-paste-ready extractions from the Pathfinder action-console codebase.
Every block is quoted exactly as it appears in source, with a `file:line` citation.
Prompt strings are f-strings / `textwrap.dedent` templates — the `{...}`
interpolation placeholders are reproduced exactly as written in source.

---

## 1. Idea Generator

### 1a. Model call — system prompt + generation params

`src/pathfinder/action_console/generator.py:650-665`

The `system` argument passed to `client.messages.create`:

```text
You generate grounded action-console strategy ideas. Return only the requested tool call.
```

Generation params, same call (`generator.py:650-665`):

- `model = os.environ.get("PATHFINDER_ACTION_CONSOLE_LLM_MODEL") or DEFAULT_MODEL` (`generator.py:649`)
- `max_tokens = _llm_max_tokens_for_count(int(request["count"]))` — i.e. `count * 5000` (`_LLM_TOKENS_PER_IDEA = 5000`, `generator.py:74`, `623-624`)
- `temperature = 0.8` (`generator.py:653`)
- `tool_choice = {"type": "tool", "name": "generate_grounded_action_ideas"}` (`generator.py:664`)
- Timeout env: `PATHFINDER_ACTION_CONSOLE_LLM_TIMEOUT_S` default `"90"`; `max_retries=0` (`generator.py:646-648`)
- User prompt is `org_idea_prompt(request)` when `request["audience_mode"] == "org"`, else `_idea_prompt(request)` (`generator.py:658-662`)

### 1b. Segment-mode user prompt template — `_idea_prompt`

`src/pathfinder/action_console/generator.py:482-620`

```text
Generate exactly {request["count"]} grounded strategy/action ideas for the Pathfinder action console.

Use only the audience facts below as grounding. The target is the full selected audience boundary.
Do not narrow the target into smaller subsegments unless those filters were explicitly selected.
Evidence subsegments can inspire the idea, but they are not the audience boundary.

Each idea must include:
- action_idea
- pro_facing_concept
- audience_rationale
- why_it_could_reduce_churn
- fact_grounded_in
- likely_risk_or_objection
- recommended_channel_timing
- recommended_channel
- manager_rationale
- research_state
- research_move
- research_hypothesis
- research_reason
- problem_key
- mechanism_key
- delivery_key

You are the autonomous researcher and you own the next research move.
Read the results ledger before proposing the experiment. First judge
whether the loop is exploring, improving, polishing, or confirming.
Then choose one move: new_direction, refine_champion, combine_evidence,
or reconfirm_champion. State one falsifiable research_hypothesis and a
concise research_reason grounded in the prior results. If you judge that
the loop is polishing, choose new_direction rather than another timing,
channel, copy, or naming variation of the same idea.
Classify the experiment with the bounded problem_key, mechanism_key, and
delivery_key fields. These keys identify real research similarity even
when titles, categories, or action-type labels differ.

Keep two layers separate:
- pro_facing_concept is the persona-facing concept / customer moment the
  pro would actually experience. It must be plain language, concrete,
  tied to a real work pain, and free of internal churn, retention,
  account-management, score, or lock-in language.
- manager_rationale is the manager-facing rationale explaining why HCP
  would test this action: the audience fact pattern, the churn-reduction
  hypothesis, why this is more promising than a generic touch, and what
  would make the test succeed or fail.

If an action only makes sense for accounts in a particular observable state
(e.g. turning on a feature an account does not yet have), declare that as an
applicability_condition map of factor_key -> required value, using real factor
keys/values from the fact pack. Omit it entirely when the action applies to the
whole selected segment. Only use applicability_condition when the fact pack
indicates that state covers the vast majority of the selected audience; otherwise
create a broader action for the whole boundary. Do NOT invent factors; unknown
ones are discarded.

For recommended_channel choose sms (use none only for a monitor-only
hold). Every touch must be sms-native: short, time-sensitive, and
mobile-first (something a pro can act on between jobs).
recommended_channel_timing then explains the timing/cadence in prose.

Do not write final email copy, SMS copy, or campaign copy unless the action type truly calls for copy.
Do not mention churn-risk points, deliverability scores, database provenance,
retention cases, switching costs, account cadence, or internal analyst jargon
in pro_facing_concept. Make ideas operationally testable and meaningfully
different from each other.

--- CONCEPT EXECUTABILITY (these ideas are SEEDS, not final copy) ---
These ideas are seeds/concepts. Per-Pro personalization (the Pro's name,
company, and similar) is applied downstream by the marketing team - do NOT add
merge fields or worry about name/company personalization here.
The CONCEPT must apply to the WHOLE selected audience. Do NOT build an idea
around a per-Pro operational value we cannot pull for every Pro - their exact
AR balance, job count, unsent-invoice list, specific dates, or any individual
data point that would need a per-Pro data pull. Marketing cannot fill in data
we do not have, so if the concept only works given each Pro's specific number,
it is out of scope.
When leaning on a group trait, prefer one the audience fact pack shows a large
majority (~90%+) share, framed conditionally in second person.
Example framing: "If you haven't set up X yet, here's why it helps...".
Group-reference framing ("many pros like you...") is acceptable, but the
conditional version usually reads better. Do NOT state a non-universal trait as
a definite fact about the individual ("You haven't turned on X") unless ~100%.
Default posture: the concept is safely applicable to the whole group.
---

This console asks for ideas sequentially. Treat prior results as evidence,
not blanket bans on concepts, channels, categories, or action types. Never
repeat an exact prior title. You may refine or reconfirm a prior concept when
your research judgment says that is the highest-learning move. Read
results_log like Karpathy's results.tsv: keep means the branch advanced,
discard means the experiment did not beat the current_branch, and crash
means the persona run produced no usable score. An abstained idea that
also carries a screening_reduction_pp is different: it DID produce a
usable, non-authoritative persona estimate. Positive pp means the
persona panel expects this message to retain better than typical
outreach content for this audience; negative pp means it lands worse
than typical content -- below the audience's usual bar. Treat a
negative screening estimate as evidence that this mechanism or framing
underperforms typical outreach for this audience -- change the
underlying mechanism, do not just rephrase it. Prefer extending the
mechanisms behind the highest-screening prior ideas over untested
territory; search_directive.screening_leaders and
search_directive.screening_laggards summarize exactly this -- leaders
are mechanisms to build on, laggards are mechanisms to abandon. If the
history shows only polishing, change the underlying mechanism instead
of renaming another Max-plan value audit, walkthrough, or
feature-adoption check-in. Name the concrete mechanism being tested.
Do not put internal audience labels like cell=2A, plan=max, org counts,
or database filters in action_idea or pro_facing_concept; keep those in
manager_rationale and fact_grounded_in.

Search directive:
{json.dumps((request.get("parent_context") or {}).get("search_directive") or {}, sort_keys=True)}

If search_directive.move is agent_decides, use its target, prior titles,
opportunity_axis, research_phase, and remaining_budget as research
evidence. Choose only from allowed_research_moves. For other directive
moves, follow search_directive.move, change every listed must_change axis,
avoid every listed must_avoid value, and use opportunity_axis when present.

measured_action_priors, when present in Branch context below, gives
this audience segment's MEASURED panel receptiveness by action_type
(mean_panel_reaction_sdg, a 3-7 scale, from the churn-validated
calibration) and the real 3-month churn of orgs whose early experience
was dominated by that action_type. Prefer mechanisms native to
high-reaction action_types; treat low-reaction ones as headwinds.

Audience fact pack:
{json.dumps(request["audience_fact_pack"], indent=2, sort_keys=True)}

Branch context:
{json.dumps(request.get("parent_context") or {}, sort_keys=True)}

Return the ideas through the {_LLM_TOOL_NAME} tool only.
```

Note: `_LLM_TOOL_NAME = "generate_grounded_action_ideas"` (`generator.py:73`).

### 1c. Forced tool / JSON schema — `_idea_tool_schema`

`src/pathfinder/action_console/generator.py:374-479`

```text
name:        generate_grounded_action_ideas
description: Return grounded, novel strategy/action ideas for this audience.
input_schema:
  type: object
  additionalProperties: false
  required: [ideas]
  properties.ideas:
    type: array
    minItems: 1
    items:
      type: object
      additionalProperties: false
      required:
        - action_idea
        - pro_facing_concept
        - audience_rationale
        - why_it_could_reduce_churn
        - fact_grounded_in
        - likely_risk_or_objection
        - recommended_channel_timing
        - recommended_channel
        - manager_rationale
        - idea_category
        - outreach_action_type
        - estimated_churn_risk_reduction_pp
        - estimated_health_delta
        - research_state
        - research_move
        - research_hypothesis
        - research_reason
        - problem_key
        - mechanism_key
        - delivery_key
      properties:
        action_idea:                       {type: string}
        pro_facing_concept:                 {type: string}
        audience_rationale:                 {type: string}
        why_it_could_reduce_churn:          {type: string}
        fact_grounded_in:                   {type: string}
        likely_risk_or_objection:           {type: string}
        recommended_channel_timing:         {type: string}
        recommended_channel:                {type: string, enum: list(PROPOSAL_CHANNELS) + ["none"], description: see below}
        manager_rationale:                  {type: string}
        idea_category:                      {type: string}
        outreach_action_type:               {type: string}
        estimated_churn_risk_reduction_pp:  {type: number}
        estimated_health_delta:             {type: number}
        research_state:  {type: string, enum: list(_RESEARCH_STATES)}
        research_move:   {type: string, enum: list(_RESEARCH_MOVES)}
        research_hypothesis:                {type: string}
        research_reason:                    {type: string}
        problem_key:     {type: string, enum: list(RESEARCH_PROBLEM_KEYS)}
        mechanism_key:   {type: string, enum: list(RESEARCH_MECHANISM_KEYS)}
        delivery_key:    {type: string, enum: list(RESEARCH_DELIVERY_KEYS)}
        applicability_condition: {type: object, description: see below}   # NOT in required[]
```

`recommended_channel` enum + description, verbatim (`generator.py:419-428`):

```text
enum: list(PROPOSAL_CHANNELS) + ["none"]
description:
The outreach channel for this idea. Only sms is in scope right now: short, time-sensitive, mobile-first nudges a pro can act on between jobs. Use 'none' only for a monitor-only hold.
```

`research_state` description (`generator.py:437-440`):

```text
Your judgment of whether the loop is exploring, improving, polishing, or confirming.
```

`research_move` description (`generator.py:445`):

```text
The research move you chose for this experiment.
```

`applicability_condition` description (`generator.py:462-472`):

```text
OPTIONAL. Include ONLY when the action presupposes an observable account state (e.g. activating a feature the account lacks). A flat map of factor_key -> required value, e.g. {"csr_ai_status": "inactive"}. Reference real factor keys/values from the audience fact pack; omit entirely when the action applies to the whole segment.
```

### 1d. How `PROPOSAL_CHANNELS` feeds the enum

`src/pathfinder/action_console/generator.py:421`

The `recommended_channel` enum is built at schema-construction time as:

```python
"enum": list(PROPOSAL_CHANNELS) + ["none"],
```

`PROPOSAL_CHANNELS` is imported from `pathfinder.action_console.models` (`generator.py:13-21`).
The enum is therefore `PROPOSAL_CHANNELS` (in source order) with the literal
`"none"` appended as the last member. The prose prompt (1b) additionally tells
the model to choose `sms` only, using `none` only for a monitor-only hold — so
the schema enum is broader than the prose instruction.

> Note: the exact tuple contents of `PROPOSAL_CHANNELS` live in
> `src/pathfinder/action_console/models.py` (not among the files read for this
> artifact). It is referenced by identity here, not paraphrased.

### 1e. Retry escalation instructions (directive misses)

Soft retry, `generator.py:986-991`:

```text
The previous idea missed search_directive. Return a different idea that satisfies search_directive exactly.
```

Hard/final retry, `generator.py:1048-1053` (`{forbidden}` interpolated):

```text
FINAL ATTEMPT: two previous ideas missed search_directive. You are forbidden from repeating {forbidden} -- these are now listed in search_directive.must_avoid. Return a genuinely different idea that satisfies search_directive exactly.
```

---

## 2. Org-brief / Org Prompt Construction

### 2a. Org-mode user prompt template — `org_idea_prompt`

`src/pathfinder/action_console/org_prompt.py:57-141`

```text
You are Waypoint's action researcher working in ORG MODE: propose the
next {request["count"]} action ideas for ONE specific Pro (a single
HCP customer organization), not a group.

You are the autonomous researcher and you own the next research move.
Read the results ledger before proposing the experiment. First judge
whether the loop is exploring, improving, polishing, or confirming.
Then choose one move: new_direction, refine_champion, combine_evidence,
or reconfirm_champion. State one falsifiable research_hypothesis and a
concise research_reason grounded in the prior results. If you judge that
the loop is polishing, choose new_direction rather than another timing,
channel, copy, or naming variation of the same idea.
Classify the experiment with the bounded problem_key, mechanism_key, and
delivery_key fields. These keys identify real research similarity even
when titles, categories, or action-type labels differ.

--- INDIVIDUALIZATION FIRST (this is the whole point of org mode) ---
Build each idea around THIS Pro's actual observable state below.
Prioritize what is DISTINCTIVE about them: an unusual combination of
factors, a gap between their plan and their usage, a feature their
state says they would benefit from. A generic idea that could be sent
to any Pro is a wasted slot here.
You MAY use definite second-person phrasing for any factor in "known"
(e.g. "You're on the Max plan and haven't turned on online booking").

--- GROUNDING (hard rule) ---
Do NOT cite, state, or imply any specific value about this Pro that is
not in the "known" map below — no invented AR balances, job counts,
revenue figures, or dates. Factors listed in "unknown" are gaps: never
assert them as facts. An unknown factor may motivate a question-framed
touch ("How are you handling overdue invoices today?") but never a
stated fact.
---

Keep two layers separate:
- pro_facing_concept is the concept / customer moment this Pro would
  actually experience. Plain language, concrete, tied to a real work
  pain, free of internal churn, retention, account-management, score,
  or lock-in language.
- manager_rationale is the manager-facing rationale: which of this
  Pro's facts the idea leans on, the churn-reduction hypothesis, and
  what would make the test succeed or fail.

For recommended_channel choose from email or sms only (use none only
for a monitor-only hold). Reach for sms when the touch is short,
time-sensitive, and mobile-first. recommended_channel_timing then
explains the timing/cadence in prose.

Do not write final email copy, SMS copy, or campaign copy unless the
action type truly calls for copy. Do not mention churn-risk points,
deliverability scores, database provenance, retention cases, switching
costs, account cadence, or internal analyst jargon in
pro_facing_concept. Make ideas operationally testable and meaningfully
different from each other. Never repeat an exact prior title. Do not
put internal identifiers like org_uuid or database filters in
action_idea or pro_facing_concept; keep those in manager_rationale and
fact_grounded_in.

Search directive:
{json.dumps((request.get("parent_context") or {}).get("search_directive") or {}, sort_keys=True)}

If search_directive.move is agent_decides, use its target, prior titles,
opportunity_axis, research_phase, and remaining_budget as research
evidence. Choose only from allowed_research_moves. For other directive
moves, follow search_directive.move, change every listed must_change
axis, avoid every listed must_avoid value, and use opportunity_axis
when present.

This Pro's context pack (known = real values you may state as fact;
unknown = gaps you must never assert; field_sources = bookkeeping that
records which system each known field came from — field NAMES only,
never facts about this Pro, and never something to mention in an idea):
{json.dumps(pack, indent=2, sort_keys=True)}
{_semantics_block(pack)}

Branch context:
{json.dumps(request.get("parent_context") or {}, sort_keys=True)}

Return the ideas through the generate_grounded_action_ideas tool only.
```

### 2b. How the condensed org brief (`pack`) is built

`src/pathfinder/action_console/org_prompt.py:58`

```python
pack = prompt_safe_org_context(request["org_context_pack"])
```

- The raw `org_context_pack` is passed through
  `prompt_safe_org_context(...)` (imported from
  `pathfinder.action_console.org_context`, `org_prompt.py:26`) before being
  serialized into the prompt. That function is the include/exclude gate — its
  body lives in `org_context.py` (not read for this artifact).
- The pack is documented (module docstring, `org_prompt.py:11-14`) as carrying
  **29 fields under contract `org-context-v2`** (vs 13 in v1).
- Pack shape referenced in the prompt: a `"known"` map (real values, may be
  stated as fact), an `"unknown"` list (gaps, never assertable), and a
  `"field_sources"` map (bookkeeping — field names only, never facts).

### 2c. Semantics / interpretation block — `_semantics_block`

`src/pathfinder/action_console/org_prompt.py:30-54`

Produced by `pack_semantics(pack)` (from
`pathfinder.action_console.org_context_semantics`, `org_prompt.py:27`). Emitted
only when non-empty; otherwise the block is omitted entirely. Template
(`org_prompt.py:44-54`):

```text
--- HOW TO READ THE PACK (interpretation, NOT extra facts) ---
These are reading rules for the values in "known". They add no new
information about this Pro, and they never license asserting anything
beyond what "known" already contains. notes_for_this_pro is the part
that should change what you propose.
{block}
---
```

where `{block}` = `json.dumps(semantics, indent=2, sort_keys=True)`.

---

## 3. Critics

### 3a. Breadth critic

`src/pathfinder/action_console/breadth_critic.py`

Model call (`breadth_critic.py:158-166`): `max_tokens=2000`, `temperature=0.0`,
`tool_choice={"type": "tool", "name": "judge_idea_breadth"}`. System prompt
(`breadth_critic.py:162`):

```text
You audit action-idea breadth. Return only the requested tool call.
```

User prompt template — `_prompt` (`breadth_critic.py:66-132`):

```text
You are auditing whether each proposed action idea serves the WHOLE
selected audience, or only a sub-slice of it.

Selected audience:
{json.dumps(fact_pack, indent=2, sort_keys=True)}

Observed factor spread across this audience (key, value, org_count):
{json.dumps(spread, indent=2, sort_keys=True)}

Classify the PRIMARY breadth block for each idea into exactly one
block_kind. Only two of them bench the idea; the others let it through.

  - "per_pro_data" (HARD BLOCK): the message or execution depends on an
    INDIVIDUAL Pro's specific value — their exact AR balance, job count,
    unsent-invoice list, or specific dates (e.g. "cite their AR balance"
    or "their jobs booked last month") — that is NOT among the audience
    data shown above. The data does not exist to send it, so always flag
    it. Treat the factor list above as the per-Pro data we actually have.

  - "concentration" (HARD BLOCK): the CONCEPT itself only makes sense for
    a clear MINORITY of this audience, or is redundant because nearly
    everyone already has the feature/state. Use the factor spread to judge.
    TIGHTENED: only flag concentration when the required trait/state is
    held by a CLEAR minority (well under half) OR is near-universal
    (redundant). If a solid majority (~70%+) can act on it, do NOT flag —
    return "none". Do NOT flag near-universal traits (e.g. 80-90% share).

  - "framing" (NOT a hard block): the CONCEPT applies broadly to the whole
    audience, but the COPY over-commits to a sub-slice with definite
    second-person phrasing (e.g. "you're a solo pro", "you've been in
    business a year"). This is fixed downstream by rewording it
    conditionally, so it is NOT suppressed — record it as a note only.
    Use "framing" when the only issue is phrasing, not concept relevance.

  - "none": no breadth problem — the concept fits the whole audience.

IMPORTANT — unknown-neutral rule: Do NOT block an idea merely because a
precondition cannot be confirmed from the data. If the fact pack does not
show whether a trait is widely shared or not, that is unconfirmable —
return "none". Only flag a hard block when the data positively shows the
problem. Unknown / unconfirmable = neutral, exactly as the coverage lever
treats it.

Keep breadth_reason to one short operator-facing sentence explaining the
chosen block_kind, e.g. "requires each Pro's jobs-booked count, which
isn't in the audience data". Return a verdict for EVERY idea id.

Ideas:
{json.dumps(idea_blobs, indent=2, sort_keys=True)}
```

Breadth tool schema (`breadth_critic.py:39-63`): tool `judge_idea_breadth`,
required top-level `[verdicts]`; each verdict object requires
`[idea_id, block_kind, breadth_reason]`; `block_kind` enum =
`list(_BLOCK_KINDS)` where `_BLOCK_KINDS = ("none", "framing", "concentration", "per_pro_data")` (`breadth_critic.py:36`).

### 3b. Grounding critic

`src/pathfinder/action_console/grounding_critic.py`

Model call (`grounding_critic.py:153-161`): `max_tokens=2000`,
`temperature=0.0`, `tool_choice={"type": "tool", "name": "judge_idea_grounding"}`.
System prompt (`grounding_critic.py:157`):

```text
You audit action-idea grounding. Return only the requested tool call.
```

User prompt template — `_prompt` (`grounding_critic.py:62-127`):

```text
You are auditing action ideas proposed for ONE specific Pro (org mode).
The ONLY data we actually have about this Pro is the context pack
below: "known" holds real values; "unknown" lists factors whose value
we do NOT have. "field_sources" is bookkeeping — it names which system
each known field came from, asserts nothing about this Pro, and an idea
is neither grounded nor ungrounded by anything in it.

This Pro's context pack:
{json.dumps(prompt_pack, indent=2, sort_keys=True)}

How to read the pack's vocabularies (reading rules only — these add no
facts about this Pro, and a value they explain is still only as certain
as the pack says it is):
{json.dumps(pack_semantics(prompt_pack), indent=2, sort_keys=True)}

One consequence matters most for your job: a feature_<name>_state of
"attached_usage_unknown" means we know they HAVE the feature and do NOT
know whether they use it. An idea asserting that they do use it, or
that they do not, is ungrounded.

Classify the PRIMARY grounding problem for each idea into exactly one
block_kind. Only "ungrounded" benches the idea; the others let it
through.

  - "ungrounded" (HARD BLOCK): the message or execution depends on a
    specific value about this Pro that is NOT in "known" — an exact AR
    balance, job count, revenue figure, date, or a definite claim
    about a factor listed in "unknown" (e.g. stating "your overdue
    invoices" when open_ar_band is unknown). The data does not exist
    to send it, so always flag it.

  - "generic" (NOT a hard block): the idea is grounded but broad
    boilerplate — it could be sent to any Pro and ignores what is
    distinctive in "known". Record it as a note so the operator can
    see the loop is not individualizing. Do NOT bench it.

  - "none": individualized AND grounded — it leans on real values
    from "known" without asserting anything we do not have.

IMPORTANT — question-framing is allowed: an idea may ASK about an
unknown factor ("How are you handling overdue invoices today?")
without asserting it. Only flag "ungrounded" when the idea STATES or
REQUIRES a value we do not have.

Keep grounding_reason to one short operator-facing sentence, e.g.
"states an exact AR balance that is not in the context pack".
Return a verdict for EVERY idea id.

Ideas:
{json.dumps(idea_blobs, indent=2, sort_keys=True)}
```

Grounding tool schema (`grounding_critic.py:35-59`): tool
`judge_idea_grounding`, required top-level `[verdicts]`; each verdict object
requires `[idea_id, block_kind, grounding_reason]`; `block_kind` enum =
`list(_GROUNDING_KINDS)` where
`_GROUNDING_KINDS = ("none", "generic", "ungrounded")` (`grounding_critic.py:32`).

---

## 4. Model ID & Generation Params

**The `llm_tooling.py` file is at `src/pathfinder/llm_tooling.py`, NOT
`src/pathfinder/action_console/llm_tooling.py`.** All three call sites
(generator, breadth_critic, grounding_critic) import `DEFAULT_MODEL` from
`pathfinder.llm_tooling`.

`src/pathfinder/llm_tooling.py:19`

```python
DEFAULT_MODEL = "claude-sonnet-4-6"
```

Model resolution at every call site (env override wins):

```python
model = os.environ.get("PATHFINDER_ACTION_CONSOLE_LLM_MODEL") or DEFAULT_MODEL
```

- generator: `generator.py:649`
- breadth_critic: `breadth_critic.py:157`
- grounding_critic: `grounding_critic.py:152`

Per-call-site generation params:

| Call site | file:line | max_tokens | temperature | tool_choice (forced tool) |
|---|---|---|---|---|
| Idea generator | `generator.py:650-665` | `count * 5000` (`_LLM_TOKENS_PER_IDEA=5000`) | `0.8` | `generate_grounded_action_ideas` |
| Breadth critic | `breadth_critic.py:158-166` | `2000` | `0.0` | `judge_idea_breadth` |
| Grounding critic | `grounding_critic.py:153-161` | `2000` | `0.0` | `judge_idea_grounding` |

Shared client construction at all three sites: `max_retries=0`, timeout from
env `PATHFINDER_ACTION_CONSOLE_LLM_TIMEOUT_S` (default `"90"` seconds).

---

## 5. Token → Dollar Price Table & Usage Capture

`src/pathfinder/action_console/llm_usage.py`

### 5a. Price table

`src/pathfinder/action_console/llm_usage.py:30-41`

Values are **USD per 1,000,000 tokens**, ordered `(input, output)`. Comment at
`llm_usage.py:30-31`: *"(input, output) USD per million tokens. Verified against
Anthropic's pricing 2026-07-31. Keys are the exact model ids the API accepts."*

**Date stamp: `2026-07-31`**

| Model key | Input ($/Mtok) | Output ($/Mtok) |
|---|---|---|
| `claude-fable-5` | 10.00 | 50.00 |
| `claude-opus-5` | 5.00 | 25.00 |
| `claude-opus-4-8` | 5.00 | 25.00 |
| `claude-opus-4-7` | 5.00 | 25.00 |
| `claude-opus-4-6` | 5.00 | 25.00 |
| `claude-sonnet-5` | 3.00 | 15.00 |
| `claude-sonnet-4-6` | 3.00 | 15.00 |
| `claude-haiku-4-5` | 1.00 | 5.00 |

Verbatim source (`llm_usage.py:32-41`):

```python
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
```

Note: the default model in use, `claude-sonnet-4-6`, is priced at **$3.00 in /
$15.00 out per Mtok**.

### 5b. Cache multipliers

`src/pathfinder/action_console/llm_usage.py:43-48`

```python
_CACHE_READ_MULTIPLIER = 0.10   # cache reads bill at ~0.1x the input rate
_CACHE_WRITE_MULTIPLIER = 1.25  # a 5-minute cache write at 1.25x
_MILLION = 1_000_000
```

Call sites do not set `cache_control` today (per module comment,
`llm_usage.py:43-46`), so these normally multiply zero.

### 5c. Cost formula — `cost_usd`

`src/pathfinder/action_console/llm_usage.py:131-147`

```python
price = PRICES_USD_PER_MTOK.get(str(model).strip())
if price is None:
    return None
input_rate, output_rate = price
return (
    usage.input_tokens * input_rate
    + usage.cache_read_tokens * input_rate * _CACHE_READ_MULTIPLIER
    + usage.cache_write_tokens * input_rate * _CACHE_WRITE_MULTIPLIER
    + usage.output_tokens * output_rate
) / _MILLION
```

Returns `None` (not `0.0`) for a model absent from the table, so an unpriced
model never renders as "$0.00 / free".

### 5d. `record_response` flow — how usage is captured

- **`record_response(model, response)`** (`llm_usage.py:213-223`): reads the
  current `ContextVar` ledger (`_current`); if none is bound it is a **no-op**.
  Otherwise records `usage_from_response(response)` into the ledger. Never
  raises.
- **`usage_from_response`** (`llm_usage.py:97-128`): duck-types the response's
  `.usage` block. Reads `input_tokens`/`output_tokens` (falling back to OpenAI's
  `prompt_tokens`/`completion_tokens`), plus `cache_read_input_tokens` and
  `cache_creation_input_tokens`. A missing usage block →
  `ModelUsage(calls=1, unmeasured_calls=1)`.
- **`usage_scope()`** (`llm_usage.py:202-210`): context manager the runner opens
  to bind a fresh `UsageLedger` for one run's duration. Uses a `ContextVar`
  (not a module global) so concurrent batch runs don't bill one org's tokens to
  another (`llm_usage.py:14-16`).
- **`UsageLedger.record` / `.totals`** (`llm_usage.py:150-194`): thread-safe
  (`threading.Lock`) per-model accumulation. `totals()` returns `llm_cost_usd =
  None` when ANY model that ran is unpriced OR any call was unmeasured, and
  lists offenders in `llm_unpriced_models`.

Call-site ordering (both critics, verbatim intent): `record_response(...)` is
called **before** `extract_tool_input(...)` so a response that failed to call
the tool but still consumed tokens is not under-reported
(`breadth_critic.py:167-171`, `grounding_critic.py:162-166`). In the generator,
`record_response` is deliberately placed **outside** the `try` that wraps the
API call, so a recording failure can never be reported as an idea-generation
failure (`generator.py:668-671`).
