from __future__ import annotations

import dataclasses

from pathfinder.action_console.models import AudienceBoundary, GeneratedIdea


# ---------------------------------------------------------------------------
# Coverage math, suppression floor, and applicability stamping
# ---------------------------------------------------------------------------

COVERAGE_SUPPRESSION_FLOOR = 0.5


def applicable_coverage_fraction(
    idea: GeneratedIdea, audience: AudienceBoundary
) -> float | None:
    """Fraction of the selected audience an idea's precondition addresses.

    - Empty condition -> 1.0 (applies to the whole audience).
    - Condition present and at least one referenced factor is observable in the
      evidence -> addressable / total.
    - Condition present but NONE of its factors appear in any evidence row ->
      None (unknown; never scored as 0%).
    """
    condition = idea.applicability_condition or {}
    if not condition:
        return 1.0
    rows = audience.org_uuid_evidence
    total = len(rows)
    if total <= 0:
        return None
    observable = any(
        key in (row.matched_factors or {}) for row in rows for key in condition
    )
    if not observable:
        return None
    addressable = sum(
        1
        for row in rows
        if all(
            (row.matched_factors or {}).get(key) == value
            for key, value in condition.items()
        )
    )
    return addressable / total


# Breadth block kinds that BENCH an idea. "framing" is deliberately excluded:
# the concept fits the whole audience and the over-committed copy is reworded
# downstream by the humans who send it, so framing is a surfaced note, not a
# hard block. "per_pro_data" (data we cannot pull) and "concentration" (concept
# genuinely serves a minority) remain hard blocks.
_SUPPRESSING_BLOCK_KINDS = frozenset({"concentration", "per_pro_data"})


def is_suppressed(idea: GeneratedIdea) -> tuple[bool, str]:
    """Single source of truth for benching an idea from the top ranks.

    For org-by-org mode: only the grounding verdict gates suppression; breadth
    and coverage are group concepts and are ignored for an N=1 audience.

    For segment mode: suppressed when the breadth critic classified a *hard* block
    (concentration or per_pro_data) OR its KNOWN coverage is below the floor.
    A "framing" block is NOT suppressed (fixed in post). Unknown coverage
    (None) is neutral. Returns (suppressed, operator-facing reason).
    """
    # Org-by-org mode: the grounding critic is the only gate. Breadth and
    # coverage are group concepts and are ignored for an N=1 audience.
    if idea.audience_mode == "org":
        if idea.grounding_block_kind == "ungrounded":
            return True, (
                idea.grounding_reason or "Cites data we do not have for this org."
            )
        return False, ""

    kind = idea.breadth_block_kind
    if kind is not None:
        if kind in _SUPPRESSING_BLOCK_KINDS:
            return True, (idea.breadth_reason or "Too narrow for the selected audience.")
    elif idea.breadth_ok is False:
        # Legacy path: ideas judged by the old critic (or stored before the
        # block_kind split) carry no kind — honor the old breadth_ok veto.
        return True, (idea.breadth_reason or "Too narrow for the selected audience.")
    cov = idea.applicable_coverage
    if cov is not None and cov < COVERAGE_SUPPRESSION_FLOOR:
        pct = round(cov * 100, 1)
        return True, f"Addresses only {pct}% of the selected audience — below the whole-audience bar."
    return False, ""


def stamp_applicability(
    idea: GeneratedIdea, audience: AudienceBoundary
) -> GeneratedIdea:
    """Return a copy of *idea* with applicable_coverage set (the value ranking reads)."""
    return dataclasses.replace(
        idea, applicable_coverage=applicable_coverage_fraction(idea, audience)
    )


# ---------------------------------------------------------------------------
# Shared sort key for top-three ranking (used by scorer class AND view model)
# ---------------------------------------------------------------------------


def idea_has_persona_score(idea: GeneratedIdea) -> bool:
    """True when the idea has a usable churn-risk/persona estimate."""
    return idea.persona_status == "scored" and idea.persona_reduction_pp is not None


def idea_is_llm_generated(idea: GeneratedIdea) -> bool:
    """True when the idea came from the grounded LLM generator, not fallback."""
    return idea.generation_source == "llm_grounded"


def _screening_sort_value(idea: GeneratedIdea) -> tuple[int, float]:
    """Tie-break for abstained ideas that carry a screen-then-confirm estimate.

    Scored ideas (idea_has_persona_score True) always get a constant (0, 0.0)
    here -- idea_has_persona_score already sorts them into an earlier bucket
    in ranked_ideas' primary key, so this can never perturb scored-idea
    ordering. Within the unscored bucket: an idea with a screening estimate
    (labeled, non-authoritative -- see persona_response.py's
    reaction_not_significant handling) sorts above a bare abstained/failed
    idea with no numeric estimate at all, and ideas that both carry a
    screening estimate order by screening_reduction_pp descending.
    """
    if idea_has_persona_score(idea):
        return (0, 0.0)
    if idea.screening_reduction_pp is not None:
        return (0, -idea.screening_reduction_pp)
    return (1, 0.0)


def ranked_ideas(ideas: list[GeneratedIdea]) -> list[GeneratedIdea]:
    """Return all ideas sorted by the canonical action-console rank key.

    Sort key:
    1. Fully scored persona/churn-risk estimates before failed/unavailable ones.
    2. Suppressed ideas (low coverage or breadth_ok=False) sort below eligible ideas.
    3. Among unscored ideas, one carrying a screen-then-confirm screening
       estimate (Task 11) sorts above a bare abstained/failed idea, ordered by
       screening_reduction_pp descending. Never perturbs scored-idea ordering.
    4. Grounded LLM ideas before deterministic fallback ideas.
    5. Then score, coverage (tie-break), health, local LLM estimate, and test number.

    This is the single implementation shared by ActionIdeaScorer.top_three and
    view_model.rank_top_three so the two can never diverge.
    """
    def _coverage_sort_value(idea: GeneratedIdea) -> float:
        # Unknown coverage sorts as neutral/high (1.0), never as 0.
        return idea.applicable_coverage if idea.applicable_coverage is not None else 1.0

    return sorted(
        ideas,
        key=lambda idea: (
            0 if idea_has_persona_score(idea) else 1,
            1 if is_suppressed(idea)[0] else 0,
            _screening_sort_value(idea),
            0 if idea_is_llm_generated(idea) else 1,
            -idea.score(),
            -_coverage_sort_value(idea),
            -idea.health_delta,
            -idea.llm_estimate_pp,
            idea.test_number,
        ),
    )


def top_three_ranked(ideas: list[GeneratedIdea]) -> list[GeneratedIdea]:
    """Return the top three ideas sorted by the canonical key."""
    return ranked_ideas(ideas)[:3]


def support_tier_for_idea(idea: GeneratedIdea) -> str:
    """Honest support tier for a recommendation, derived from its persona estimate.

    This is the claim-altitude signal the operator needs — NOT the generation
    source. It mirrors the project's support discipline (a claim is only as good
    as the evidence behind it) and the goal-clearing rule (which gates on
    ci_lower):

    - ``unsupported``: no usable estimate (persona failed/abstained) — never a
      recommendation, only a lead to investigate.
    - ``provisional``: scored, but the CI crosses zero (or is missing) or the
      result is out of calibrated range — we cannot rule out no effect / harm.
    - ``supported``: scored, in calibrated range, and the whole CI is on the
      churn-reduction side (ci_lower > 0).

    Exception: a ``directional_prior`` calibration (sign asserted, magnitude
    not learned) is already gated on direction significance before it can
    reach ``scored``, so a scored directional-prior idea is always
    ``supported`` — the pp ci_lower / in_calibrated_range checks below
    validate a magnitude such an idea cannot support.

    Screen-then-confirm (Task 11) downgrade-only override: an idea whose
    search-panel estimate cleared goal_pp but whose confirm-panel re-score did
    NOT also clear (``confirmation_status == "not_confirmed"``) keeps its
    ``persona_status == "scored"`` and its honest search-estimate numbers, but
    is operator-indistinguishable from a trustworthy "supported" claim unless
    this tier says otherwise. This override can only turn a computed
    ``supported`` into ``provisional`` — it never upgrades ``unsupported`` or
    an already-``provisional`` tier into something better.
    """
    if idea.persona_status != "scored" or idea.persona_reduction_pp is None:
        return "unsupported"
    if idea.calibration_confidence == "directional_prior":
        # Scored on the cards path already means the significance gate passed
        # (direction-only CI > 0). The pp ci_lower / in_calibrated_range checks
        # below validate a magnitude the directional prior cannot support.
        tier = "supported"
    else:
        ci_lower = idea.persona_ci_lower
        if ci_lower is None or ci_lower <= 0 or not idea.in_calibrated_range:
            tier = "provisional"
        else:
            tier = "supported"

    if tier == "supported" and idea.confirmation_status == "not_confirmed":
        return "provisional"
    return tier


# ---------------------------------------------------------------------------
# Pure scoring functions
# ---------------------------------------------------------------------------


def score_generated_ideas(
    *,
    audience: AudienceBoundary,
    ideas: list[GeneratedIdea],
) -> list[GeneratedIdea]:
    """Consolidate signals into churn_risk_reduction_pp on each idea.

    The persona estimate is the PRIMARY scorer.  EvaluatorM3 / historical is
    corroboration only and NEVER gates or moves the final score.

    Rules
    -----
    - persona_status == "scored" AND persona_reduction_pp is not None
      → churn_risk_reduction_pp = persona_reduction_pp
    - all other states (pending / abstained / failed / missing)
      → churn_risk_reduction_pp = 0.0
      A short note is appended to risk_reasoning so the state is surfaced.

    Originals are never mutated; dataclasses.replace() is used throughout.
    """
    result: list[GeneratedIdea] = []
    for idea in ideas:
        if idea.persona_status == "scored" and idea.persona_reduction_pp is not None:
            final_score = idea.persona_reduction_pp
            risk_note = idea.risk_reasoning
        else:
            final_score = 0.0
            state_label = idea.persona_status
            risk_note = (
                f"Persona estimate unavailable (status: {state_label}); "
                "score set to 0.0 — historical corroboration only. "
                + (idea.risk_reasoning or "")
            ).rstrip()

        scored = dataclasses.replace(
            idea,
            churn_risk_reduction_pp=final_score,
            risk_reasoning=risk_note,
        )
        result.append(stamp_applicability(scored, audience))
    return result


def enrich_winner_reasoning(
    *,
    audience: AudienceBoundary,
    winner: GeneratedIdea,
) -> GeneratedIdea:
    """Return a replace() of *winner* with a full narrative rationale and risk_reasoning.

    The rationale must contain all required substrings per the design contract:
    - ``LLM estimate <n> pts · health <n>``
    - ``generation_source <source> · Historical``
    - Why Pathfinder chose this idea for THIS audience (references label/filters)
    - ``Why it might not be good`` + honest caveat
    - Which exact factors made it relevant (audience filters/factor keys)
    - A sample of 1-3 exact org_uuids from audience.org_uuid_evidence

    Uses ``·`` (U+00B7) exactly as specified.
    """
    llm_pp = winner.llm_estimate_pp
    health = winner.health_delta
    gen_src = winner.generation_source

    # Audience context
    aud_label = audience.label
    filter_keys = list(audience.filters.keys())
    factor_keys = [f.get("factor", "") for f in audience.factor_summary]
    relevant_factors = filter_keys + [k for k in factor_keys if k not in filter_keys]

    # Collect up to 3 org_uuid samples
    uuid_samples = [e.org_uuid for e in audience.org_uuid_evidence[:3]]
    uuid_str = ", ".join(uuid_samples) if uuid_samples else "(none on record)"

    # Build rationale
    rationale_parts = [
        f"LLM estimate {llm_pp:.1f} pts · health {health:.1f}",
        f"generation_source {gen_src} · Historical corroboration consulted.",
        (
            f"Pathfinder selected this idea for audience '{aud_label}' because it "
            f"addresses the key risk factors: {', '.join(relevant_factors) if relevant_factors else '(see filters)'}. "
            f"The audience filters ({', '.join(f'{k}: {v}' for k, v in audience.filters.items())}) "
            "point to a segment with elevated churn signals where this action type has traction."
        ),
        (
            f"Why it might not be good: this estimate is based on synthetic persona "
            "reactions and historical patterns; individual Pro context may differ. "
            "Actual churn reduction depends on message timing, Pro engagement history, "
            "and factors not captured in the current covariate set."
        ),
        f"Relevant org evidence sample: {uuid_str}.",
    ]
    rationale = "  ".join(rationale_parts)

    # Risk reasoning — surface persona state clearly without blocking
    if winner.persona_status == "scored":
        risk = (
            f"Persona estimate available (n={winner.persona_panel_n}, "
            f"CI [{winner.persona_ci_lower}, {winner.persona_ci_upper}]). "
            "Historical corroboration is supplementary."
        )
        if winner.calibration_confidence == "directional_prior":
            risk += " (directional estimate — not yet validated)"
    else:
        state_label = winner.persona_status
        risk = (
            f"Persona estimate is unavailable/abstained (status: {state_label}). "
            "Score is 0.0; historical is corroboration only. "
            "Operator may still act — this note is informational, not a gate."
        )

    return dataclasses.replace(winner, rationale=rationale, risk_reasoning=risk)


# ---------------------------------------------------------------------------
# Scorer class (thin wrapper — all logic lives in the free functions above)
# ---------------------------------------------------------------------------


class ActionIdeaScorer:
    """Pure, deterministic scorer.  Never calls the persona service, LLMs, or network."""

    def score_many(
        self,
        *,
        audience: AudienceBoundary,
        ideas: list[GeneratedIdea],
    ) -> list[GeneratedIdea]:
        return score_generated_ideas(audience=audience, ideas=ideas)

    def top_three(self, ideas: list[GeneratedIdea]) -> list[GeneratedIdea]:
        return top_three_ranked(ideas)

    def enrich_winner_reasoning(
        self,
        *,
        audience: AudienceBoundary,
        winner: GeneratedIdea,
    ) -> GeneratedIdea:
        return enrich_winner_reasoning(audience=audience, winner=winner)
