from waypoint.feasibility import gate_pro
from waypoint.n8n import OrgBrief


def brief(**kwargs) -> OrgBrief:
    return OrgBrief(org_uuid="org-1", **kwargs)


def test_consent_blocks_channel() -> None:
    result = gate_pro(brief(sms_consent_state="opted_out"), ["sms", "email"], "churn_risk")
    assert result.allowed_channels == ("email",)
    assert not result.blocked


def test_all_channels_blocked_abstains() -> None:
    result = gate_pro(
        brief(sms_consent_state="opted_out", email_consent_state="unsubscribed"),
        ["sms", "email"],
        "churn_risk",
    )
    assert result.blocked
    assert result.reason is not None and "no_contactable_channel" in result.reason


def test_unknown_consent_passes() -> None:
    # The audience SQL is the authoritative DNC filter (design doc "Audience and
    # sending boundary"); this gate only blocks on affirmative negative signals.
    result = gate_pro(brief(), ["sms"], "churn_risk")
    assert result.allowed_channels == ("sms",)


def test_low_churn_risk_contradicts_churn_window() -> None:
    result = gate_pro(brief(churn_risk_state="low"), ["sms"], "churn_risk")
    assert result.blocked
    assert result.reason is not None and "journey_window_mismatch" in result.reason


def test_high_churn_risk_passes_churn_window() -> None:
    assert not gate_pro(brief(churn_risk_state="high"), ["sms"], "churn_risk").blocked


def test_unknown_churn_state_passes_churn_window() -> None:
    assert not gate_pro(brief(), ["sms"], "churn_risk").blocked


def test_non_onboarding_lifecycle_contradicts_onboarding_window() -> None:
    result = gate_pro(brief(lifecycle_stage="mature"), ["sms"], "onboarding")
    assert result.blocked


def test_onboarding_lifecycle_passes_onboarding_window() -> None:
    assert not gate_pro(brief(lifecycle_stage="onboarding"), ["sms"], "onboarding").blocked


def test_unknown_lifecycle_stage_passes_onboarding_window() -> None:
    # Fail-open on unknown data (same principle as churn_risk_state): an
    # unrecognized vocabulary value must not read as an affirmative
    # contradiction just because it isn't in the known-onboarding set.
    result = gate_pro(brief(lifecycle_stage="new_customer"), ["sms"], "onboarding")
    assert not result.blocked
    result = gate_pro(brief(lifecycle_stage="trial"), ["sms"], "onboarding")
    assert not result.blocked


def test_mature_lifecycle_still_blocks_onboarding_window() -> None:
    assert gate_pro(brief(lifecycle_stage="mature"), ["sms"], "onboarding").blocked
