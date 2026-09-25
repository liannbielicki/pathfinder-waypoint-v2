# Contact Plan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pick one (Pro, channel) per org before ideation, consent-first and RECO-ranked, and
make ideation, the follow-up, winners, handoff and call to-dos all use it.

**Architecture:**
- A pure module `contact_plan.py` ranks (admin, channel) pairs from Snowflake candidate rows
  and checks Iterable profiles lazily, with direct GETs.
- A new pipeline stage `plan`, between `context` and `evolve`, computes it once, checkpoints
  it and reloads it on resume.
- `CONTACT_PLAN_MODE=off|shadow|enforce` gates the behaviour change. Shadow only records the
  plan; enforce pins the channel and the Pro.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async / pydantic v2 / httpx / pytest +
pytest-httpx (asyncio auto, mypy strict, ruff line length 100); Next.js + React Testing
Library in `apps/web`; n8n flow JSON in `n8n/`.

**Spec:** `docs/superpowers/specs/2026-09-25-contact-plan-design.md`. Candidate SQL:
`docs/superpowers/specs/2026-09-25-contact-candidate.sql`. Read both before starting.

## Global Constraints

- Branch: develop on `feature/channel-selection` (rebased on `V4-Improvements`). Per `CLAUDE.md`,
  V4 work lands on `V4-Improvements`, and every push redeploys Railway staging. Fast-forward it
  from the V4 worktree only when Tasks 1–8 pass. `CONTACT_PLAN_MODE` defaults to `off`, so that
  deploy is inert. Never touch `main`. Never force-push. Never run `ruff format`.
- Backend checks, from `services/api` (the repo's commands): `.venv/bin/python -m pytest -q`,
  `.venv/bin/python -m ruff check src tests`, `.venv/bin/python -m mypy src`. Run all three before
  every commit. In the steps below, `uv run X` means `.venv/bin/python -m X`.
- Iterable IDs are named constants in `contact_plan.py` only:
  - SMS channels `{62580, 141340, 141339, 62566}` and `116525`;
  - SMS message types `{77064, 86860, 149942}`;
  - email channel `47360`, email message type `183150`.
- Nothing from an Iterable profile (phone, email, name) may reach logs, prompts or checkpoints.
  Only reason codes and `pro_uuid` may.
- Salesforce `dnc_email__c` is never used as a flag.
- DNC never blocks `call`; it adds the flag `dnc_call`.
- Every commit message ends with:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Do **not** edit the live n8n workflow. Task 8 changes only the repo copy; deploying to live n8n
  is a human step (Task 10).
- Keep new files under 400 lines and functions under 50 lines.

## File map

| File | Change | Responsibility |
|---|---|---|
| `services/api/src/waypoint/contact_plan.py` | create | constants, `Profile`, `ContactPlan`, consent rules, ranking, `build_plan`, `legacy_plan`, `fetch_profile`, `make_profile_client` |
| `services/api/tests/test_contact_plan.py` | create | selector + fetcher tests |
| `services/api/src/waypoint/n8n.py` | modify | `OrgBrief.contact_candidates` (excluded field) |
| `services/api/src/waypoint/catalog.py` | modify | keep `contact_candidates` out of the Standard prompt's `unknown` list |
| `services/api/src/waypoint/staging_context.py` | modify | `_contact_candidates(rows)` |
| `services/api/src/waypoint/api.py` | modify | persist candidates in the staging checkpoint |
| `services/api/src/waypoint/settings.py` | modify | `CONTACT_PLAN_MODE` |
| `services/api/src/waypoint/pipeline.py` | modify | `plan` stage, state/deps fields, enforce consumers, winner evidence |
| `services/api/src/waypoint/worker.py` | modify | build the Iterable profile client and wire it into deps |
| `services/api/src/waypoint/handoff.py` | modify | hold back rows whose channel disagrees with the plan |
| `services/api/src/waypoint/models.py`, `call_todos.py` | modify | `CallItem.pro_uuid`, `flags` |
| `services/api/src/waypoint/workbench.py` | modify | inventory ignores `waypoint_contact_candidate` rows |
| `n8n/waypoint-variable-audit-context-async-v1.json` | modify | append the candidate block to Part 1 |
| `apps/web/src/components/WinnerReview.tsx`, `RunStart.tsx`, `apps/web/src/app/calls/page.tsx` | modify | show the plan |
| `contracts/openapi.json`, `apps/web/src/lib/api-types.ts` | regenerate | contract |
| `docs/REPO-MAP.md`, `CLAUDE.md`, `services/api/src/waypoint/feasibility.py` docstring | modify | docs |

---

### Task 1: Pure selector — consent rules, ranking, `build_plan`

**Files:**
- Create: `services/api/src/waypoint/contact_plan.py`
- Create: `services/api/tests/test_contact_plan.py`

**Interfaces:**
- Produces:
  - `Profile.from_iterable(user: Mapping[str, Any]) -> Profile`
  - `ContactPlan` (frozen dataclass), with `ContactPlan.to_json() -> dict[str, Any]` and
    `ContactPlan.from_json(data: Mapping[str, Any]) -> ContactPlan`
  - `ProfileFetcher = Callable[[str], Awaitable[Profile | None]]`
  - `async build_plan(candidates: Sequence[Mapping[str, Any]], channels: Sequence[str], fetch_profile: ProfileFetcher | None, today: date, forced_pro: str | None = None) -> ContactPlan`
  - `legacy_plan(pro_uuid: str | None, channels: Sequence[str]) -> ContactPlan`

- [ ] **Step 1: Write the failing tests**

```python
# services/api/tests/test_contact_plan.py
"""Contact plan selector: consent first, RECO rank, lazy Iterable checks."""

from datetime import date
from typing import Any

import pytest

from waypoint.contact_plan import ContactPlan, Profile, build_plan, legacy_plan

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/api && uv run pytest tests/test_contact_plan.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'waypoint.contact_plan'`.

- [ ] **Step 3: Write the implementation**

```python
# services/api/src/waypoint/contact_plan.py
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

# Iterable ids (verified 2026-09-25 via /api/channels, /api/messageTypes and a
# real lcmRun-stamped Waypoint email). LCM moving channels means editing these.
SMS_CHANNEL_IDS = frozenset({62580, 141340, 141339, 62566, 116525})
SMS_MESSAGE_TYPE_IDS = frozenset({77064, 86860, 149942})
EMAIL_CHANNEL_ID = 47360  # "Marketing Channel - Updates"
EMAIL_MESSAGE_TYPE_ID = 183150  # "Retention"
RECO_MAX_AGE = timedelta(days=2)

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
```

Append the orchestration, which keeps each function under 50 lines:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/api && uv run pytest tests/test_contact_plan.py -q && uv run mypy src/waypoint/contact_plan.py && uv run ruff check src/waypoint/contact_plan.py tests/test_contact_plan.py`
Expected: all pass, with no mypy or ruff errors. If `test_profiles_are_fetched_lazily_and_once`
fails because `open_channels` fetched `pro_b`, check that `open_channels` only calls
`checker.reason` for `chosen`.

- [ ] **Step 5: Commit**

```bash
git add services/api/src/waypoint/contact_plan.py services/api/tests/test_contact_plan.py
git commit -m "feat: contact plan selector — consent-first, RECO-ranked (Pro, channel) pick

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Iterable profile fetcher

**Files:**
- Modify: `services/api/src/waypoint/contact_plan.py` (append)
- Modify: `services/api/tests/test_contact_plan.py` (append)

**Interfaces:**
- Consumes: `Profile.from_iterable` (Task 1).
- Produces:
  - `make_profile_client(api_key: str) -> httpx.AsyncClient`
  - `async fetch_profile(client: httpx.AsyncClient, pro_uuid: str) -> Profile | None`: returns
    None on a 404 or an empty user, and raises `httpx.HTTPStatusError` on other non-2xx
    responses (so the job requeues).

- [ ] **Step 1: Write the failing tests**

```python
# append to services/api/tests/test_contact_plan.py
# (put these imports at the TOP of the file with the others — ruff E402)
#   import re
#   import httpx
#   from pytest_httpx import HTTPXMock
#   from waypoint.contact_plan import fetch_profile, make_profile_client

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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/api && uv run pytest tests/test_contact_plan.py -q -k fetch_profile`
Expected: ImportError, because `fetch_profile` isn't defined.

- [ ] **Step 3: Implement (append to `contact_plan.py`, adding `import httpx` and
  `from urllib.parse import quote` at the top)**

```python
ITERABLE_BASE_URL = "https://api.iterable.com"


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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/api && uv run pytest tests/test_contact_plan.py -q && uv run mypy src/waypoint/contact_plan.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/api/src/waypoint/contact_plan.py services/api/tests/test_contact_plan.py
git commit -m "feat: read-only Iterable profile fetch for the contact plan

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Carry candidate rows from the staging callback to the job

**Files:**
- Modify: `services/api/src/waypoint/n8n.py:132` (OrgBrief field)
- Modify: `services/api/src/waypoint/catalog.py:208-212`
- Modify: `services/api/src/waypoint/staging_context.py:271-288`
- Modify: `services/api/src/waypoint/api.py:365-372`
- Modify: `services/api/src/waypoint/workbench.py:451-458`
- Test: `services/api/tests/test_staging_context.py`, `services/api/tests/test_api.py`,
  `services/api/tests/test_workbench.py`

**Interfaces:**
- Produces: `OrgBrief.contact_candidates: list[dict[str, Any]] | None`, excluded from
  `model_dump`. It is `None` when the flow emitted no candidate rows, which means legacy.
  `_contact_candidates(rows) -> list[dict[str, Any]] | None`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_staging_context.py`)

```python
def test_staging_brief_carries_contact_candidates_outside_the_prompt() -> None:
    bundle = promotion({"source_key": "SAFE", "source_table": "UNKNOWN", "canonical_key": "safe"})
    candidate = {"pro_uuid": "pro_aaa", "pct_sms": 0.9, "sf_sms_opt_out": False}
    snowflake = [
        {"VARIABLE_NAME": "SAFE", "VALUE": 1},
        {"QUERY_NAME": "waypoint_contact_candidate", "VARIABLE_NAME": "contact_candidate",
         "VALUE": candidate},
        {"QUERY_NAME": "waypoint_contact_candidate", "VARIABLE_NAME": "contact_candidate",
         "VALUE": json.dumps({"pro_uuid": "pro_bbb"})},
    ]
    brief = compile_staging_brief("889901", snowflake, context_payload(), bundle)
    assert brief.contact_candidates == [candidate, {"pro_uuid": "pro_bbb"}]
    assert "pro_aaa" not in str(brief.curated_context)
    assert "contact_candidates" not in brief.model_dump()


def test_staging_brief_without_candidate_rows_is_legacy() -> None:
    bundle = promotion({"source_key": "SAFE", "source_table": "UNKNOWN", "canonical_key": "safe"})
    brief = compile_staging_brief("889901", [{"VARIABLE_NAME": "SAFE", "VALUE": 1}],
                                  context_payload(), bundle)
    assert brief.contact_candidates is None
```

Add `import json` at the top of the test file if it's missing. In `tests/test_api.py`, find the
existing staging-callback success test (`grep -n "staging_context\"\]\[\"brief\"\]" tests/test_api.py`),
add a `waypoint_contact_candidate` row to its Snowflake payload, and assert:

```python
assert job.checkpoint["staging_context"]["brief"]["contact_candidates"] == [{"pro_uuid": "pro_aaa"}]
```

In `tests/test_workbench.py`, next to the existing variable-inventory test, add a case asserting
that a row with `query_name="waypoint_contact_candidate"` produces no inventory item.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/api && uv run pytest tests/test_staging_context.py tests/test_api.py tests/test_workbench.py -q -k "candidate"`
Expected: FAIL, with `OrgBrief` rejecting or lacking `contact_candidates`.

- [ ] **Step 3: Implement**

`n8n.py`, just below the `curated_context` field:

```python
    # Contact-plan inputs (one dict per active admin), never context: excluded
    # from model_dump so neither prompt path can serialize them.
    contact_candidates: list[dict[str, Any]] | None = Field(default=None, exclude=True)
```

`catalog.py:208-212`: exclude it in both places:

```python
    known = brief.model_dump(mode="json", exclude_none=True, exclude={"curated_context"})
    unknown = [
        name for name in type(brief).model_fields
        if name not in {"curated_context", "contact_candidates"} and getattr(brief, name) is None
    ]
```

`staging_context.py`: add after `_contact_pro`:

```python
def _contact_candidates(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]] | None:
    """Rows from the flow's `waypoint_contact_candidate` block: identifiers and
    booleans for the contact plan. Bypass promotion like `_contact_pro`. None
    means the flow predates the block (legacy plan), not "no admins"."""
    found: list[dict[str, Any]] = []
    for row in rows:
        if str(_row_value(row, "query_name") or "") != "waypoint_contact_candidate":
            continue
        value = _row_value(row, "value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, Mapping):
            found.append(dict(value))
    return found or None
```

Add `import json` at the top. In the `return OrgBrief(...)` of `compile_staging_brief`, add
`contact_candidates=_contact_candidates(rows),`.

`api.py:365-372`: persist it explicitly, the same way as `curated_context`:

```python
            "brief": {
                **brief.model_dump(mode="json", exclude_none=True),
                "curated_context": brief.curated_context,
                **(
                    {"contact_candidates": brief.contact_candidates}
                    if brief.contact_candidates is not None
                    else {}
                ),
            },
```

`workbench.py`: in the Snowflake rows loop, right after `query = _row_value(row, "query_name")`
(move that line above the `key` check if needed):

```python
            if str(query or "") == "waypoint_contact_candidate":
                continue  # contact-plan identifiers, never promotable variables
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/api && uv run pytest -q tests/test_staging_context.py tests/test_api.py tests/test_workbench.py tests/test_catalog.py && uv run mypy src`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A services/api
git commit -m "feat: carry contact-plan candidate rows from the staging callback, outside the prompt

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The `plan` stage (off / shadow / enforce), checkpointed and resumable

**Files:**
- Modify: `services/api/src/waypoint/settings.py` (after `POLL_SECONDS`)
- Modify: `services/api/src/waypoint/pipeline.py`:
  - `STAGES` at :113
  - `PipelineDeps` at :260
  - `PipelineState` at :288
  - `_abstain_pro` at :338
  - a new `_stage_plan` after `_stage_context` at :606
  - `STAGE_HANDLERS` at :2004
  - `run_job` after `state.brief = ...` at :2139
  - the top of `_stage_evolve`
- Modify: `services/api/src/waypoint/worker.py`: `_worker_loop` params and the `PipelineDeps(...)`
  at :317, and `main` next to `lcm_client` at :433
- Modify: `services/api/tests/conftest.py`: `FakeDeps` gains `fetch_profile`
- Test: `services/api/tests/test_pipeline.py`, `services/api/tests/test_resume.py`

**Interfaces:**
- Consumes: `build_plan`, `legacy_plan`, `ContactPlan`, `ProfileFetcher` (Task 1),
  `fetch_profile`, `make_profile_client` (Task 2), `OrgBrief.contact_candidates` (Task 3).
- Produces:
  - `Settings.CONTACT_PLAN_MODE: Literal["off", "shadow", "enforce"]` (default `"off"`; Railway staging sets `shadow` then `enforce`)
  - `PipelineDeps.contact_plan_mode: str` (default `"off"`, so existing tests are unchanged)
  - `PipelineDeps.fetch_profile: ProfileFetcher | None` (default `None`)
  - `PipelineState.plan: ContactPlan | None`
  - `job.checkpoint["plan"] == {"mode": str, "plan": dict | None}`
  - `_enforced(state, deps) -> ContactPlan | None`: returns the plan only when the mode is
    enforce, the plan exists, and its source isn't legacy
  - `_abstain_pro(..., evidence: dict[str, Any] | None = None)`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_pipeline.py`; reuse the
  file's existing `seeded_job`/`deps` fixtures and its helper that sets a brief on
  `deps.context.batch`. Check how existing tests build a brief with
  `grep -n "model_copy(update" tests/test_pipeline.py | head`.)

```python
CANDIDATES = [
    {"pro_uuid": "pro_best", "has_phone": True, "pct_call": 0.9, "pct_sms": 0.1,
     "pct_email": 0.1, "reco_scoring_date": "2099-01-01"},
    {"pro_uuid": "pro_other", "has_phone": True, "pct_call": 0.2, "pct_sms": 0.2,
     "pct_email": 0.2, "reco_scoring_date": "2099-01-01"},
]


def _with_candidates(deps, candidates):
    org = deps.context.batch.organizations[0]
    deps.context.batch = deps.context.batch.model_copy(update={
        "organizations": [org.model_copy(update={"contact_candidates": candidates})]
    })


async def test_plan_stage_off_by_default_records_nothing(seeded_job, deps) -> None:
    await run_job(seeded_job.id, deps)
    job = await deps.db.get(JobRow, seeded_job.id)
    assert job.checkpoint["plan"] == {"mode": "off", "plan": None}


async def test_shadow_plan_is_recorded_but_changes_nothing(seeded_job, deps) -> None:
    deps.contact_plan_mode = "shadow"
    _with_candidates(deps, CANDIDATES)
    await run_job(seeded_job.id, deps)
    job = await deps.db.get(JobRow, seeded_job.id)
    plan = job.checkpoint["plan"]["plan"]
    # The run's channels are ["sms"]; with no fetcher, sms is blocked → abstain plan.
    assert plan["abstain_reason"] == "no_contactable_pair"
    winner = await deps.store.winner_for(seeded_job.run_id, seeded_job.pro_id)
    # Shadow never acts on the plan: no contact-plan abstain, the loop still ran.
    assert winner is not None
    assert not (winner.rationale or "").startswith("contact_plan:")
    assert deps.gateway.call_count > 0


async def test_enforce_abstains_before_any_llm_call(seeded_job, deps) -> None:
    deps.contact_plan_mode = "enforce"
    _with_candidates(deps, CANDIDATES)  # run channels ["sms"], no Iterable → nothing open
    await run_job(seeded_job.id, deps)
    winner = await deps.store.winner_for(seeded_job.run_id, seeded_job.pro_id)
    assert winner.kind == "abstained"
    assert winner.rationale.startswith("contact_plan: no_contactable_pair")
    assert winner.evidence["contact_plan"]["abstain_reason"] == "no_contactable_pair"
    assert deps.gateway.call_count == 0


async def test_shadow_plan_error_never_fails_the_job(seeded_job, deps) -> None:
    deps.contact_plan_mode = "shadow"
    _with_candidates(deps, CANDIDATES)

    async def boom(pro: str):
        raise RuntimeError("iterable down")

    deps.fetch_profile = boom
    await run_job(seeded_job.id, deps)
    job = await deps.db.get(JobRow, seeded_job.id)
    assert job.checkpoint["plan"] == {"mode": "shadow", "plan": None, "error": "RuntimeError"}
    assert deps.gateway.call_count > 0


async def test_enforce_without_candidates_is_legacy_and_runs(seeded_job, deps) -> None:
    deps.contact_plan_mode = "enforce"
    await run_job(seeded_job.id, deps)
    job = await deps.db.get(JobRow, seeded_job.id)
    assert job.checkpoint["plan"]["plan"]["source"] == "legacy"
    assert deps.gateway.call_count > 0
```


Append to `tests/test_resume.py`:

```python
async def test_plan_is_loaded_not_recomputed_on_resume(seeded_job, deps) -> None:
    deps.contact_plan_mode = "enforce"
    org = deps.context.batch.organizations[0]
    deps.context.batch = deps.context.batch.model_copy(update={"organizations": [
        org.model_copy(update={"contact_candidates": [
            {"pro_uuid": "pro_best", "has_phone": True, "pct_call": 0.9,
             "reco_scoring_date": "2099-01-01"}]})]})
    run = await deps.db.get(RunRow, seeded_job.run_id)
    run.channels = ["call"]
    await deps.db.commit()
    deps.fail_after("plan")
    with pytest.raises(InjectedCrash):
        await run_job(seeded_job.id, deps)
    deps.clear_failure()
    # Different candidates on resume must not change the pinned plan.
    deps.context.batch = deps.context.batch.model_copy(update={"organizations": [
        org.model_copy(update={"contact_candidates": [
            {"pro_uuid": "pro_new", "has_phone": True, "pct_call": 0.99,
             "reco_scoring_date": "2099-01-01"}]})]})
    await run_job(seeded_job.id, deps)
    job = await deps.db.get(JobRow, seeded_job.id)
    assert job.checkpoint["plan"]["plan"]["pro_uuid"] == "pro_best"
```

(Import `InjectedCrash`, `RunRow` and `JobRow` the way the existing resume tests do.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/api && uv run pytest -q tests/test_pipeline.py tests/test_resume.py -k "plan"`
Expected: FAIL (`KeyError: 'plan'` / no attribute `contact_plan_mode`).

- [ ] **Step 3: Implement**

`settings.py` (import `Literal` from `typing` if it's missing):

```python
    # Contact plan (docs/superpowers/specs/2026-09-25-contact-plan-design.md):
    # off = not computed; shadow = computed + recorded, behaviour unchanged;
    # enforce = the pinned (Pro, channel) drives ideation, follow-up and handoff.
    CONTACT_PLAN_MODE: Literal["off", "shadow", "enforce"] = "off"
```

`pipeline.py`:
- `STAGES = ("context", "plan", "evolve", "final", "score", "measure", "ready")`
- import `from waypoint.contact_plan import ContactPlan, ProfileFetcher, build_plan, legacy_plan`,
  plus `from datetime import date` if it's missing.
- `PipelineDeps`: add `contact_plan_mode: str = "off"` and
  `fetch_profile: ProfileFetcher | None = None`.
- `PipelineState`: add `plan: ContactPlan | None = None`.

`_abstain_pro` gains `evidence: dict[str, Any] | None = None` and passes `evidence=evidence or {}`
to `WinnerRow`.

Add after `_stage_context`:

```python
def _enforced(state: PipelineState, deps: PipelineDeps) -> ContactPlan | None:
    plan = state.plan
    if deps.contact_plan_mode != "enforce" or plan is None or plan.source == "legacy":
        return None
    return plan


def _legacy_pro(state: PipelineState) -> str | None:
    if state.brief is not None and state.brief.pro_uuid:
        return state.brief.pro_uuid
    return state.pro_id if state.pro_id.startswith("pro_") else None


async def _stage_plan(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
    """Decide once who to contact and how, before any LLM spend. A job that
    already started evolving before this stage existed keeps today's pick."""
    mode = deps.contact_plan_mode
    if mode == "off" or state.brief is None:
        return {"mode": mode, "plan": None}
    channels = list(state.run.channels)
    candidates = state.brief.contact_candidates
    if "evolve" in state.job.checkpoint or candidates is None:
        plan = legacy_plan(_legacy_pro(state), channels)
    else:
        forced = state.pro_id if state.pro_id.startswith("pro_") else None
        try:
            plan = await build_plan(candidates, channels, deps.fetch_profile,
                                    date.today(), forced_pro=forced)
        except Exception as error:  # noqa: BLE001 - shadow must never change a run
            if mode != "shadow":
                raise  # enforce: the backstop requeues; consent is never guessed
            log.warning("shadow contact plan failed for job %s: %r", state.job.id, error)
            return {"mode": mode, "plan": None, "error": type(error).__name__}
    log.info("contact plan job=%s mode=%s pro=%s channel=%s source=%s blocked=%s",
             state.job.id, mode, plan.pro_uuid, plan.channel, plan.source,
             sorted({reason for _, _, reason in plan.blocked}))
    state.plan = plan
    if _enforced(state, deps) is not None and plan.pro_uuid is None:
        await _abstain_pro(state, deps, state.pro_id,
                           f"contact_plan: {plan.abstain_reason}",
                           evidence={"contact_plan": plan.to_json()})
    return {"mode": mode, "plan": plan.to_json()}
```

Register `"plan": _stage_plan` in `STAGE_HANDLERS`, after `"context"`.

At the top of `_stage_evolve`, right after `if brief is None: return {"skipped": "no_brief"}`:

```python
    if await deps.store.winner_for(state.run.id, state.pro_id) is not None:
        return {"skipped": "decided"}  # e.g. the contact plan abstained
```

In `run_job`, right after the `state.brief = next(...)` assignment:

```python
    saved_plan = job.checkpoint.get("plan")
    if isinstance(saved_plan, Mapping) and isinstance(saved_plan.get("plan"), Mapping):
        state.plan = ContactPlan.from_json(saved_plan["plan"])
```

`conftest.py` `FakeDeps.__init__`: nothing is needed, because the defaults are `off` and `None`.
Tests set `deps.contact_plan_mode` and `deps.fetch_profile` directly.

`worker.py`:
- In `main`, next to `lcm_client`:

  ```python
      # Read-only Iterable profile lookups for the contact plan; None => call-only plans.
      iterable_profiles = (
          make_profile_client(settings.ITERABLE_API_KEY.get_secret_value())
          if settings.ITERABLE_API_KEY is not None else None
      )
      if iterable_profiles is None:
          log.info("ITERABLE_API_KEY unset; contact plans are call-only")
  ```

- Pass `iterable_profiles=iterable_profiles` into `_worker_loop` and add the parameter
  `iterable_profiles: httpx.AsyncClient | None`.
- In `PipelineDeps(...)` add:

  ```python
                      contact_plan_mode=settings.CONTACT_PLAN_MODE,
                      fetch_profile=(
                          partial(fetch_profile, iterable_profiles)
                          if iterable_profiles is not None else None
                      ),
  ```

- Close it with `await iterable_profiles.aclose()` where `lcm_client` is closed. If `lcm_client`
  isn't closed explicitly, follow whatever that code does.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/api && uv run pytest -q && uv run mypy src && uv run ruff check src tests`
Expected: PASS. Existing tests that assert the exact checkpoint keys or `STAGES` (grep `STAGES`
and `"context", "evolve"` in `tests/`) need `"plan"` added. That's the only acceptable edit to
existing tests in this task.

- [ ] **Step 5: Commit**

```bash
git add -A services/api
git commit -m "feat: contact plan pipeline stage (off/shadow/enforce), checkpointed, resume-safe

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Enforce — pin the channel, the follow-up and the Pro; take RECO out of the LLM layers

**Files:**
- Modify: `services/api/src/waypoint/pipeline.py`:
  - a new `_apply_plan`
  - `_stage_plan`
  - `run_job` (the resume load)
  - `_stage_evolve` at :1141
  - `_verdicts_for_batch` at :873-916
  - winner evidence at :1790-1805
  - `_attach_follow_up` at :1845
- Test: `services/api/tests/test_pipeline.py`

**Interfaces:**
- Consumes: `_enforced`, `PipelineState.plan` (Task 4).
- Produces: under enforce,
  - `WinnerRow.evidence["pro_uuid"] == plan.pro_uuid`
  - `evidence["contact_plan"] == plan.to_json()`
  - under shadow, `evidence["contact_plan_shadow"] == plan.to_json()`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_pipeline.py`)

```python
async def _enforce_call_plan(seeded_job, deps) -> None:
    deps.contact_plan_mode = "enforce"
    _with_candidates(deps, CANDIDATES)
    run = await deps.db.get(RunRow, seeded_job.run_id)
    run.channels = ["sms", "email", "call"]
    await deps.db.commit()


async def test_enforce_pins_generation_to_the_plan_channel(seeded_job, deps) -> None:
    await _enforce_call_plan(seeded_job, deps)
    await run_job(seeded_job.id, deps)
    prompts = [p for p in deps.gateway.prompts if "Delivery for this Pro is gated to" in p]
    assert prompts and all('gated to "call"' in p for p in prompts)
    assert all("suggested_channel" not in p for p in deps.gateway.prompts)


async def test_enforce_winner_carries_the_plan_pro(seeded_job, deps) -> None:
    await _enforce_call_plan(seeded_job, deps)
    await run_job(seeded_job.id, deps)
    winner = await deps.store.winner_for(seeded_job.run_id, seeded_job.pro_id)
    if winner.kind == "winner":
        assert winner.evidence["pro_uuid"] == "pro_best"
        assert winner.evidence["contact_plan"]["channel"] == "call"


async def test_shadow_winner_records_plan_without_using_it(seeded_job, deps) -> None:
    deps.contact_plan_mode = "shadow"
    _with_candidates(deps, CANDIDATES)
    await run_job(seeded_job.id, deps)
    winner = await deps.store.winner_for(seeded_job.run_id, seeded_job.pro_id)
    if winner.kind == "winner":
        assert "contact_plan_shadow" in winner.evidence
        assert winner.evidence.get("pro_uuid") != "pro_best"
```

Check what `FakeLLM` records: `grep -n "self.prompts\|prompts.append\|call_count" tests/conftest.py`.
If it doesn't keep prompts, add `self.prompts: list[str] = []` and append in its call method as
part of this step. That's test scaffolding, not production code. Use whatever keeps the fake
LLM's canned batch valid when the pinned channel is `call`: if its fixture ideas are all `sms`,
they get coerced (Step 3), which is exactly what we want to exercise.

Add a unit test for the coercion:

```python
async def test_off_pin_idea_is_coerced_not_suppressed(seeded_job, deps) -> None:
    await _enforce_call_plan(seeded_job, deps)
    await run_job(seeded_job.id, deps)
    rows = (await deps.db.execute(select(CandidateRow))).scalars().all()
    assert rows and all(r.recommendation["channel"] == "call" for r in rows)
    assert not any((r.verdict or {}).get("block_kind") == "infeasible_channel" for r in rows)
```

(Use the real column names on `CandidateRow` for the verdict: `grep -n "class CandidateRow" -A 25 src/waypoint/tables.py`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/api && uv run pytest -q tests/test_pipeline.py -k "enforce or shadow_winner or coerced"`
Expected: FAIL (generation still gated to all run channels).

- [ ] **Step 3: Implement**

Add near `_enforced`:

```python
def _apply_plan(state: PipelineState, deps: PipelineDeps) -> None:
    """Under enforce, RECO's hint must not reach the model: the pin already
    encodes it, and a differing hint makes the critic bench on-pin ideas."""
    if _enforced(state, deps) is None or state.brief is None:
        return
    brief = state.brief
    curated = dict(brief.curated_context or {})
    if isinstance(curated.get("v"), dict):
        curated["v"] = {k: v for k, v in curated["v"].items() if k != "suggested_channel"}
    state.brief = brief.model_copy(update={
        "suggested_channel": None,
        "curated_context": curated if brief.curated_context is not None else None,
    })


def _generation_channels(state: PipelineState, deps: PipelineDeps) -> list[str]:
    plan = _enforced(state, deps)
    return [plan.channel] if plan and plan.channel else list(state.run.channels)


def _follow_up_channels(state: PipelineState, deps: PipelineDeps) -> list[str]:
    plan = _enforced(state, deps)
    return list(plan.open_channels) if plan else list(state.run.channels)
```

Wire these in:
- `_stage_plan`: call `_apply_plan(state, deps)` right after `state.plan = plan`.
- `run_job`: call `_apply_plan(state, deps)` right after the resume load of `state.plan`.
- `_stage_evolve` :1141: `gate = gate_pro(brief, _generation_channels(state, deps), state.run.journey_window)`.
  Also re-read `brief = state.brief` at the top, which the function already does.
- `_attach_follow_up` :1845: `gate = gate_pro(state.brief, _follow_up_channels(state, deps), state.run.journey_window)`.
- `_verdicts_for_batch`, inside the `for index, idea in enumerate(ideas):` loop, before the
  `recently_failed` check:

  ```python
          if len(channels) == 1 and idea.channel not in channels and _enforced(state, deps):
              log.info("coercing off-pin idea channel %r to %r", idea.channel, channels[0])
              idea.channel = channels[0]
  ```

- Winner evidence (:1790-1805): replace the `pro_uuid` line with:

  ```python
                      **_contact_evidence(state, deps),
  ```

  and add:

  ```python
  def _contact_evidence(state: PipelineState, deps: PipelineDeps) -> dict[str, Any]:
      legacy = state.brief.pro_uuid if state.brief and state.brief.pro_uuid else None
      plan = _enforced(state, deps)
      if plan is not None:
          return {"pro_uuid": plan.pro_uuid, "contact_plan": plan.to_json()}
      shadow = (
          {"contact_plan_shadow": state.plan.to_json()}
          if state.plan is not None and deps.contact_plan_mode == "shadow" else {}
      )
      return {**({"pro_uuid": legacy} if legacy else {}), **shadow}
  ```

Leave `suggested_channel` / `channel_override_reason` evidence alone in this task. Under enforce
they are empty, because the hint was stripped. They're removed in Task 9.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/api && uv run pytest -q && uv run mypy src && uv run ruff check src tests`
Expected: PASS, with the off-mode suite unchanged.

- [ ] **Step 5: Commit**

```bash
git add -A services/api
git commit -m "feat: enforce the contact plan — pinned channel, follow-up, winner Pro; no RECO hint to the model

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5b: Recently-failed mechanisms follow the org, not the admin

**Files:**
- Modify: `services/api/src/waypoint/evidence.py:138-160` (`failed_mechanisms`)
- Modify: `services/api/src/waypoint/pipeline.py` (the `failed_mechanisms(...)` call in `_stage_evolve`, about :1177)
- Test: `services/api/tests/test_evidence.py`

**Why:** staging runs key `pro_id` by the numeric org, but outcomes that come through an exposure
carry `pro_id = pro_uuid`, so this gate never matches today (review finding). With a contact plan
the contacted admin can change between runs, so match on the org as well (spec §2 default E).

**Interfaces:**
- Produces: `failed_mechanisms(session, pro_id: str, org_id: str | None = None) -> list[str]`.
  It matches rows where `pro_id == pro_id` **or** (`org_id` is given and `TouchOutcomeRow.org_id == org_id`).

- [ ] **Step 1: Write the failing test** (in `tests/test_evidence.py`, next to the existing
  `failed_mechanisms` tests; copy their row-building helper)

```python
async def test_failed_mechanisms_match_the_org_across_admins(db_session) -> None:
    # A failed touch recorded against admin A (pro_id=pro_uuid) with org_id "294916"
    add_outcome(db_session, pro_id="pro_admin_a", org_id="294916",
                mechanism="billing-transparency", returned_7d=False)
    await db_session.commit()
    assert await failed_mechanisms(db_session, "294916", org_id="294916") == ["billing-transparency"]
    assert await failed_mechanisms(db_session, "294916") == []  # old behaviour without org_id
```

(`add_outcome` stands for the file's existing helper; use its real name and required fields.)

- [ ] **Step 2: Run the test to verify it fails.** `cd services/api && .venv/bin/python -m pytest -q tests/test_evidence.py -k org_across`. Expected: `TypeError` (unexpected keyword `org_id`).

- [ ] **Step 3: Implement.** In `failed_mechanisms`, add the `org_id: str | None = None` parameter
  and replace `TouchOutcomeRow.pro_id == pro_id,` with:

```python
                (TouchOutcomeRow.pro_id == pro_id)
                | ((TouchOutcomeRow.org_id == org_id) if org_id else false()),
```

  (import `false` from `sqlalchemy`). In `_stage_evolve`:

```python
    org_key = (brief.org_id or brief.org_uuid) if brief else None
    failed = {mechanism_key(item) for item in await failed_mechanisms(session, state.pro_id, org_key)}
```

- [ ] **Step 4: Run the tests.** `.venv/bin/python -m pytest -q && .venv/bin/python -m mypy src`. Expected: PASS.

- [ ] **Step 5: Commit** with the message `fix: recently-failed mechanisms match the org, so they survive switching admins`
  and the Co-Authored-By trailer.

---

### Task 6: Handoff guard and call to-do contact

**Files:**
- Modify: `services/api/src/waypoint/handoff.py:248-251`
- Modify: `services/api/src/waypoint/models.py:256-275` (`CallItem`)
- Modify: `services/api/src/waypoint/call_todos.py:36-52`
- Regenerate: `contracts/openapi.json`, `apps/web/src/lib/api-types.ts`
- Test: `services/api/tests/test_handoff.py`, `services/api/tests/test_calls.py`, `tests/test_contract.py`

**Interfaces:**
- Consumes: `evidence["contact_plan"]` (Task 5).
- Produces: `CallItem.pro_uuid: str | None = None` and `CallItem.flags: list[str] = []`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_handoff.py`, copy the existing "measured winner produces an LCM row" test (find
it with `grep -n "def test_" tests/test_handoff.py | head -20`) into two new tests:

```python
async def test_handoff_holds_back_a_row_off_its_contact_plan(...):
    # same setup as the copied test, plus:
    winner.evidence = {**winner.evidence, "pro_uuid": "pro_x",
                       "contact_plan": {"channel": "email", "pro_uuid": "pro_x"}}
    # candidate.recommendation["channel"] is "sms"
    assert await ready_rows(session, run_id) == []


async def test_handoff_sends_the_plan_pro(...):
    winner.evidence = {**winner.evidence, "pro_uuid": "pro_x",
                       "contact_plan": {"channel": "sms", "pro_uuid": "pro_x"}}
    rows = await ready_rows(session, run_id)
    assert rows[0]["pro_uuid"] == "pro_x"
```

Fill in the `...` fixtures and setup verbatim from the copied test. In `tests/test_calls.py`, add
evidence `{"pro_uuid": "pro_x", "contact_plan": {"flags": ["dnc_call"]}}` to a call winner and
assert `item.pro_uuid == "pro_x"` and `item.flags == ["dnc_call"]`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd services/api && uv run pytest -q tests/test_handoff.py tests/test_calls.py`
Expected: FAIL.

- [ ] **Step 3: Implement**

`handoff.py`, right after the `sms/email` channel filter:

```python
        plan = winner.evidence.get("contact_plan")
        if (candidate is not None and isinstance(plan, dict) and plan.get("channel")
                and plan["channel"] != candidate.recommendation.get("channel")):
            log.warning("winner %s: channel disagrees with its contact plan; held back", winner.id)
            continue
```

`models.py` `CallItem`: add the fields after `org_id`:

```python
    pro_uuid: str | None = None  # the contact plan's Pro to phone
    flags: list[str] = Field(default_factory=list)  # e.g. dnc_call, phone_shared
```

`call_todos.py`, inside `CallItem(...)`:

```python
                pro_uuid=(str(winner.evidence["pro_uuid"])
                          if winner.evidence.get("pro_uuid") else None),
                flags=[str(f) for f in (winner.evidence.get("contact_plan") or {}).get("flags", [])],
```

Regenerate the contract, using the command printed in `tests/test_contract.py`:

```bash
cd services/api && uv run python -c 'import json; from waypoint.api import app; open("../../contracts/openapi.json", "w").write(json.dumps(app.openapi(), indent=2, sort_keys=True))'
cd ../../apps/web && npm run codegen
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/api && uv run pytest -q && uv run mypy src`
Expected: PASS, including `test_contract.py`.

- [ ] **Step 5: Commit**

```bash
git add -A services/api contracts apps/web/src/lib/api-types.ts
git commit -m "feat: hand off only on-plan rows; call to-dos name the Pro and DNC flags

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Operator UI

**Files:**
- Modify: `apps/web/src/components/RunStart.tsx:220`
- Modify: `apps/web/src/components/WinnerReview.tsx:210, 262-275`
- Modify: `apps/web/src/app/calls/page.tsx:62-63`
- Test: `apps/web/src/components/WinnerReview.test.tsx`, `apps/web/src/components/RunStart.test.tsx`

- [ ] **Step 1: Write the failing tests** (in `WinnerReview.test.tsx`, next to the existing
  "Selected … RECO …" test at about :158)

```tsx
it("shows the contact plan when the winner has one", () => {
  const winner = makeWinner({ evidence: { ...baseEvidence, contact_plan: {
    pro_uuid: "pro_x", channel: "call", source: "reco_fallback",
    runner_up: ["pro_y", "email"], flags: ["dnc_call"], blocked: [["pro_z", "sms", "sf_dnc"]],
  } } });
  render(<WinnerReview winner={winner} {...baseProps} />);
  expect(screen.getByText(/Contact plan: CALL to pro_x/)).toBeInTheDocument();
  expect(screen.getByText(/RECO fallback/)).toBeInTheDocument();
  expect(screen.getByText(/runner-up pro_y by email/)).toBeInTheDocument();
  expect(screen.getByText(/DNC on file — not a marketing call/)).toBeInTheDocument();
  expect(screen.getByText(/1 pair blocked by consent/)).toBeInTheDocument();
});
```

Use the file's own factory/helper names (`makeWinner`, `baseEvidence`, `baseProps` are
placeholders for whatever `WinnerReview.test.tsx` already defines; open the file and match
them). In `RunStart.test.tsx`, change the label assertion to `"Allowed channels"`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/web && npx vitest run src/components/WinnerReview.test.tsx src/components/RunStart.test.tsx`
(or `npm test -- …` if the package uses jest; check `apps/web/package.json` `scripts.test`)
Expected: FAIL.

- [ ] **Step 3: Implement**

`RunStart.tsx:220`: `<span>Allowed channels (Waypoint picks one Pro and channel per org)</span>`.

`WinnerReview.tsx`: above the existing "Selection summary" paragraph, render the plan when it
exists, and keep the old line only when there's no plan:

```tsx
const plan = winner.evidence?.contact_plan as
  | { pro_uuid?: string; channel?: string; source?: string; runner_up?: [string, string] | null;
      flags?: string[]; blocked?: unknown[] }
  | undefined;
const SOURCE_LABEL: Record<string, string> = {
  reco_default: "RECO default", reco_fallback: "RECO fallback",
  no_reco: "no RECO data", legacy: "legacy pick",
};
```

```tsx
{plan ? (
  <p>
    Contact plan: {String(plan.channel ?? "?").toUpperCase()} to <code>{plan.pro_uuid}</code> ·{" "}
    {SOURCE_LABEL[plan.source ?? ""] ?? plan.source}
    {plan.runner_up ? ` · runner-up ${plan.runner_up[0]} by ${plan.runner_up[1]}` : ""}
    {plan.flags?.includes("dnc_call") ? " · DNC on file — not a marketing call" : ""}
    {plan.blocked?.length ? ` · ${plan.blocked.length} pair${plan.blocked.length === 1 ? "" : "s"} blocked by consent` : ""}
  </p>
) : (
  /* existing "Selected … · RECO …" paragraph unchanged */
)}
```

On the abstained card (about :210): if `winner.evidence?.contact_plan?.abstain_reason` exists,
show `Contact plan abstained: {reason}`.

`calls/page.tsx:62-63`:

```tsx
Pro <code>{call.pro_uuid ?? call.pro_id}</code> · org <code>{call.org_id || "?"}</code> ·{" "}
{call.flags.includes("dnc_call") && <strong>DNC on file — not a marketing call · </strong>}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/web && npm test && npm run lint && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A apps/web
git commit -m "feat: show the contact plan to operators (winner review, calls, run form)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: n8n flow (repo copy) and docs

**Files:**
- Modify: `n8n/waypoint-variable-audit-context-async-v1.json` (the "Part 1 - Org snapshot and metadata" query)
- Modify: `services/api/tests/test_n8n_workbench_workflow.py:52-55`
- Modify: `docs/REPO-MAP.md` ("Runtime shape"), `CLAUDE.md` ("How a run gets context"),
  `services/api/src/waypoint/feasibility.py:1-15` (docstring), `TODOS.md` (the channel item)

- [ ] **Step 1: Write the failing test** (in `test_n8n_workbench_workflow.py`, next to :52)

```python
    assert "waypoint_contact_candidate" in org_snapshot_query
    assert "boolor_agg" in org_snapshot_query  # multi-account orgs fail closed
    assert "dnc_email__c ilike" not in org_snapshot_query  # addresses, not a flag
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd services/api && uv run pytest -q tests/test_n8n_workbench_workflow.py`
Expected: FAIL.

- [ ] **Step 3: Implement** by running this script from the repo root. It edits only the repo
  JSON copy:

```bash
python3 - <<'EOF'
import json, pathlib
flow_path = pathlib.Path("n8n/waypoint-variable-audit-context-async-v1.json")
flow = json.loads(flow_path.read_text())
sql = pathlib.Path("docs/superpowers/specs/2026-09-25-contact-candidate.sql").read_text()
body = sql[sql.index("with org as"):].rstrip().rstrip(";")
body = body.replace("\norder by adm.pro_uuid", "")
node = next(n for n in flow["nodes"] if n["name"] == "Part 1 - Org snapshot and metadata")
query = node["parameters"]["query"].rstrip().rstrip(";")
assert "waypoint_contact_candidate" not in query
node["parameters"]["query"] = (
    f"{query}\nunion all\n"
    "-- waypoint_contact_candidate: contact-plan inputs, one row per active admin\n"
    f"select query_name, variable_name, value, metadata from (\n{body}\n);\n"
)
flow_path.write_text(json.dumps(flow, indent=2) + "\n")
EOF
```

Check the JSON's original indentation first (`head -c 300 n8n/waypoint-variable-audit-context-async-v1.json`)
and match it in `json.dumps`, so the diff contains only the query change.

Docs:
- **REPO-MAP "Runtime shape":** add a "Contact plan" paragraph. The `plan` stage sits after
  `context`. Candidates come from the Part 1 `waypoint_contact_candidate` block, and Iterable is
  read directly. `CONTACT_PLAN_MODE` is off/shadow/enforce. Link the spec.
- **CLAUDE.md "How a run gets context":** the contact `pro_uuid` bypass now has a sibling,
  `contact_candidates`, which is also a bypass and never promoted.
- **feasibility.py docstring:** replace "the audience SQL upstream is the authoritative
  DNC/suppression filter" with "consent is decided by `contact_plan`; this gate only applies the
  journey window and the plan's channel".
- **TODOS.md:** replace the channel-selection item with a pointer to this plan.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd services/api && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A n8n services/api docs CLAUDE.md TODOS.md
git commit -m "feat: contact-candidate block in the workbench flow (repo copy) + docs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Cleanup (only after enforce has been verified on staging, Task 10)

**Files:**
- Modify: `services/api/src/waypoint/feasibility.py`: delete `NEGATIVE_CONSENT`,
  `CONSENT_FIELD` and `_consent_blocks`. `gate_pro` keeps only the window conflict and the
  channels filter.
- Modify: `services/api/src/waypoint/n8n.py`: drop `sms_consent_state` / `email_consent_state`
  from `OrgBrief` and `ALLOWED_FIELDS`.
- Modify: `services/api/src/waypoint/prompts.py`:
  - drop "consent states" from `channel_directive`;
  - drop the `unsupported_channel_override` rule at :461-463 and the `channel_override_reason`
    instructions at :339-340 and :440;
  - bump `PROMPT_VERSION` to `"waypoint_v8"` with a comment: `# v8: contact plan pins the channel`.
- Modify: `services/api/src/waypoint/models.py:92-100`: remove `channel_override_reason`, and
  `pipeline.py:622-625, 907-916, 1797-1802`.
- Modify: `apps/web/src/components/WinnerReview.tsx`: remove the old "Selected · RECO · override"
  fallback paragraph.
- Modify: `n8n/waypoint-variable-audit-context-async-v1.json`: remove the old
  `waypoint_contact_pro` row and the `addendum_channel_recommendation` union branch. Keep
  `staging_context._contact_pro` for old checkpoints.
- Tests to update (all of these are expected to change):
  - `test_feasibility.py:9,15,25,56,86`
  - `test_pipeline.py:1186,1201,1401`
  - `test_ranking.py:248,272`
  - `test_prompts.py:319`
  - `test_n8n.py:48`
  - `test_n8n_workbench_workflow.py:52-55`
  - `WinnerReview.test.tsx:158`

- [ ] **Step 1:** Delete the code listed above. Replace each broken test with one that
  asserts the new behaviour. For example, `test_feasibility`'s consent cases become "`gate_pro`
  never blocks on consent, since the contact plan owns consent".
- [ ] **Step 2:** Run `cd services/api && uv run pytest -q && uv run mypy src && uv run ruff check src tests` and `cd apps/web && npm test`. Everything must pass.
- [ ] **Step 3:** Commit: `refactor: remove dead consent fields and the RECO override machinery (contact plan owns both)`,
  with the Co-Authored-By trailer.

---

### Task 10: Staging rollout and verification (human-gated)

These steps change shared systems, so they need Jake's go-ahead each time.

`CONTACT_PLAN_MODE` takes `off | shadow | enforce` (case-insensitive; empty means `off`).

**Kill switch:** flipping `enforce` → `off` mid-job is acceptable. A job past the `plan` stage
then stops enforcing: it drops the handoff channel guard and reverts to the legacy `pro_uuid`.

- [ ] **Step 1: Check permissions (read-only).** Confirm the n8n Snowflake credential's role can
  read the candidate SQL's tables. Run the candidate SQL through the live flow's credential,
  e.g. in the n8n UI with a test execution on org 294916. Don't save anything. Also check,
  read-only:
  - `production.reco.channel_recommendations` is unique per `(pro_uuid, organization_id)`
    (duplicates fan out candidate rows);
  - `analytics.main.dim_service_pro.rank_by_phone` exists.

  One bad column fails the whole Part 1 node and holds the staging slot, so check these first.
- [ ] **Step 2: Export the live flow** (`GET /api/v1/workflows/uagiNmDAitRAHHRv`) and diff it
  against the repo copy from before Task 8. Stop if they differ beyond the known webhook path.
- [ ] **Step 3: Deploy the code with `CONTACT_PLAN_MODE=shadow`** on Railway staging (Jake), and
  set `ITERABLE_API_KEY` if it isn't already there.
- [ ] **Step 4: Apply the Part 1 query change to the live flow** (Jake, in the n8n UI). The change
  is additive, so old code ignores the new rows.
- [ ] **Step 5: Run shadow for about a day** on the verification orgs:
  - 294916 and 842846;
  - one Salesforce `sms_opt_out` org, one opportunity-DNC org, one single-admin org, one no-RECO
    org, one all-suppressed org.

  Check:
  - `job.checkpoint["plan"]` is present;
  - `contact_plan_shadow` is on the winners;
  - shadow logs show source `reco_default`/`reco_fallback`, not only `no_reco`;
  - no new failures or timeouts.
- [ ] **Step 6: Switch to `CONTACT_PLAN_MODE=enforce`** and re-run the same orgs. Check:
  - every candidate's channel equals the pin;
  - the handoff `pro_uuid` equals `contact_plan.pro_uuid`;
  - abstained jobs have `llm_calls` = 0;
  - LCM rejects no rows;
  - the call to-do shows the Pro and the DNC flag;
  - wall time per org doesn't regress noticeably;
  - zero critic verdicts of kind `unsupported_channel_override`.
- [ ] **Step 7:** Record the results in `docs/verification/` and then do Task 9.
