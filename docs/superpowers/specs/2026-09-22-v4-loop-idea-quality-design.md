# V4 Loop and Idea Quality Design

## Goal

Make each V4 run produce executable outreach ideas, compare every idea on equivalent evidence, choose a defensible winner, learn only from trustworthy outcomes, and explain the decision without adding a large dashboard.

## Approved Behavior

### Model choice

Each run chooses `deep` or `fast`. New runs default to `deep`, the tier backed by the configured Sonnet model. The run snapshot and winner evidence record both the tier and the actual model used at each paid stage. Cached persona reactions are keyed by the resolved model name so changing models cannot reuse another model's judgment.

### Loop semantics

- Round one is `cold`: generate the configured number of genuinely distinct mechanisms.
- `refine` generates variants of the currently selected mechanism, even when that candidate did not beat the champion.
- `shift` generates mechanisms not already tried in the run.
- Mechanism comparison uses a normalized key so capitalization and punctuation cannot evade duplicate or tried-mechanism checks.
- Every feasible candidate in a round receives the same persona-screen scoring contract. The winner is the highest valid screen score; the ranker remains useful ordering evidence, not a shortcut that hides candidates from comparison.
- Reaction payloads must contain each requested persona exactly once with finite scores in the supported 3-7 range. Invalid judgments are retried and then become unavailable; they never win by malformed arithmetic.

### Channel and no-action safety

Generated recommendations may use only `sms`, `email`, or `call`. `none` remains only a follow-up stop state and a `Winner.kind = no_action` decision; it is never a sendable recommendation. Handoff defensively skips any channel other than SMS or email, so a malformed or legacy value cannot inherit LCM's SMS default.

When a valid RECO suggested channel exists and is available for the run, generation follows it unless the recommendation carries an explicit override reason. The decision evidence shows suggested channel, selected channel, and the reason for any override.

### Executability

The model selects a known feature/action, but Waypoint resolves the user-facing CTA and destination from the existing feature catalog. Unknown, broken, or unreachable claims are blocked before persona spend. Generic non-feature actions remain possible only when they carry an explicit executable next step; Waypoint does not invent product destinations or extend the unverified LCM payload contract.

### Persona selection

Persona selection is a fallback ladder, not an exact-match gate:

1. weighted matches across available segment, plan, usage, lifecycle, trade, and business-state fields;
2. same-segment fallback;
3. best available broad fallback.

Match strength and field coverage are recorded separately so a segment-only match cannot look like a rich match. A lack of exact matches never fails a run. An empty/unavailable persona service remains an infrastructure/data problem rather than being disguised as a match.

Final validation prefers personas not used during screening. When the pool is too small, reuse is allowed, identified, and surfaced as degraded evidence rather than failing the run.

### Learning

Outcome aggregation deduplicates rows by logical touch before counting evidence, preventing multiple sources from double-counting one outreach event. Warm starts require adequate shared fingerprint coverage as well as similarity. Existing channel-specific evidence remains channel-specific; the implementation does not invent email/call pollers where external source contracts are absent.

### Compact visibility

The existing winner card adds a compact explanation:

- selected channel versus RECO suggestion and override reason;
- number of rounds and candidates considered;
- candidate rank and comparable screen score summary;
- warm-start status;
- actual models used.

Full stored ranking details are available in a collapsed disclosure. No separate analytics dashboard or new visualization system is added.

## Boundaries

- Waypoint remains recommendation-only. Iterable is read-only; LCM owns SMS/email copy and sending; call recommendations remain operator to-dos.
- Existing untracked n8n work is untouched.
- No commit, push, email/call outcome integration, automatic threshold tuning, or mandatory exact/non-overlapping persona requirement is part of this change.

## Verification

Use red-green tests for each behavior, then run backend ruff, mypy, all pytest tests with local Postgres, frontend unit tests, lint, production build, migration-head checks, diff checks, and an independent final code review.
