"""Contact plan selector: consent first, RECO rank, lazy Iterable checks."""

import re
from datetime import date
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from waypoint.contact_plan import (
    ContactPlan,
    Profile,
    build_plan,
    fetch_profile,
    legacy_plan,
    make_profile_client,
)

TODAY = date(2026, 9, 25)
OK_PROFILE = Profile(
    has_phone=True, sms_disclaimer=True, dnc=False,
    unsub_channels=frozenset(), unsub_message_types=frozenset(),
)


def cand(pro: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "pro_uuid": pro, "founding_pro": False, "has_phone": True, "phone_shared": False,
        "is_poc": False, "recommended_action": "email",
        "p_email": 0.1, "p_sms": 0.1, "p_call": 0.1,
        "pct_email": 0.5, "pct_sms": 0.5, "pct_call": 0.5,
        "email_optout_suppressed": False, "reco_scoring_date": "2026-09-25",
        "org_best_channel": None, "email_contact_pro": None, "sms_contact_pro": None,
        "call_contact_pro": None,
        "sf_sms_opt_out": False, "sf_dnc_phone": False, "sf_donotcall": False,
        "sf_email_opted_out": False, "sf_sms_opted_out": False, "global_unsub_other": False,
    }
    return {**base, **over}


def fetcher(profiles: dict[str, Profile | None], calls: list[str] | None = None):
    async def fetch(pro: str) -> Profile | None:
        if calls is not None:
            calls.append(pro)
        return profiles.get(pro)
    return fetch


async def test_rank_one_is_recos_org_default() -> None:
    a = cand("pro_a", pct_sms=0.9, org_best_channel="sms", sms_contact_pro="pro_a")
    b = cand("pro_b", pct_email=0.8)
    plan = await build_plan([a, b], ["sms", "email", "call"],
                            fetcher({"pro_a": OK_PROFILE, "pro_b": OK_PROFILE}), TODAY)
    assert (plan.pro_uuid, plan.channel, plan.source) == ("pro_a", "sms", "reco_default")
    assert plan.runner_up == ("pro_b", "email")


async def test_blocked_default_falls_back_to_next_admin() -> None:
    unsub = Profile(True, True, False, frozenset({62580}), frozenset())
    a = cand("pro_a", pct_sms=0.9, org_best_channel="sms", sms_contact_pro="pro_a")
    b = cand("pro_b", pct_sms=0.7)
    plan = await build_plan([a, b], ["sms"], fetcher({"pro_a": unsub, "pro_b": OK_PROFILE}), TODAY)
    assert (plan.pro_uuid, plan.channel, plan.source) == ("pro_b", "sms", "reco_fallback")
    assert ("pro_a", "sms", "iterable_sms_unsub") in plan.blocked


async def test_suppress_all_drops_only_that_pro() -> None:
    a = cand("pro_a", recommended_action="suppress_all", pct_email=0.99)
    b = cand("pro_b")
    plan = await build_plan([a, b], ["email"], fetcher({"pro_b": OK_PROFILE}), TODAY)
    assert plan.pro_uuid == "pro_b"


@pytest.mark.parametrize("field", [
    "sf_sms_opt_out", "sf_dnc_phone", "sf_donotcall", "sf_sms_opted_out",
])
async def test_salesforce_flags_block_sms(field: str) -> None:
    plan = await build_plan([cand("pro_a", **{field: True})], ["sms"],
                            fetcher({"pro_a": OK_PROFILE}), TODAY)
    assert plan.pro_uuid is None and plan.abstain_reason == "no_contactable_pair"


@pytest.mark.parametrize("profile,reason", [
    (None, "no_iterable_profile"),
    (Profile(False, True, False, frozenset(), frozenset()), "iterable_no_phone"),
    (Profile(True, False, False, frozenset(), frozenset()), "no_sms_disclaimer"),
    (Profile(True, True, True, frozenset(), frozenset()), "iterable_dnc"),
    (Profile(True, True, False, frozenset(), frozenset({86860})), "iterable_sms_unsub"),
])
async def test_iterable_lcm_rule_blocks_sms(profile: Profile | None, reason: str) -> None:
    plan = await build_plan([cand("pro_a")], ["sms"], fetcher({"pro_a": profile}), TODAY)
    assert ("pro_a", "sms", reason) in plan.blocked


@pytest.mark.parametrize("profile", [
    Profile(True, True, False, frozenset({47360}), frozenset()),
    Profile(True, True, False, frozenset(), frozenset({183150})),
])
async def test_email_blocked_by_waypoint_email_channel(profile: Profile) -> None:
    plan = await build_plan([cand("pro_a")], ["email"], fetcher({"pro_a": profile}), TODAY)
    assert ("pro_a", "email", "iterable_email_unsub") in plan.blocked


async def test_email_marketing_unsub_elsewhere_does_not_block() -> None:
    profile = Profile(True, True, False, frozenset({48948, 98324}), frozenset({124327}))
    plan = await build_plan([cand("pro_a")], ["email"], fetcher({"pro_a": profile}), TODAY)
    assert plan.channel == "email"


@pytest.mark.parametrize("field", [
    "email_optout_suppressed", "sf_email_opted_out", "global_unsub_other",
])
async def test_snowflake_flags_block_email(field: str) -> None:
    plan = await build_plan([cand("pro_a", **{field: True})], ["email"],
                            fetcher({"pro_a": OK_PROFILE}), TODAY)
    assert plan.pro_uuid is None


async def test_dnc_flags_call_but_never_blocks_it() -> None:
    plan = await build_plan([cand("pro_a", sf_dnc_phone=True, phone_shared=True)], ["call"],
                            fetcher({"pro_a": OK_PROFILE}), TODAY)
    assert plan.channel == "call"
    assert set(plan.flags) == {"dnc_call", "phone_shared"}


async def test_call_needs_a_phone() -> None:
    plan = await build_plan([cand("pro_a", has_phone=False)], ["call"],
                            fetcher({}), TODAY)
    assert plan.pro_uuid is None


async def test_iterable_unconfigured_leaves_call_only() -> None:
    plan = await build_plan([cand("pro_a", pct_sms=0.9, pct_call=0.2)], ["sms", "call"],
                            None, TODAY)
    assert plan.channel == "call"
    assert ("pro_a", "sms", "iterable_unconfigured") in plan.blocked


async def test_profiles_are_fetched_lazily_and_once() -> None:
    calls: list[str] = []
    a = cand("pro_a", pct_email=0.9, pct_sms=0.8)
    b = cand("pro_b", pct_email=0.1, pct_sms=0.1)
    await build_plan([a, b], ["sms", "email"],
                     fetcher({"pro_a": OK_PROFILE, "pro_b": OK_PROFILE}, calls), TODAY)
    assert calls == ["pro_a"]


async def test_tiebreak_poc_then_founder_then_uuid() -> None:
    a = cand("pro_a")
    b = cand("pro_b", founding_pro=True)
    c = cand("pro_c", is_poc=True)
    plan = await build_plan([a, b, c], ["call"], fetcher({}), TODAY)
    assert plan.pro_uuid == "pro_c"


async def test_stale_or_missing_reco_is_no_reco() -> None:
    old = cand("pro_a", reco_scoring_date="2026-09-20", pct_call=0.9)
    plan = await build_plan([old, cand("pro_b", founding_pro=True, reco_scoring_date="2026-09-20")],
                            ["call"], fetcher({}), TODAY)
    assert plan.source == "no_reco" and plan.pro_uuid == "pro_b"


async def test_forced_pro_limits_candidates() -> None:
    plan = await build_plan([cand("pro_a", pct_call=0.9), cand("pro_b")], ["call"],
                            fetcher({}), TODAY, forced_pro="pro_b")
    assert plan.pro_uuid == "pro_b"


async def test_open_channels_lists_consented_channels_for_the_chosen_pro() -> None:
    a = cand("pro_a", pct_email=0.9, sf_sms_opt_out=True)
    plan = await build_plan([a], ["sms", "email", "call"], fetcher({"pro_a": OK_PROFILE}), TODAY)
    assert plan.channel == "email"
    assert plan.open_channels == ("email", "call")


def test_legacy_plan_keeps_todays_behaviour() -> None:
    plan = legacy_plan("pro_x", ["sms", "email"])
    assert (plan.pro_uuid, plan.channel, plan.source) == ("pro_x", "sms", "legacy")
    assert plan.open_channels == ("sms", "email")


def test_plan_round_trips_through_json() -> None:
    plan = ContactPlan(pro_uuid="pro_a", channel="sms", open_channels=("sms",),
                       source="reco_default", flags=("phone_shared",),
                       runner_up=("pro_b", "email"), blocked=(("pro_c", "sms", "sf_dnc"),))
    assert ContactPlan.from_json(plan.to_json()) == plan


def test_profile_from_iterable_reads_only_consent_fields() -> None:
    user = {"email": "x@example.com", "dataFields": {
        "phoneNumber": "+15555550100", "receivedSMSDisclaimer": True, "dnc_flag": " ",
        "unsubscribedChannelIds": [62580, "47360"], "unsubscribedMessageTypeIds": [183150],
        "firstName": "ignored"}}
    profile = Profile.from_iterable(user)
    assert profile == Profile(True, True, False, frozenset({62580, 47360}), frozenset({183150}))


USER_URL = re.compile(r"https://api\.iterable\.com/api/users/byUserId/.*")


async def test_fetch_profile_reads_the_user(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=USER_URL, method="GET", json={"user": {"dataFields": {
        "phoneNumber": "+15555550100", "receivedSMSDisclaimer": True}}})
    async with make_profile_client("it-key") as client:
        profile = await fetch_profile(client, "pro_a")
    assert profile is not None and profile.sms_disclaimer
    request = httpx_mock.get_request()
    assert request is not None and request.headers["Api-Key"] == "it-key"
    assert request.url.path == "/api/users/byUserId/pro_a"


@pytest.mark.parametrize("status,body", [(404, {}), (200, {}), (200, {"user": None})])
async def test_fetch_profile_missing_user_is_none(
    httpx_mock: HTTPXMock, status: int, body: dict[str, Any]
) -> None:
    httpx_mock.add_response(url=USER_URL, status_code=status, json=body)
    async with make_profile_client("it-key") as client:
        assert await fetch_profile(client, "pro_a") is None


async def test_fetch_profile_server_error_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(url=USER_URL, status_code=503)
    async with make_profile_client("it-key") as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_profile(client, "pro_a")
