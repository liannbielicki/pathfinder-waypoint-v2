from __future__ import annotations

import pytest


def test_minimize_keeps_only_allowlisted_fields():
    from pathfinder.action_console.org_context_contract import minimize_row

    # org_uuid, snapshot_date, source_row_hash are plausible correlation
    # columns a real query would return alongside the contract fields — none
    # of them are allowlisted or forbidden, so they must be dropped silently.
    row = {
        "org_uuid": "00000000-0000-0000-0000-000000000001",
        "open_ar_band": "1k_5k",
        "snapshot_date": "2026-07-29",
        "source_row_hash": "abc123",
    }
    out = minimize_row(row)
    assert out == {"open_ar_band": "1k_5k"}
    assert "org_uuid" not in out
    assert "snapshot_date" not in out
    assert "source_row_hash" not in out


def test_minimize_raises_on_forbidden_even_if_otherwise_unrecognized():
    from pathfinder.action_console.org_context_contract import (
        ContractViolation,
        minimize_row,
    )

    # Forbidden fields must raise even though, absent the forbidden check,
    # they would simply be dropped as unrecognized (not allowlisted) — the
    # contract must fail loudly on a query shape change, never silently drop.
    with pytest.raises(ContractViolation) as excinfo:
        minimize_row(
            {
                "customer_email": "pro@example.com",
                "invoice_id": "inv_991",
                "open_ar_band": "1k_5k",
            }
        )
    message = str(excinfo.value)
    assert "customer_email" in message
    assert "invoice_id" in message


def test_minimize_rejects_forbidden_field_loudly():
    from pathfinder.action_console.org_context_contract import (
        ContractViolation,
        minimize_row,
    )

    # A forbidden field is a contract breach, not something to silently drop:
    # it means the upstream query changed shape.
    with pytest.raises(ContractViolation) as excinfo:
        minimize_row({"message_body": "hi there", "open_ar_band": "0"})
    assert "message_body" in str(excinfo.value)


def test_minimize_stringifies_values():
    from pathfinder.action_console.org_context_contract import minimize_row

    out = minimize_row({"outreach_count_28d_band": 3})
    assert out == {"outreach_count_28d_band": "3"}


def test_minimize_drops_none_and_blank():
    from pathfinder.action_console.org_context_contract import minimize_row

    out = minimize_row({"open_ar_band": None, "sms_consent_state": ""})
    assert out == {}


def test_allowed_and_forbidden_never_overlap():
    from pathfinder.action_console.org_context_contract import (
        ALLOWED_FIELDS,
        FORBIDDEN_FIELDS,
    )

    assert not (ALLOWED_FIELDS & FORBIDDEN_FIELDS)


def test_contract_version_is_set():
    from pathfinder.action_console.org_context_contract import CONTRACT_VERSION

    assert CONTRACT_VERSION.startswith("org-context-v")


# ---------------------------------------------------------------------------
# v2 field set
# ---------------------------------------------------------------------------


def test_v2_keeps_every_v1_field():
    """v2 ADDS to v1; it removes nothing. A dropped field would silently stop
    reaching the prompt with no failure anywhere -- minimize_row drops
    unrecognized keys by design."""
    from pathfinder.action_console.org_context_contract import ALLOWED_FIELDS

    v1_fields = {
        "feature_adoption_band",
        "plan_gap_band",
        "ltv_score_band",
        "open_ar_band",
        "ar_aging_band",
        "jobs_created_28d_band",
        "estimates_created_28d_band",
        "invoices_sent_28d_band",
        "outreach_count_28d_band",
        "sms_consent_state",
        "email_consent_state",
    }
    assert v1_fields <= ALLOWED_FIELDS
    assert len(ALLOWED_FIELDS) == 29


def test_the_ten_per_feature_state_fields_are_allowlisted():
    from pathfinder.action_console.org_context_contract import (
        ALLOWED_FIELDS,
        PER_FEATURE_STATE_FEATURES,
        PER_FEATURE_STATE_FIELDS,
    )

    assert len(PER_FEATURE_STATE_FEATURES) == 10
    assert len(set(PER_FEATURE_STATE_FEATURES)) == 10
    assert set(PER_FEATURE_STATE_FIELDS) <= ALLOWED_FIELDS
    assert PER_FEATURE_STATE_FIELDS == tuple(
        f"feature_{name}_state" for name in PER_FEATURE_STATE_FEATURES
    )


def test_unknown_usage_is_a_distinct_state_from_unused():
    """The distinction v2 exists for. Collapsing them would tell the model
    "they pay for this and don't use it" about a feature we cannot observe."""
    from pathfinder.action_console.org_context_contract import FEATURE_STATES

    assert FEATURE_STATES == {
        "not_attached",
        "attached_unused",
        "attached_active",
        "attached_usage_unknown",
    }


def test_no_v2_field_collides_with_a_forbidden_name():
    """invoice_amount and payment_method are forbidden. Any v2 signal derived
    from those must be BOTH banded and renamed -- open_ar_band, not
    invoice_amount."""
    from pathfinder.action_console.org_context_contract import (
        ALLOWED_FIELDS,
        FORBIDDEN_FIELDS,
    )

    lowered_forbidden = {name.lower() for name in FORBIDDEN_FIELDS}
    assert not ({name.lower() for name in ALLOWED_FIELDS} & lowered_forbidden)
    for name in ("invoice_amount", "payment_method", "card_last4"):
        assert name in lowered_forbidden
        assert name not in {f.lower() for f in ALLOWED_FIELDS}


def test_every_v2_field_name_reads_as_bounded():
    """A field whose name promises a raw value is a review failure regardless
    of what the SQL happens to emit today. Every allowlisted name must be a
    band, a state, a closed vocabulary, or a gated feature name."""
    from pathfinder.action_console.org_context_contract import ALLOWED_FIELDS

    allowed_bare = {
        "recommended_focus",       # gated against 34 verified names
        "top_unused_paid_feature",  # gated to the ten contract features
        "vertical",                 # mapped to 16 buckets
        "plan_tier",                # gated to 5 verified tiers
    }
    for name in ALLOWED_FIELDS:
        assert (
            name.endswith("_band")
            or name.endswith("_state")
            or name in allowed_bare
        ), f"{name} does not read as a bounded value"


def test_minimize_rejects_dict_value():
    from pathfinder.action_console.org_context_contract import (
        ContractViolation,
        minimize_row,
    )

    with pytest.raises(ContractViolation) as excinfo:
        minimize_row({"open_ar_band": {"invoice_id": "inv_1", "amount": 500}})
    assert "open_ar_band" in str(excinfo.value)


def test_minimize_rejects_list_value():
    from pathfinder.action_console.org_context_contract import (
        ContractViolation,
        minimize_row,
    )

    with pytest.raises(ContractViolation) as excinfo:
        minimize_row({"ltv_score_band": ["a", "b"]})
    assert "ltv_score_band" in str(excinfo.value)


def test_minimize_rejects_bytes_value():
    from pathfinder.action_console.org_context_contract import (
        ContractViolation,
        minimize_row,
    )

    with pytest.raises(ContractViolation) as excinfo:
        minimize_row({"sms_consent_state": b"opted_in"})
    assert "sms_consent_state" in str(excinfo.value)


def test_minimize_accepts_boolean_value():
    from pathfinder.action_console.org_context_contract import minimize_row

    out = minimize_row({"sms_consent_state": True})
    assert out == {"sms_consent_state": "True"}


def test_minimize_raises_on_miscased_forbidden_field():
    from pathfinder.action_console.org_context_contract import (
        ContractViolation,
        minimize_row,
    )

    with pytest.raises(ContractViolation) as excinfo:
        minimize_row({"Customer_Email": "pro@example.com", "open_ar_band": "1k_5k"})
    assert "customer_email" in str(excinfo.value).lower()


def test_minimize_drops_unknown_case_insensitively_and_whitespace():
    from pathfinder.action_console.org_context_contract import minimize_row

    out = minimize_row(
        {
            "plan_gap_band": "unknown",
            "ltv_score_band": "UNKNOWN",
            "open_ar_band": "   ",
        }
    )
    assert out == {}


# --- Final review wave: findings 1 and 7 ---


def test_minimize_matches_allowlist_case_insensitively():
    """Finding 1a: Snowflake returns column names UPPERCASE natively. An
    uppercase allowlisted column must be recognized and emitted under the
    canonical lowercase name, not dropped as unrecognized."""
    from pathfinder.action_console.org_context_contract import minimize_row

    out = minimize_row(
        {
            "org_uuid": "a",
            "OPEN_AR_BAND": "5k_10k",
            "Ltv_Score_Band": "high",
        }
    )
    assert out == {"open_ar_band": "5k_10k", "ltv_score_band": "high"}


def test_minimize_raises_when_no_allowlisted_field_matches():
    """Finding 1b: a row that matches NO allowlisted field is a query-shape
    failure. Returning {} would produce an empty context stamped with an
    approved contract version and no logged cause."""
    from pathfinder.action_console.org_context_contract import (
        ContractViolation,
        minimize_row,
    )

    with pytest.raises(ContractViolation) as excinfo:
        minimize_row({"org_uuid": "a", "open_ar_bnd": "5k_10k"})
    message = str(excinfo.value)
    assert "open_ar_bnd" in message
    assert "org_uuid" in message
    # Names only -- never the values, which the caller does not scrub for shape
    # errors raised out of this module.
    assert "5k_10k" not in message


def test_minimize_returns_empty_dict_when_allowlisted_values_are_all_blank():
    """Finding 1b, other half: allowlisted keys ARE present and every value is
    blank/unknown/None. That is a legitimate empty result, not a shape problem,
    so it must return {} without raising."""
    from pathfinder.action_console.org_context_contract import minimize_row

    out = minimize_row(
        {
            "org_uuid": "a",
            "open_ar_band": None,
            "ltv_score_band": "unknown",
            "sms_consent_state": "  ",
        }
    )
    assert out == {}


def test_minimize_rejects_disagreeing_contract_version():
    """Finding 7: a row honestly stamped with ANOTHER version must be refused,
    not silently relabelled with this build's own version.

    The disagreeing version is derived from CONTRACT_VERSION rather than
    hardcoded, so this keeps testing refusal across a version bump instead of
    accidentally asserting the CURRENT version is refused."""
    from pathfinder.action_console.org_context_contract import (
        CONTRACT_VERSION,
        ContractViolation,
        minimize_row,
    )

    other = CONTRACT_VERSION + "-not-this-one"
    with pytest.raises(ContractViolation) as excinfo:
        minimize_row({"contract_version": other, "open_ar_band": "1k_5k"})
    message = str(excinfo.value)
    assert other in message
    assert CONTRACT_VERSION in message


def test_minimize_accepts_matching_contract_version():
    """Finding 7: a row declaring the enforced version is fine, and the
    declaration itself is not emitted as context."""
    from pathfinder.action_console.org_context_contract import (
        CONTRACT_VERSION,
        minimize_row,
    )

    out = minimize_row(
        {"contract_version": CONTRACT_VERSION, "open_ar_band": "1k_5k"}
    )
    assert out == {"open_ar_band": "1k_5k"}


def test_minimize_treats_blank_contract_version_as_undeclared():
    """Finding 7: a blank declaration is 'no declaration', not a mismatch."""
    from pathfinder.action_console.org_context_contract import minimize_row

    assert minimize_row({"contract_version": "", "open_ar_band": "1k_5k"}) == {
        "open_ar_band": "1k_5k"
    }


# --- Fix wave 2: case collision and forbidden-field class ---


def test_minimize_refuses_case_colliding_keys():
    """Fix 1: two source keys normalizing onto one allowlisted field used to
    return whichever the loop hit last, decided by column order, with nothing
    logged. During a view rename a quoted alias can coexist with the unquoted
    column, so this is reachable."""
    from pathfinder.action_console.org_context_contract import (
        ContractViolation,
        minimize_row,
    )

    with pytest.raises(ContractViolation) as excinfo:
        minimize_row({"open_ar_band": "0", "OPEN_AR_BAND": "50k_plus"})
    message = str(excinfo.value)
    assert "open_ar_band" in message
    assert "OPEN_AR_BAND" in message
    # Names only -- never the values.
    assert "50k_plus" not in message
    assert "0" not in message


def test_minimize_refuses_collision_even_when_one_value_is_blank():
    """Fix 1: the collision check runs before values are read, so a collision
    is caught even when one side would have been dropped as blank -- otherwise
    the refusal would itself depend on column content."""
    from pathfinder.action_console.org_context_contract import (
        ContractViolation,
        minimize_row,
    )

    with pytest.raises(ContractViolation):
        minimize_row({"ltv_score_band": "", "Ltv_Score_Band": "high"})


def test_minimize_reports_every_colliding_field():
    """Fix 1: two independent collisions in one row are both named."""
    from pathfinder.action_console.org_context_contract import (
        ContractViolation,
        minimize_row,
    )

    with pytest.raises(ContractViolation) as excinfo:
        minimize_row(
            {
                "open_ar_band": "0",
                "OPEN_AR_BAND": "50k_plus",
                "sms_consent_state": "opted_in",
                "SMS_CONSENT_STATE": "opted_out",
            }
        )
    message = str(excinfo.value)
    assert "open_ar_band" in message
    assert "sms_consent_state" in message


def test_minimize_accepts_a_single_key_in_any_casing():
    """Fix 1 must not break the wave-1 behavior it guards: ONE key per field is
    fine in any casing. Only an actual collision is refused."""
    from pathfinder.action_console.org_context_contract import minimize_row

    assert minimize_row({"OPEN_AR_BAND": "5k_10k"}) == {"open_ar_band": "5k_10k"}
    assert minimize_row({"Open_Ar_Band": "5k_10k"}) == {"open_ar_band": "5k_10k"}
    assert minimize_row({"open_ar_band": "5k_10k"}) == {"open_ar_band": "5k_10k"}


def test_forbidden_field_raises_the_specific_subclass():
    """Fix 2: a near-miss PII leak must be distinguishable from a shape typo so
    Phase 2 alerting can page on one and ticket the other."""
    from pathfinder.action_console.org_context_contract import (
        ForbiddenFieldViolation,
        minimize_row,
    )

    with pytest.raises(ForbiddenFieldViolation):
        minimize_row({"card_last4": "4242", "open_ar_band": "0"})


def test_forbidden_field_is_still_caught_as_contract_violation():
    """Fix 2: the subclass must not break existing `except ContractViolation`
    handlers -- including the one in snowflake_org_context.prefetch."""
    from pathfinder.action_console.org_context_contract import (
        ContractViolation,
        ForbiddenFieldViolation,
        minimize_row,
    )

    assert issubclass(ForbiddenFieldViolation, ContractViolation)
    with pytest.raises(ContractViolation):
        minimize_row({"card_last4": "4242", "open_ar_band": "0"})


def test_shape_typo_is_not_the_forbidden_subclass():
    """Fix 2, the other half: a renamed column must NOT look like a PII
    tripwire. Same for a collision and a version mismatch."""
    from pathfinder.action_console.org_context_contract import (
        CONTRACT_VERSION,
        ContractViolation,
        ForbiddenFieldViolation,
        minimize_row,
    )

    for row in (
        {"org_uuid": "a", "open_ar_bnd": "5k_10k"},
        {"open_ar_band": "0", "OPEN_AR_BAND": "50k_plus"},
        {"contract_version": CONTRACT_VERSION + "-not-this-one", "open_ar_band": "0"},
        {"open_ar_band": {"invoice_id": "inv_1"}},
    ):
        with pytest.raises(ContractViolation) as excinfo:
            minimize_row(row)
        assert not isinstance(excinfo.value, ForbiddenFieldViolation)


def test_index_rows_maps_each_requested_org():
    from pathfinder.action_console.org_context_contract import index_rows_by_org

    rows = [
        {"org_uuid": "a", "open_ar_band": "0"},
        {"org_uuid": "b", "open_ar_band": "1k_5k"},
    ]
    out = index_rows_by_org(rows, ["a", "b"])
    assert set(out) == {"a", "b"}
    assert out["b"]["open_ar_band"] == "1k_5k"


def test_index_rows_rejects_cross_org_row():
    from pathfinder.action_console.org_context_contract import (
        OrgIsolationError,
        index_rows_by_org,
    )

    rows = [{"org_uuid": "a"}, {"org_uuid": "intruder"}]
    with pytest.raises(OrgIsolationError) as excinfo:
        index_rows_by_org(rows, ["a"])
    assert "intruder" in str(excinfo.value)


def test_index_rows_rejects_duplicate_org():
    from pathfinder.action_console.org_context_contract import (
        OrgIsolationError,
        index_rows_by_org,
    )

    rows = [{"org_uuid": "a"}, {"org_uuid": "a"}]
    with pytest.raises(OrgIsolationError):
        index_rows_by_org(rows, ["a"])


def test_index_rows_rejects_missing_org():
    from pathfinder.action_console.org_context_contract import (
        OrgIsolationError,
        index_rows_by_org,
    )

    with pytest.raises(OrgIsolationError) as excinfo:
        index_rows_by_org([{"org_uuid": "a"}], ["a", "b"])
    assert "b" in str(excinfo.value)


def test_index_rows_rejects_row_without_org_uuid():
    from pathfinder.action_console.org_context_contract import (
        OrgIsolationError,
        index_rows_by_org,
    )

    with pytest.raises(OrgIsolationError):
        index_rows_by_org([{"open_ar_band": "0"}], ["a"])
