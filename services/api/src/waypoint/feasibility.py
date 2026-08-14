"""Pre-spend feasibility and policy gate (spec stage 1).

Rejects a Pro or a channel before any LLM or persona budget is spent. Two
rules, both fail-open on UNKNOWN data and fail-closed on affirmative negative
data: the audience SQL upstream is the authoritative DNC/suppression filter,
so this gate is belt-and-braces against contradictory briefs, not a re-filter.

  * consent: a channel whose consent state affirmatively reads as opted-out is
    removed; a Pro with no contactable channel abstains.
  * journey-window relevance: a brief that affirmatively contradicts the run's
    journey window (e.g. churn_risk_state=low in a churn_risk run) abstains.
"""

from dataclasses import dataclass

from waypoint.n8n import OrgBrief

# ponytail: literal negative-state vocabulary; extend when the n8n flow's real
# band vocabulary is confirmed (HUMAN-TASKS: live contract verification).
NEGATIVE_CONSENT = frozenset(
    {"opted_out", "opted-out", "unsubscribed", "suppressed", "dnc", "blocked", "revoked", "no"}
)

CONSENT_FIELD = {"sms": "sms_consent_state", "email": "email_consent_state"}

_LOW_CHURN = frozenset({"low", "none", "minimal"})


@dataclass(frozen=True)
class GateResult:
    allowed_channels: tuple[str, ...]
    blocked: bool
    reason: str | None


def _consent_blocks(brief: OrgBrief, channel: str) -> bool:
    state = getattr(brief, CONSENT_FIELD[channel], None)
    return state is not None and state.strip().lower() in NEGATIVE_CONSENT


def window_conflict(brief: OrgBrief, journey_window: str) -> str | None:
    """An affirmative contradiction between the brief and the run's window.
    Unknown/None values never conflict — the audience SQL owns targeting."""
    if journey_window == "churn_risk":
        state = (brief.churn_risk_state or "").strip().lower()
        if state in _LOW_CHURN:
            return f"churn_risk_state={state!r} contradicts churn_risk window"
    if journey_window == "onboarding":
        stage = (brief.lifecycle_stage or "").strip().lower()
        if stage and "onboard" not in stage and stage != "new":
            return f"lifecycle_stage={stage!r} is not onboarding"
    # upsell: org-context-v2 has no reliable contradiction signal; pass.
    return None


def gate_pro(brief: OrgBrief, run_channels: list[str], journey_window: str) -> GateResult:
    conflict = window_conflict(brief, journey_window)
    if conflict is not None:
        return GateResult((), True, f"journey_window_mismatch: {conflict}")
    allowed = tuple(
        c for c in run_channels if c in CONSENT_FIELD and not _consent_blocks(brief, c)
    )
    if not allowed:
        return GateResult(
            (), True, "no_contactable_channel: consent blocks every run channel"
        )
    return GateResult(allowed, False, None)
