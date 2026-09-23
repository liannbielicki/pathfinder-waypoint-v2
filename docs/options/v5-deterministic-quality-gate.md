# V5 spike: deterministic quality gate as the initial evaluation

**Question:** can we grade a touch deterministically before the personas run,
so the personas become the *final* evaluation instead of the first one?

**Verdict: possible, but not as the proposal is written.** The proposal grades
*messages*. Waypoint does not produce messages — it produces a
`Recommendation`, and Allison's LCM tool writes the copy. Re-aim the rubric at
the concept and roughly half the dimensions are computable today with the data
already in the pipeline. The other half are real, but they belong downstream of
drafting, in LCM, not here.

## What Waypoint actually holds at gate time

- `Recommendation` — `title`, `mechanism`, `actions[]`, `pro_facing_concept`,
  `manager_rationale`, `channel`, `risk` (`models.py`).
- `OrgBrief` — ~45 band-level fields per Pro (`n8n.py`).
- Prior touches for this Pro — `failed_mechanisms()`, winners table
  (`evidence.py`).
- Contact frequency — `outreach_count_28d_band` only (a band, not timestamps).

Important: `pro_facing_concept` is the **only field the copywriter sees**
(`handoff.py:234` sends it as `theme`). So grading that field is not a proxy
for grading the send — it is grading the entire brief the send is written from.
That is the strongest argument this is worth doing.

## The proposal's ten dimensions, scored against our data

| Dimension | Verdict | Why |
|---|---|---|
| Length | ✗ here | Copy length is decided in LCM. A concept-length ceiling is all we can do. |
| Reading difficulty | ✗ here | Flesch-Kincaid on a concept measures our prompt style, not the Pro's send. |
| Action clarity (one CTA) | ✓ | `len(actions)` is exact and already structured. |
| Personalization | ✓ | Token overlap against the brief's **values**. Field names don't count — they are identical for every Pro. |
| Specificity | ~ | Partly covered by the existing critic (`ungrounded` / `generic`), which is an LLM call, not deterministic. |
| Friction | ✓ | Ask-count across concept + actions. |
| Channel fit | ~ | SMS ≤160 applies to copy we don't have. A concept ceiling catches structurally oversized ideas only. |
| Compliance | ✓ | Consent-ask is already deterministic; internal-jargon on pro-facing surfaces was prompt-only and is now a hard block. |
| Repetition | ✓ | `difflib` ratio against this Pro's prior concepts; mechanism-level dedupe already exists. |
| Timing | ~ | Only `outreach_count_28d_band`. Real inter-touch timing needs `touch_outcomes.sent_at` coverage we don't have yet. |

## What was built

`services/api/src/waypoint/quality.py` + `tests/test_quality.py` (9 passing).
Pure, no I/O, no model call. Three hard blocks (consent-ask, internal jargon,
near-duplicate) and five weighted dimensions (single ask, grounded in brief,
channel fit, concrete, distinct).

On the existing fixture brief: a grounded single-ask concept scores **1.0**, and
ungrounded three-ask boilerplate scores **0.58**. The floor sits at 0.75.

## The honest caveat

**That floor is set by fiat, not calibrated.** We have no labeled candidate
corpus — no set of concepts with known persona reactions or known real
outcomes. Until we do, the gate's *hard blocks* are trustworthy (they encode
rules we already enforce probabilistically) and its *score* is a heuristic.
Ship the blocks first; hold the score threshold at advisory until it can be
regressed against persona reaction.

The cheapest way to get that corpus: log `quality.evaluate()` alongside every
persona reaction for a few runs without letting it bench anything. Then
re-derive the weights and the floor from data instead of taste.

## Does it speed anything up

Yes, and measurably. Today the free pre-gate in `_verdicts_for_batch` checks
only `recently_failed` and `infeasible_channel`; everything else pays for a
critic call, then a ranker call, then persona screens. A benched candidate
saves all three. It does not shorten the critical path for good candidates —
the batch still waits on the critic — so the gain is spend and round-count, not
latency per Pro.

## Not done, pending your call

The gate is **not wired into the pipeline**. The wire point is one block in
`_verdicts_for_batch` (`pipeline.py:686`), next to the existing free gates, with
`internal_jargon` and `low_quality` added to `SUPPRESSING_BLOCK_KINDS`.
Say the word and it's a small diff.
