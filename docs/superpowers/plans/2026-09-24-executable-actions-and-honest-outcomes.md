# Executable Actions and Honest Search Outcomes Implementation Plan

> **For agentic workers:** Use Superpowers-style implementation, specification review, and code quality review. Work only in the existing V4 worktree. Do not commit, push, deploy, or touch the unrelated n8n export.

**Goal:** Generate recommendations from actions the Pro can actually execute, and never report a search or evaluation failure as a supported no-action decision.

**Architecture:** Derive direct feature targets from explicit entitlement evidence and the verified CTA catalog. Give those targets to generation and use the same derivation in deterministic candidate validation; reuse the existing channel gate and keep full promoted context for discovery. Replay separate counters for scored dry mechanisms and suppressed rounds from the durable round ledger. Stop promptly on an unavailable evaluation. Use existing `abstained` storage with explicit inconclusive rationales when the search did not collect enough scored evidence.

**Tech stack:** Python/Pydantic/FastAPI/SQLAlchemy, React/TypeScript, pytest, Vitest.

**Scope:** The user's two approved changes. No model change, threshold loosening, n8n/Snowflake edit, new product catalog, database migration, or new external service.

## Invariants

- Waypoint recommends; LCM owns copy and sending. No action menu entry may send a message.
- A promoted `pc` card is reference material, never proof of feature entitlement. Its `e` flag expresses plan compatibility only. An attached feature state or a trusted `top_unused_paid_feature` is the positive direct-feature evidence; `not_attached`, unknown, and plan conflict never become direct feature CTAs. The packaged Staging promotion currently provides neither attachment nor paid-feature rules, so Staging may have **zero direct feature targets** until approved source data is added in a separate change.
- A verified URL alone is insufficient. Promoted direct feature actions require entitlement evidence, an explicit `pc.e=available` plan result, and `resolve_cta` success. A missing `pc` card can mean the plan-incompatible card was filtered out; `e=unknown` cannot authorize a direct CTA. Standard context retains entitlement plus verified destination when plan eligibility is unavailable, labeled as plan unverified. `works_on=web/ios/mobile` verifies the catalog destination type, not this Pro's device reachability. The existing post-generation gate remains authoritative.
- Reuse channels permitted by `gate_pro`; unknown consent is allowed under the existing upstream-DNC policy, not newly certified as consent. A non-feature call or outreach concept may explore an unknown as a question. Forbid model-authored URLs in all ideas; attach catalog URLs only after the gate. A null `feature_key` still requires critic review for implied product claims, so measure that residual semantic risk rather than claim it is eliminated.
- Derive target keys once per round from the brief and process-loaded catalog and use them in both generation and the gate. Resumed rounds may see a changed catalog after deployment; past ledger outcomes do not change. Keep compact catalog/menu identity in round evidence if needed for audit; do not duplicate full menus on candidates.
- `suppressed` spends a separate bounded blocked-round budget, not scored non-improvement. `unavailable` means evaluation infrastructure failed: stop promptly and preserve an existing champion. Resume replay must reproduce scored and blocked budgets. A zero-numeric-screen run and an unavailable final are inconclusive; a numerically scored final rejection remains a separate no-handoff outcome.
- The UI must count and describe the stored outcomes consistently. Historical rows remain readable.

## Current-to-projected behavior

| Case | Current | Projected after implementation |
| --- | --- | --- |
| Promoted `pc` card with `e=available`, no attachment evidence | Can enter `verified_destination_keys` and pass the gate if a CTA exists | Reference-only card; no direct feature CTA in menu |
| `sales_proposal` without verified destination | Generated, then `infeasible_execution` | Omitted as a direct action target before generation; the generator may use a grounded non-product route |
| Fully suppressed rounds, zero screens | `no_action:no_round_cleared_screen` | `abstained:inconclusive_no_screen` after the blocked budget; no handoff |
| Scored losses mixed with blocked rounds | Blocked rounds exhaust `MAX_NO_IMPROVE`; UI says “not worth a touch” | Scored losses and blocked attempts tracked separately; if the blocked budget ends first, UI says “search inconclusive” |
| Screen winner fails held-out final | Generic no-action tally | Remains unsent; UI names final rejection and avoids claiming real-world ineffectiveness |
| Final panel fails to provide reactions | `no_action:all_candidates_abstained` | `abstained:inconclusive_final_unavailable`; no handoff |

## Task 1 — Action menu and fact evidence

**Files:** `services/api/src/waypoint/catalog.py`, `services/api/src/waypoint/staging_context.py` for same-key conflict names only, `services/api/tests/test_catalog.py`, and focused Staging context tests.

- [ ] Write failing tests for promoted `pc` relevance without attachment, `e=unknown`, `not_in_current_plan`, attached feature with explicit `e=available` and verified CTA, attached feature with filtered/missing `pc` card, paid unused feature, unverified CTA, and standard context.
- [ ] Reuse `available_feature_keys` and `verified_destination_keys` as the direct feature menu. For promoted context, require explicit entitlement and `pc.e=available`; for standard context, retain entitlement evidence with plan eligibility marked unverified. Retain `resolve_cta` and existing channel gate. Keep unknown and plan-incompatible cards as reference material when the promotion requests them, but out of direct targets. Assert that the packaged Staging promotion with no entitlement fields yields no direct feature CTA, rather than claiming feature-action lift.
- [ ] Keep the existing standard `known/unknown` and promoted `v/n` representations. Ensure absent and null are never treated as zero. Expose only the *names* of same-canonical-key conflicts dropped by `staging_context.py` as unknown; do not claim to detect contradictions across different keys.
- [ ] Expose the direct target keys in `waypoint_context`; retain compatibility for other callers and historical stored candidates. No new menu table, service, or duplicate channel list.
- [ ] Run `pytest services/api/tests/test_catalog.py -q` from the repository root with the API virtualenv, then Ruff and mypy for changed Python files.

## Task 2 — Use one menu for generation and validation

**Files:** `services/api/src/waypoint/prompts.py`, `services/api/src/waypoint/pipeline.py`, and focused prompt/pipeline tests.

- [ ] Write failing tests showing that a candidate with a `pc`-only or plan-conflicting feature cannot pass, while a proven entitled feature with a verified CTA can. Include a call concept asking about an unknown without claiming it is known.
- [ ] Use the existing context `verified_destination_keys` as the generation menu and `available_feature_keys` plus `resolve_cta` as the gate, both derived from the same entitlement rule. Keep broad `pc` cards explicitly labeled as reference. Ask manager rationale to identify supporting known keys or mark an unknown as a question.
- [ ] Keep the deterministic gate independent of model compliance: clear any model-authored CTA, reject model-authored URLs in candidate text, attach a verified CTA only for a permitted feature. Keep critic review of the full idea, including manager rationale and product-claim consistency; do not add a second LLM gate. Add an adversarial test for `feature_key=null` plus a product-link instruction.
- [ ] Use existing stored recommendation, critic reason, and round ledger as decision evidence. Add a compact catalog identity to round evidence only if a catalog-change resume test shows audit ambiguity; no per-candidate menu snapshot.
- [ ] Run focused prompt/pipeline tests. Compare a frozen fixture batch's feasible-candidate and unsupported-product-claim counts before and after; report actual counts rather than assuming lift. A new Staging run may have no direct product targets until approved entitlement data exists.

## Task 3 — Separate blocked attempts from scored non-improvement

**Files:** `services/api/src/waypoint/loop.py`, `services/api/src/waypoint/pipeline.py`, `services/api/tests/test_loop.py`, `services/api/tests/test_pipeline.py`.

- [ ] Write failing replay tests for all-suppressed, scored-dry-only, mixed lose/suppressed, unavailable evaluation, champion-preserving failure, and resume from an existing ledger. Pin bounded paid work under all-suppressed output and cover `MAX_NO_IMPROVE=0`.
- [ ] Track scored dry mechanisms and suppressed rounds separately in replayable `LoopState`. Reuse `MAX_NO_IMPROVE` as each cap to avoid another operator knob; `MAX_ROUNDS` remains the overall cap. A win resets scored dry count, while a suppressed round never pretends to be a scored loss. Preserve the existing `PATIENCE` meaning for scored dry mechanisms.
- [ ] Add explicit stop reasons for blocked budget and evaluation unavailable. Stop on unavailable evaluation without buying repeated rounds; preserve a previously won champion through final evaluation. Test live and replay equivalence, including an old-ledger resume that may continue under the new policy but stays inside max rounds and spend reservation.
- [ ] In `_stage_score`, a supported `no_action` requires **no champion, at least one numeric screen, and scored dry mechanisms >= `MAX_NO_IMPROVE`**. A round cap, blocked cap, or unavailable evaluation before that scored exhaustion is `WinnerRow.kind="abstained"` with an `inconclusive_*` rationale. Require an actual numeric screen even if `MAX_NO_IMPROVE=0`. Route final `score.abstained` to `abstained:inconclusive_final_unavailable`, not `no_action`. Keep numeric final rejection distinct. No schema migration or handoff behavior change.
- [ ] Run focused loop and pipeline tests, including replay equivalence between live state and stored rounds.

## Task 4 — Make the UI describe the stored decision

**Files:** `apps/web/src/components/WinnerReview.tsx`, `apps/web/src/components/RunStatus.tsx`, one small shared outcome-classification helper if necessary, and focused tests. The existing wire type should not change.

- [ ] Write failing UI tests for zero-screen inconclusive, mixed blocked/scored inconclusive, scored no-action, final rejection, and historical rows.
- [ ] Classify winners, scored no-action, final-rejected, inconclusive, and other abstentions consistently from existing kind/rationale. Show distinct counts in `RunStatus` and matching cards in `WinnerReview`; adapt the existing abstained branch rather than making a second card. Derive round counts from authoritative `proRounds`, with candidate fallback only for historical rows lacking rounds. Unknown historical rationale remains unknown, not silently recast. Do not claim a stopped search proves “not worth a touch.”
- [ ] Replace “critic-suppressed” with “blocked before evaluation” and “stronger panel” with “held-out final.” Display generated/blocked/evaluated counts where available. Change the `MAX_NO_IMPROVE` UI label to describe scored dry and blocked budgets. Keep all non-winners out of LCM handoff.
- [ ] Run focused Vitest tests, TypeScript check, and frontend build.

## Review and verification gates

1. Before implementation, obtain independent plan reviews for product truth/privacy, loop/resume/cost semantics, and minimal scope/UI clarity. Resolve contradictions in this plan before code edits.
2. Review the final diff for accidental broadening of feature access, changed consent behavior, LCM send behavior, or unrelated files.
3. Run focused red/green tests as each task lands. Then run the full API suite, Ruff, mypy, frontend tests, typecheck, and build. If local Postgres is needed, use the existing ephemeral test setup and remove it afterward.
4. Replay the saved 11-Pro ledger read-only where feasible to check reclassification and bound changes. Compare fixture candidate feasibility and unsupported product claims before/after. No old LLM output can prove the new generator's future yield; projected improvements are not production results until a new run measures them.
5. Leave the plan and code uncommitted until the user explicitly authorizes a commit. Do not push or deploy.

## Review findings incorporated

- Product-truth review rejected `pc.e=available` as proof of attachment. The implementation now requires a recognized attached state or trusted paid-feature pointer, explicit promoted plan compatibility, and a catalog destination. Standard context labels plan eligibility unverified because its upstream record has no independent plan result.
- Loop review found that a blocked refine was indirectly spending scored patience. Blocked and unavailable rounds now preserve scored tries. Evaluation unavailability stops the search promptly, preserving any champion already found.
- UI review found that historical no-action rows can overstate certainty. The UI uses round ledger evidence and an explicit unknown category where the stored result cannot prove a completed search; it also separates final rejection from no-action.
- The packaged Staging promotion lacks the entitlement rules required for direct feature CTAs. An approved upstream entitlement source remains necessary before measuring whether Staging yields more executable product actions. A fresh run and copy audit remain the production-quality check for semantic claims with `feature_key=null`.
