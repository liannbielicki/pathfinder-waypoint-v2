"""Deterministic concept-quality gate (v5 spike).

Runs BEFORE the critic LLM call and before any persona spend, on the artifact
Waypoint actually owns: the `Recommendation`. Waypoint never writes message
copy — `pro_facing_concept` is the only field the LCM copywriter sees (see
handoff.py), so that field, `title`, and `actions` are what can be graded here.
Copy-level rules (final character count, reading level of the send, opt-out
footer) belong downstream of drafting, not here.

Same idea + same context -> same score, always. No I/O, no model: prior
concepts and the brief are passed in by the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from waypoint.models import Recommendation
from waypoint.n8n import OrgBrief

# Internal vocabulary the generator prompts already ban on pro-facing surfaces.
# Prompt-level bans are probabilistic; this is the hard backstop.
_INTERNAL_JARGON = re.compile(
    r"\b(churn|retention|retain|at[- ]risk|lock[- ]?in|account manag\w*|"
    r"health score|risk score|ltv|upsell|win[- ]?back|save offer)\b",
    re.IGNORECASE,
)
_CONSENT_ASK = re.compile(
    r"\b(opt[ -]?in|consent|permission to (text|message|contact))\b", re.IGNORECASE
)
# One concept, one ask. Extra asks are friction the copywriter cannot remove.
_ASK = re.compile(r"\?|\b(also|and then|plus|additionally|as well as)\b", re.IGNORECASE)

# A concept the copywriter must fit into ONE ~160-char SMS. The concept itself
# is a brief, not the copy, so the ceiling is generous — it only catches
# concepts that are structurally too big for one text.
SMS_CONCEPT_CHAR_CEILING = 320
# Near-duplicate of something this Pro already got. difflib ratio, not
# embeddings: no new dependency, and the threshold is tunable on real data.
REPETITION_RATIO = 0.80
# Bench below this. Set by fiat, NOT calibrated: on the fixture corpus a fully
# grounded single-ask concept scores 1.0 and ungrounded three-ask boilerplate
# scores 0.58, so 0.75 separates them. Re-derive it against labeled candidates
# (persona reaction as the label) before this number decides real spend.
PASS_SCORE = 0.75

# Value vocabulary meaning "we have no signal here", not a fact about the Pro.
_EMPTY_VALUES = frozenset({"none", "never", "asked", "unknown", "current", "band", "state"})

# ponytail: flat weights over the five soft dimensions; re-weight once a real
# corpus shows which ones predict persona reactions.
_WEIGHTS = {
    "single_ask": 0.25,
    "grounded_in_brief": 0.25,
    "channel_fit": 0.20,
    "concrete": 0.15,
    "distinct": 0.15,
}


@dataclass(frozen=True)
class QualityResult:
    score: float
    block_kind: str | None  # a hard block: bench before any spend
    reason: str
    dimensions: dict[str, float] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.block_kind is None and self.score >= PASS_SCORE


def _surfaces(idea: Recommendation) -> str:
    """Only what a Pro could ever see. manager_rationale/risk are internal and
    may legitimately say "churn" — grading them would bench good ideas."""
    return " ".join([idea.title, idea.pro_facing_concept, *idea.actions])


def brief_vocabulary(brief: OrgBrief) -> set[str]:
    """Words the brief actually gives us about THIS Pro. Only VALUES count:
    field names are identical for every Pro, so matching one ("feature",
    "plan", "email") proves nothing about personalization."""
    words: set[str] = set()
    for name, value in brief.model_dump().items():
        if name == "org_uuid" or not isinstance(value, str) or not value:
            continue
        for token in re.split(r"[_\W]+", value.lower()):
            if len(token) > 3 and token not in _EMPTY_VALUES:
                words.add(token)
    return words


def _distinctness(concept: str, prior_concepts: list[str]) -> float:
    if not prior_concepts:
        return 1.0
    closest = max(
        SequenceMatcher(None, concept.lower(), p.lower()).ratio() for p in prior_concepts
    )
    return 1.0 - closest


def evaluate(
    idea: Recommendation,
    brief: OrgBrief,
    *,
    prior_concepts: list[str] | None = None,
) -> QualityResult:
    prior = prior_concepts or []
    surfaces = _surfaces(idea)

    if _CONSENT_ASK.search(surfaces):
        return QualityResult(0.0, "consent_ask", "idea asks for messaging consent/opt-in")
    jargon = _INTERNAL_JARGON.search(surfaces)
    if jargon:
        return QualityResult(
            0.0, "internal_jargon", f"pro-facing text uses internal language {jargon.group(0)!r}"
        )
    if _distinctness(idea.pro_facing_concept, prior) < 1.0 - REPETITION_RATIO:
        return QualityResult(
            0.0, "repetition", "concept is a near-duplicate of a touch this Pro already got"
        )

    concept = idea.pro_facing_concept
    dimensions = {
        "single_ask": 1.0 if len(idea.actions) == 1 else max(0.0, 1.0 - 0.34 * (len(idea.actions) - 1)),
        "grounded_in_brief": _overlap(concept + " " + " ".join(idea.actions), brief),
        "channel_fit": _channel_fit(idea),
        "concrete": 0.0 if _ASK.search(concept) and len(idea.actions) > 1 else 1.0,
        "distinct": _distinctness(concept, prior),
    }
    score = sum(_WEIGHTS[k] * v for k, v in dimensions.items())
    weakest = min(dimensions, key=lambda k: dimensions[k])
    return QualityResult(
        round(score, 4),
        None,
        "passes" if score >= PASS_SCORE else f"below floor; weakest dimension: {weakest}",
        dimensions,
    )


def _overlap(text: str, brief: OrgBrief) -> float:
    vocab = brief_vocabulary(brief)
    if not vocab:
        # No brief vocabulary is our gap, not the idea's: fail open.
        return 1.0
    hits = sum(1 for token in set(re.split(r"[_\W]+", text.lower())) if token in vocab)
    return min(1.0, hits / 2)  # two grounded references is full credit


def _channel_fit(idea: Recommendation) -> float:
    if idea.channel != "sms":
        return 1.0
    over = len(idea.pro_facing_concept) - SMS_CONCEPT_CHAR_CEILING
    return 1.0 if over <= 0 else max(0.0, 1.0 - over / SMS_CONCEPT_CHAR_CEILING)
