"""Contact plan: one (Pro, channel) per org, decided before ideation.

Consent first (hard), RECO ranks what survives, Iterable is checked lazily.
Spec: docs/superpowers/specs/2026-09-25-contact-plan-design.md. Pure except
fetch_profile; never logs or stores profile values, only reason codes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import httpx

# Iterable ids (verified 2026-09-25 via /api/channels, /api/messageTypes and a
# real lcmRun-stamped Waypoint email). LCM moving channels means editing these.
SMS_CHANNEL_IDS = frozenset({62580, 141340, 141339, 62566, 116525})
SMS_MESSAGE_TYPE_IDS = frozenset({77064, 86860, 149942})
EMAIL_CHANNEL_ID = 47360  # "Marketing Channel - Updates"
EMAIL_MESSAGE_TYPE_ID = 183150  # "Retention"
RECO_MAX_AGE = timedelta(days=2)
ITERABLE_BASE_URL = "https://api.iterable.com"

_SF_BLOCKS: dict[str, tuple[tuple[str, str], ...]] = {
    "sms": (
        ("sf_sms_opt_out", "sf_sms_opt_out"),
        ("sf_sms_opted_out", "sf_contact_sms_opt_out"),
        ("sf_dnc_phone", "sf_dnc"),
        ("sf_donotcall", "sf_donotcall"),
    ),
    "email": (
        ("email_optout_suppressed", "reco_email_optout"),
        ("sf_email_opted_out", "sf_email_opt_out"),
        ("global_unsub_other", "global_unsub"),
    ),
    "call": (),
}


def _ids(values: Any) -> frozenset[int]:
    out: set[int] = set()
    for value in values or ():
        try:
            out.add(int(value))
        except (TypeError, ValueError):
            continue
    return frozenset(out)


@dataclass(frozen=True)
class Profile:
    has_phone: bool
    sms_disclaimer: bool
    dnc: bool
    unsub_channels: frozenset[int]
    unsub_message_types: frozenset[int]

    @classmethod
    def from_iterable(cls, user: Mapping[str, Any]) -> Profile:
        fields = user.get("dataFields") or {}
        return cls(
            has_phone=bool(fields.get("phoneNumber") or user.get("phoneNumber")),
            sms_disclaimer=fields.get("receivedSMSDisclaimer") is True,
            dnc=str(fields.get("dnc_flag") or "").strip() != "",
            unsub_channels=_ids(fields.get("unsubscribedChannelIds")),
            unsub_message_types=_ids(fields.get("unsubscribedMessageTypeIds")),
        )


ProfileFetcher = Callable[[str], Awaitable[Profile | None]]


@dataclass(frozen=True)
class ContactPlan:
    pro_uuid: str | None
    channel: str | None
    open_channels: tuple[str, ...]
    source: str  # reco_default | reco_fallback | no_reco | legacy | none
    rank_score: float | None = None
    flags: tuple[str, ...] = ()
    runner_up: tuple[str, str] | None = None
    blocked: tuple[tuple[str, str, str], ...] = ()
    reco_scoring_date: str | None = None
    abstain_reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "pro_uuid": self.pro_uuid, "channel": self.channel,
            "open_channels": list(self.open_channels), "source": self.source,
            "rank_score": self.rank_score, "flags": list(self.flags),
            "runner_up": list(self.runner_up) if self.runner_up else None,
            "blocked": [list(item) for item in self.blocked],
            "reco_scoring_date": self.reco_scoring_date,
            "abstain_reason": self.abstain_reason,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ContactPlan:
        runner = data.get("runner_up")
        return cls(
            pro_uuid=data.get("pro_uuid"), channel=data.get("channel"),
            open_channels=tuple(data.get("open_channels") or ()),
            source=str(data.get("source") or "none"), rank_score=data.get("rank_score"),
            flags=tuple(data.get("flags") or ()),
            runner_up=(str(runner[0]), str(runner[1])) if runner else None,
            blocked=tuple(
                (str(a), str(b), str(c)) for a, b, c in data.get("blocked") or ()
            ),
            reco_scoring_date=data.get("reco_scoring_date"),
            abstain_reason=data.get("abstain_reason"),
        )


def legacy_plan(pro_uuid: str | None, channels: Sequence[str]) -> ContactPlan:
    """Today's behaviour, unchanged: the flow's contact Pro, first allowed channel."""
    allowed = tuple(channels)
    return ContactPlan(
        pro_uuid=pro_uuid, channel=allowed[0] if allowed else None,
        open_channels=allowed, source="legacy",
    )


def _sf_block(candidate: Mapping[str, Any], channel: str) -> str | None:
    if channel in ("sms", "call") and not candidate.get("has_phone"):
        return "no_phone"
    for key, reason in _SF_BLOCKS[channel]:
        if candidate.get(key):
            return reason
    return None


def _profile_block(profile: Profile | None, channel: str) -> str | None:
    if profile is None:
        return "no_iterable_profile"
    if channel == "email":
        if (EMAIL_CHANNEL_ID in profile.unsub_channels
                or EMAIL_MESSAGE_TYPE_ID in profile.unsub_message_types):
            return "iterable_email_unsub"
        return None
    if not profile.has_phone:
        return "iterable_no_phone"
    if not profile.sms_disclaimer:
        return "no_sms_disclaimer"
    if profile.dnc:
        return "iterable_dnc"
    if (profile.unsub_channels & SMS_CHANNEL_IDS
            or profile.unsub_message_types & SMS_MESSAGE_TYPE_IDS):
        return "iterable_sms_unsub"
    return None


def _reco_fresh(candidates: Sequence[Mapping[str, Any]], today: date) -> bool:
    for candidate in candidates:
        raw = candidate.get("reco_scoring_date")
        try:
            scored = date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        if today - scored <= RECO_MAX_AGE:
            return True
    return False


def _score(candidate: Mapping[str, Any], channel: str, fresh: bool) -> float | None:
    value = candidate.get(f"pct_{channel}") if fresh else None
    return float(value) if isinstance(value, int | float) else None


def _rank_key(pair: tuple[Mapping[str, Any], str], fresh: bool) -> tuple[Any, ...]:
    candidate, channel = pair
    score = _score(candidate, channel, fresh)
    p = candidate.get(f"p_{channel}") if fresh else None
    return (
        score is None, -(score or 0.0), -(float(p) if isinstance(p, int | float) else 0.0),
        not candidate.get("is_poc"), not candidate.get("founding_pro"),
        str(candidate.get("pro_uuid")),
    )


class _Checker:
    """Hard-consent check per pair, fetching each Pro's profile at most once."""

    def __init__(self, fetch: ProfileFetcher | None) -> None:
        self.fetch = fetch
        self.profiles: dict[str, Profile | None] = {}
        self.blocked: list[tuple[str, str, str]] = []

    async def profile(self, pro: str) -> Profile | None:
        if self.fetch is not None and pro not in self.profiles:
            self.profiles[pro] = await self.fetch(pro)
        return self.profiles.get(pro)

    async def reason(self, candidate: Mapping[str, Any], channel: str) -> str | None:
        pro = str(candidate["pro_uuid"])
        reason = _sf_block(candidate, channel)
        if reason is None and channel in ("sms", "email"):
            if self.fetch is None:
                reason = "iterable_unconfigured"
            else:
                reason = _profile_block(await self.profile(pro), channel)
        return reason

    async def open(self, candidate: Mapping[str, Any], channel: str) -> bool:
        reason = await self.reason(candidate, channel)
        if reason is not None:
            self.blocked.append((str(candidate["pro_uuid"]), channel, reason))
        return reason is None


def _source(candidate: Mapping[str, Any], channel: str, score: float | None) -> str:
    if score is None:
        return "no_reco"
    default = (candidate.get("org_best_channel") == channel
               and candidate.get(f"{channel}_contact_pro") == candidate.get("pro_uuid"))
    return "reco_default" if default else "reco_fallback"


async def _flags(candidate: Mapping[str, Any], channel: str, checker: _Checker) -> tuple[str, ...]:
    if channel != "call":
        return ()
    profile = await checker.profile(str(candidate["pro_uuid"]))
    dnc = bool(candidate.get("sf_dnc_phone") or candidate.get("sf_donotcall")
               or (profile is not None and profile.dnc))
    return tuple(name for name, on in (("dnc_call", dnc),
                                       ("phone_shared", bool(candidate.get("phone_shared"))))
                 if on)


async def build_plan(
    candidates: Sequence[Mapping[str, Any]],
    channels: Sequence[str],
    fetch_profile: ProfileFetcher | None,
    today: date,
    forced_pro: str | None = None,
) -> ContactPlan:
    pool = [c for c in candidates if c.get("pro_uuid")
            and c.get("recommended_action") != "suppress_all"
            and (forced_pro is None or c.get("pro_uuid") == forced_pro)]
    fresh = _reco_fresh(pool, today)
    pairs = sorted(((c, ch) for c in pool for ch in channels if ch in _SF_BLOCKS),
                   key=lambda pair: _rank_key(pair, fresh))
    checker = _Checker(fetch_profile)
    survivors: list[tuple[Mapping[str, Any], str]] = []
    for candidate, channel in pairs:
        if await checker.open(candidate, channel):
            survivors.append((candidate, channel))
            if len(survivors) == 2:
                break
    if not survivors:
        return ContactPlan(None, None, (), "none", blocked=tuple(checker.blocked),
                           abstain_reason="no_contactable_pair" if pool else "no_eligible_admin")
    chosen, channel = survivors[0]
    open_channels = tuple([ch for ch in channels if ch in _SF_BLOCKS
                           and await checker.reason(chosen, ch) is None])
    score = _score(chosen, channel, fresh)
    runner = survivors[1] if len(survivors) > 1 else None
    return ContactPlan(
        pro_uuid=str(chosen["pro_uuid"]), channel=channel, open_channels=open_channels,
        source=_source(chosen, channel, score), rank_score=score,
        flags=await _flags(chosen, channel, checker),
        runner_up=(str(runner[0]["pro_uuid"]), runner[1]) if runner else None,
        blocked=tuple(checker.blocked),
        reco_scoring_date=str(chosen.get("reco_scoring_date") or "") or None,
    )


def make_profile_client(api_key: str) -> httpx.AsyncClient:
    # Short timeout: this sits on the job's critical path, unlike the poller.
    return httpx.AsyncClient(
        base_url=ITERABLE_BASE_URL,
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=False,
        headers={"Api-Key": api_key},
    )


async def fetch_profile(client: httpx.AsyncClient, pro_uuid: str) -> Profile | None:
    """READ-ONLY GET of one Iterable profile. None = no profile (fail closed for
    sms/email); any other failure raises so the job requeues."""
    response = await client.get(f"/api/users/byUserId/{quote(pro_uuid, safe='')}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    user = (response.json() or {}).get("user")
    return Profile.from_iterable(user) if isinstance(user, Mapping) and user else None
