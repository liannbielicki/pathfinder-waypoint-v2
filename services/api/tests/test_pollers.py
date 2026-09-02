"""Direct Iterable/Amplitude outcome pollers (no n8n).

Pollers feed the existing tested receivers (exposures.register /
outcomes.ingest); these tests cover the polling contract itself: bounded
cursor pagination, idempotent re-polls, fail-closed routing, missing-key
disable, malformed-response tolerance, and one end-to-end path from a mocked
Iterable send plus a mocked Amplitude return to a validated winner.
"""

import json
import re
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr
from pytest_httpx import HTTPXMock
from sqlalchemy import select

from tests.conftest import TEST_SETTINGS
from waypoint import amplitude_source, iterable_source
from waypoint.cursors import load_cursor, save_cursor
from waypoint.tables import ExposureRow, FleetControlRow, RunRow, TouchOutcomeRow, WinnerRow
from waypoint.worker import poller_specs

NOW = datetime(2026, 8, 31, 12, 30, tzinfo=UTC)
SENT = NOW - timedelta(hours=2)

POLLER_SETTINGS = TEST_SETTINGS.model_copy(
    update={
        "ITERABLE_API_KEY": SecretStr("it-key"),
        "AMPLITUDE_API_KEY": SecretStr("amp-key"),
        "AMPLITUDE_SECRET_KEY": SecretStr("amp-secret"),
    }
)

ITERABLE_EXPORT = re.compile(r"https://api\.iterable\.com/api/export/data\.json.*")


def send_event(
    message_id: str = "msg-1",
    pro_id: str = "pro-uuid-1",
    run_id: str = "run-lcm",
    routing: str = "route-to-pro",
    variant: str = "",
    **extra: object,
) -> dict:
    # Real export shape: Iterable echoes the LCM's send-time stamps back as
    # `transactionalData`, a JSON STRING (docs/n8n/outcome-ingestion.md).
    stamps: dict = {}
    if run_id:
        stamps["lcmRun"] = run_id
    if routing:
        stamps["lcmRouting"] = routing
    if variant:
        stamps["lcmVariant"] = variant
    return {
        "messageId": message_id,
        "userId": pro_id,
        "createdAt": SENT.isoformat(),
        "transactionalData": json.dumps(stamps),
        **extra,
    }


def ndjson(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


def return_event(pro_id: str = "pro-uuid-1", at: datetime = SENT + timedelta(hours=1)) -> dict:
    return {
        "event_type": "session_start",
        "user_id": pro_id,
        "event_time": at.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" "),
    }


async def seed_winner(db_session, run_id: str = "run-lcm", pro_id: str = "pro-uuid-1") -> str:
    db_session.add(RunRow(
        id=run_id, pro_ids=[pro_id], audience_query="q", audience_run="r", channels=["sms"],
    ))
    await db_session.flush()
    db_session.add(WinnerRow(
        id=f"win-{run_id}", run_id=run_id, pro_id=pro_id, kind="winner",
        item_id="item-1", item_version="v1", evidence={"org_id": "org-1"},
    ))
    await db_session.commit()
    return f"win-{run_id}"


# --- iterable ----------------------------------------------------------------


async def test_iterable_pagination_is_bounded_and_advances_the_cursor(
    db_session, httpx_mock: HTTPXMock
) -> None:
    # Three days behind: each tick covers at most CATCHUP, never the backlog.
    await save_cursor(db_session, "iterable", {"since": (NOW - timedelta(days=3)).isoformat()})
    httpx_mock.add_response(url=ITERABLE_EXPORT, text="", is_reusable=True)
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
        cursor = await load_cursor(db_session, "iterable")
        assert cursor == {"since": (NOW - timedelta(days=2)).isoformat()}
        await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
        cursor = await load_cursor(db_session, "iterable")
        assert cursor == {"since": (NOW - timedelta(days=1)).isoformat()}
    starts = {
        request.url.params["startDateTime"] for request in httpx_mock.get_requests()
    }
    # Offset-bearing on the wire, so Iterable cannot re-read the window in
    # the project's local timezone.
    assert starts == {
        (NOW - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S") + " +00:00",
        (NOW - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S") + " +00:00",
    }


async def test_iterable_send_registers_a_confirmed_winner_linked_exposure(
    db_session, httpx_mock: HTTPXMock
) -> None:
    winner_id = await seed_winner(db_session)
    httpx_mock.add_response(url=ITERABLE_EXPORT, text=ndjson(send_event(variant="A")))
    httpx_mock.add_response(url=ITERABLE_EXPORT, text="", is_reusable=True)
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        result = await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert result["exposures"] == 1
    row = await db_session.get(ExposureRow, "msg-1")
    assert row is not None
    assert row.winner_id == winner_id
    assert row.send_status == "confirmed"
    assert row.sent_at == SENT
    assert row.routing == "route-to-pro"
    assert row.arm == "A"  # the LCM's lcmVariant stamp feeds the causal gate


async def test_iterable_guardrail_and_unmarked_sends_fail_closed(
    db_session, httpx_mock: HTTPXMock
) -> None:
    await seed_winner(db_session)
    guardrailed = send_event(
        message_id="msg-guard", routing="", campaignName="Guardrail QA blast"
    )
    unmarked = send_event(message_id="msg-unknown", routing="")
    # "test" must match as a whole word only — "Latest" is a real campaign.
    latest = send_event(
        message_id="msg-latest", routing="", campaignName="Latest Tips for Pros"
    )
    httpx_mock.add_response(url=ITERABLE_EXPORT, text=ndjson(guardrailed, unmarked, latest))
    httpx_mock.add_response(url=ITERABLE_EXPORT, text="", is_reusable=True)
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    guard_row = await db_session.get(ExposureRow, "msg-guard")
    unknown_row = await db_session.get(ExposureRow, "msg-unknown")
    latest_row = await db_session.get(ExposureRow, "msg-latest")
    assert guard_row.routing == "guardrailed-test"
    assert unknown_row.routing == ""  # undeterminable stays non-evidence
    assert latest_row.routing == ""  # substring "test" must not flag it


async def test_iterable_routing_from_the_recipient_email_domain(
    db_session, httpx_mock: HTTPXMock
) -> None:
    # The LCM stamps no lcmRouting; the recipient's email domain IS the
    # routing (an SMS has no destination-number parameter — the addressed
    # profile decides both phone and email). Internal domains are guardrailed
    # testers; any other domain is a real Pro; no email fails closed.
    await seed_winner(db_session)
    real = send_event(message_id="msg-real", routing="", email="pro@plumberco.com")
    internal = send_event(message_id="msg-int", routing="", email="QA@GetHousecallPro.com")
    stamped = send_event(message_id="msg-stamped", email="qa@housecallpro.com")
    httpx_mock.add_response(url=ITERABLE_EXPORT, text=ndjson(real, internal, stamped))
    httpx_mock.add_response(url=ITERABLE_EXPORT, text="", is_reusable=True)
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert (await db_session.get(ExposureRow, "msg-real")).routing == "route-to-pro"
    assert (await db_session.get(ExposureRow, "msg-int")).routing == "guardrailed-test"
    # An explicit lcmRouting stamp outranks the domain.
    assert (await db_session.get(ExposureRow, "msg-stamped")).routing == "route-to-pro"


async def test_iterable_repoll_of_the_same_window_does_not_duplicate(
    db_session, httpx_mock: HTTPXMock
) -> None:
    await seed_winner(db_session)
    clicked = {"messageId": "msg-1", "userId": "pro-uuid-1"}
    for _ in range(2):  # same window twice, as after a failed cursor advance
        httpx_mock.add_response(url=ITERABLE_EXPORT, text=ndjson(send_event()))
        httpx_mock.add_response(url=ITERABLE_EXPORT, text="")  # smsBounce
        httpx_mock.add_response(url=ITERABLE_EXPORT, text=ndjson(clicked))  # smsClick
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
        await save_cursor(db_session, "iterable", {})  # replay the window
        await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    exposures = (await db_session.execute(select(ExposureRow))).scalars().all()
    outcomes = (await db_session.execute(select(TouchOutcomeRow))).scalars().all()
    assert len(exposures) == 1
    assert len(outcomes) == 1
    assert outcomes[0].clicked is True
    assert outcomes[0].source == "iterable"


async def test_iterable_malformed_rows_are_skipped_not_crashed(
    db_session, httpx_mock: HTTPXMock
) -> None:
    await seed_winner(db_session)
    body = 'not json\n[1, 2]\n{"messageId": ""}\n' + ndjson(send_event(routing=""))
    httpx_mock.add_response(url=ITERABLE_EXPORT, text=body)
    httpx_mock.add_response(url=ITERABLE_EXPORT, text="", is_reusable=True)
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        result = await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert result["exposures"] == 1  # only the well-formed send survived


async def test_iterable_rejected_data_type_costs_one_type_not_the_tick(
    db_session, httpx_mock: HTTPXMock
) -> None:
    # A 400 on one export type (e.g. a name this project doesn't support)
    # must not hold the cursor and starve send ingestion — prod hit exactly
    # this with a nonexistent dataTypeName.
    await seed_winner(db_session)
    httpx_mock.add_response(
        url=re.compile(r".*dataTypeName=smsSend.*"), text=ndjson(send_event())
    )
    httpx_mock.add_response(url=re.compile(r".*dataTypeName=smsBounce.*"), status_code=400)
    httpx_mock.add_response(url=re.compile(r".*dataTypeName=smsClick.*"), text="")
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        result = await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert result["exposures"] == 1
    assert (await load_cursor(db_session, "iterable")) != {}  # cursor advanced


async def test_iterable_400_on_the_send_fetch_holds_the_cursor(
    db_session, httpx_mock: HTTPXMock
) -> None:
    # The send stream is the tick's authoritative input: a rejected send
    # fetch must raise and replay the window, never advance past real sends.
    import pytest
    from httpx import HTTPStatusError

    httpx_mock.add_response(url=re.compile(r".*dataTypeName=smsSend.*"), status_code=400)
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        with pytest.raises(HTTPStatusError):
            await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert await load_cursor(db_session, "iterable") == {}  # window held


async def test_iterable_reads_stamps_from_datafields_as_fallback(
    db_session, httpx_mock: HTTPXMock
) -> None:
    winner_id = await seed_winner(db_session)
    event = {
        "messageId": "msg-df", "userId": "pro-uuid-1", "createdAt": SENT.isoformat(),
        "dataFields": {"run_id": "run-lcm", "routing": "route-to-pro", "lcmVariant": "B"},
    }
    httpx_mock.add_response(url=ITERABLE_EXPORT, text=ndjson(event))
    httpx_mock.add_response(url=ITERABLE_EXPORT, text="", is_reusable=True)
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    row = await db_session.get(ExposureRow, "msg-df")
    assert row.winner_id == winner_id
    assert row.routing == "route-to-pro"
    assert row.arm == "B"


async def test_iterable_bounce_disqualifies_the_outcome_as_evidence(
    db_session, httpx_mock: HTTPXMock
) -> None:
    # A bounced message never reached the Pro: its silence must be labelled,
    # never measured as a clean negative against the winner.
    await seed_winner(db_session)
    bounced = {"messageId": "msg-1", "userId": "pro-uuid-1"}
    httpx_mock.add_response(url=re.compile(r".*dataTypeName=smsSend.*"), text=ndjson(send_event()))
    httpx_mock.add_response(url=re.compile(r".*dataTypeName=smsBounce.*"), text=ndjson(bounced))
    httpx_mock.add_response(url=re.compile(r".*dataTypeName=smsClick.*"), text="")
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    outcome = (await db_session.execute(select(TouchOutcomeRow))).scalars().one()
    assert outcome.delivered is False
    assert outcome.evidence_limitation == "send bounced: never delivered to the Pro"


async def test_iterable_poll_is_gated_by_the_learning_kill_switch(
    db_session, httpx_mock: HTTPXMock
) -> None:
    db_session.add(FleetControlRow(id=1, learning_killed=True))
    await db_session.commit()
    httpx_mock.add_response(url=ITERABLE_EXPORT, text="", is_optional=True)
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        result = await iterable_source.poll_if_enabled(db_session, client, POLLER_SETTINGS, NOW)
    assert result is None
    assert not httpx_mock.get_requests()


async def test_iterable_ignores_non_waypoint_sms_traffic(
    db_session, httpx_mock: HTTPXMock
) -> None:
    # The run_id gate: the export covers ALL of the project's SMS — sends
    # with no LCM batch stamp (and their delivery events) are not stored.
    unstamped_send = send_event(message_id="msg-mkt", run_id="", routing="")
    unstamped_click = {"messageId": "msg-mkt", "userId": "pro-uuid-1"}
    httpx_mock.add_response(
        url=re.compile(r".*dataTypeName=smsSend.*"), text=ndjson(unstamped_send)
    )
    httpx_mock.add_response(url=re.compile(r".*dataTypeName=smsBounce.*"), text="")
    httpx_mock.add_response(
        url=re.compile(r".*dataTypeName=smsClick.*"), text=ndjson(unstamped_click)
    )
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        result = await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert result == {"exposures": 0, "outcomes": 0, "deferred": 0, "ignored": 1}
    assert (await db_session.execute(select(ExposureRow))).scalars().all() == []
    assert (await db_session.execute(select(TouchOutcomeRow))).scalars().all() == []
    assert (await load_cursor(db_session, "iterable")) != {}  # gate never holds the cursor


async def test_iterable_defers_sends_until_their_winner_is_visible(
    db_session, httpx_mock: HTTPXMock
) -> None:
    # A recent send with a stamped run id but no visible winner must NOT
    # register winner-less (the link could never be repaired by identity
    # rules alone) — it holds the cursor and the replayed window links it.
    httpx_mock.add_response(
        url=re.compile(r".*dataTypeName=smsSend.*"), text=ndjson(send_event()),
        is_reusable=True,
    )
    httpx_mock.add_response(
        url=re.compile(r".*dataTypeName=sms(Bounce|Click).*"), text="",
        is_reusable=True,
    )
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        first = await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
        assert first == {"exposures": 0, "outcomes": 0, "deferred": 1, "ignored": 0}
        assert await db_session.get(ExposureRow, "msg-1") is None
        assert await load_cursor(db_session, "iterable") == {}  # window held
        winner_id = await seed_winner(db_session)
        second = await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert second["exposures"] == 1
    assert second["deferred"] == 0
    row = await db_session.get(ExposureRow, "msg-1")
    assert row.winner_id == winner_id
    assert (await load_cursor(db_session, "iterable")) != {}  # cursor advanced


# --- amplitude ---------------------------------------------------------------

USERSEARCH = re.compile(r"https://amplitude\.com/api/2/usersearch.*")
USERACTIVITY = re.compile(r"https://amplitude\.com/api/2/useractivity.*")
SENT_OLD = NOW - timedelta(days=2)


def usersearch_body(pro_id: str = "pro-uuid-1", amplitude_id: int = 12345) -> dict:
    return {
        "matches": [{"user_id": pro_id, "amplitude_id": amplitude_id}],
        "type": "match_user_or_device_id",
    }


def amp_event(event_type: str = "session_start", at: datetime = SENT_OLD + timedelta(hours=1)) -> dict:
    return {
        "event_type": event_type,
        "event_time": at.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" "),
    }


def activity_body(*events: dict) -> dict:
    return {"userData": {"num_events": len(events)}, "events": list(events)}


async def seed_exposure(
    db_session, exposure_id: str = "exp-a", pro_id: str = "pro-uuid-1",
    sent_at: datetime = SENT_OLD, **extra,
) -> None:
    db_session.add(ExposureRow(
        id=exposure_id, pro_id=pro_id, routing="route-to-pro",
        send_status="confirmed", sent_at=sent_at, channel="sms", **extra,
    ))
    await db_session.commit()


async def test_amplitude_lookup_ingests_first_return_and_stamps_coverage(
    db_session, httpx_mock: HTTPXMock
) -> None:
    await seed_exposure(db_session)
    httpx_mock.add_response(url=USERSEARCH, json=usersearch_body())
    httpx_mock.add_response(url=USERACTIVITY, json=activity_body(
        amp_event(at=SENT_OLD + timedelta(hours=5)),
        amp_event("page_view"),  # not the return event: never a qualifying return
        amp_event(at=SENT_OLD + timedelta(hours=1)),
        amp_event(at=SENT_OLD - timedelta(hours=1)),  # pre-send: not qualifying
    ))
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        first = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
        # Outcome recorded: the exposure is settled, the repoll spends nothing.
        second = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert first == {"checked": 1, "returns": 1, "unresolved": 0, "calls": 2}
    assert second == {"checked": 0, "returns": 0, "unresolved": 0, "calls": 0}
    assert len(httpx_mock.get_requests()) == 2
    outcome = (await db_session.execute(select(TouchOutcomeRow))).scalars().one()
    assert outcome.source == "amplitude"
    assert outcome.first_return_at == SENT_OLD + timedelta(hours=1)  # the FIRST in window
    assert outcome.returned_1d is True  # derived, never caller-asserted
    exposure = await db_session.get(ExposureRow, "exp-a")
    # Stamped now MINUS the indexing-lag grace: a just-happened return may not
    # be indexed yet, and over-claiming coverage would mint false negatives.
    assert exposure.returns_checked_at == NOW - amplitude_source.GRACE


async def test_amplitude_no_return_stamps_coverage_and_backs_off(
    db_session, httpx_mock: HTTPXMock
) -> None:
    await seed_exposure(db_session)
    httpx_mock.add_response(url=USERSEARCH, json=usersearch_body())
    httpx_mock.add_response(url=USERACTIVITY, json=activity_body(
        amp_event(at=SENT_OLD - timedelta(hours=1)),  # organic pre-send activity
    ), is_reusable=True)
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        first = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
        # Every closed horizon is covered by the stamp; the next check is owed
        # only when the 7d horizon closes, so an immediate re-tick is free.
        second = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
        third = await amplitude_source.poll(
            db_session, client, POLLER_SETTINGS, SENT_OLD + timedelta(days=8)
        )
    assert first == {"checked": 1, "returns": 0, "unresolved": 0, "calls": 2}
    assert second == {"checked": 0, "returns": 0, "unresolved": 0, "calls": 0}
    # The 7d close re-checks the same pro; the cached amplitude_id is reused,
    # so only the activity call is spent.
    assert third == {"checked": 1, "returns": 0, "unresolved": 0, "calls": 1}
    assert not (await db_session.execute(select(TouchOutcomeRow))).scalars().all()


async def test_amplitude_ambiguous_identity_is_never_stamped(
    db_session, httpx_mock: HTTPXMock
) -> None:
    # usersearch is a partial match: anything but exactly one EXACT user_id
    # match cannot prove identity, so the pro's returns stay unmeasured
    # (never a false negative) and the failure is cached, not retried hot.
    await seed_exposure(db_session)
    body = {"matches": [
        {"user_id": "pro-uuid-1", "amplitude_id": 1},
        {"user_id": "pro-uuid-1", "amplitude_id": 2},
    ], "type": "match_user_or_device_id"}
    httpx_mock.add_response(url=USERSEARCH, json=body)
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        first = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
        second = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert first == {"checked": 0, "returns": 0, "unresolved": 1, "calls": 1}
    assert second == {"checked": 0, "returns": 0, "unresolved": 1, "calls": 0}
    exposure = await db_session.get(ExposureRow, "exp-a")
    assert exposure.returns_checked_at is None


async def test_amplitude_one_lookup_covers_all_of_a_pros_exposures(
    db_session, httpx_mock: HTTPXMock
) -> None:
    await seed_exposure(db_session, "exp-old", sent_at=NOW - timedelta(days=10))
    await seed_exposure(db_session, "exp-new", sent_at=SENT_OLD)
    httpx_mock.add_response(url=USERSEARCH, json=usersearch_body())
    httpx_mock.add_response(url=USERACTIVITY, json=activity_body(
        amp_event(at=SENT_OLD + timedelta(hours=1)),
    ))
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        result = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    # One usersearch + one activity call settles BOTH exposures: the return
    # falls in exp-new's window and in exp-old's 30d window too.
    assert result == {"checked": 2, "returns": 2, "unresolved": 0, "calls": 2}
    outcomes = (await db_session.execute(select(TouchOutcomeRow))).scalars().all()
    assert {o.exposure_id for o in outcomes} == {"exp-old", "exp-new"}


async def test_amplitude_call_budget_bounds_the_tick(
    db_session, httpx_mock: HTTPXMock, monkeypatch
) -> None:
    monkeypatch.setattr(amplitude_source, "CALL_BUDGET", 3)
    # pro-1 strictly older so the oldest-first ordering is deterministic.
    await seed_exposure(db_session, "exp-1", pro_id="pro-1", sent_at=SENT_OLD - timedelta(days=1))
    await seed_exposure(db_session, "exp-2", pro_id="pro-2")
    httpx_mock.add_response(
        url="https://amplitude.com/api/2/usersearch?user=pro-1",
        json=usersearch_body("pro-1", 111),
    )
    httpx_mock.add_response(
        url="https://amplitude.com/api/2/usersearch?user=pro-2",
        json=usersearch_body("pro-2", 222),
    )
    httpx_mock.add_response(url=USERACTIVITY, json=activity_body(), is_reusable=True)
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        result = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    # pro-1 costs 2 calls; pro-2's usersearch exhausts the budget before its
    # activity lookup, so it stays due (and its usersearch is cached).
    assert result == {"checked": 1, "returns": 0, "unresolved": 0, "calls": 3}
    exposure = await db_session.get(ExposureRow, "exp-2")
    assert exposure.returns_checked_at is None


async def test_amplitude_out_paged_history_never_stamps_coverage(
    db_session, httpx_mock: HTTPXMock, monkeypatch
) -> None:
    # A hyper-active pro whose history out-pages PAGE_CAP before reaching the
    # oldest pending send: returns found are ingested (positive evidence),
    # but coverage is NOT stamped — an earlier return may hide past the cap,
    # so a silent horizon must not become a measured negative.
    monkeypatch.setattr(amplitude_source, "PAGE_SIZE", 2)
    monkeypatch.setattr(amplitude_source, "PAGE_CAP", 1)
    monkeypatch.setattr(amplitude_source, "_OUTPAGED_UNTIL", {})
    await seed_exposure(db_session)
    httpx_mock.add_response(url=USERSEARCH, json=usersearch_body())
    httpx_mock.add_response(url=USERACTIVITY, json=activity_body(
        amp_event(at=SENT_OLD + timedelta(days=1)),
        amp_event(at=SENT_OLD + timedelta(hours=6)),  # full page, newer than sent
    ))
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        result = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert result == {"checked": 0, "returns": 1, "unresolved": 0, "calls": 2}
    outcome = (await db_session.execute(select(TouchOutcomeRow))).scalars().one()
    assert outcome.first_return_at == SENT_OLD + timedelta(hours=6)
    exposure = await db_session.get(ExposureRow, "exp-a")
    assert exposure.returns_checked_at is None
    # The pro backs off instead of re-spending PAGE_CAP calls every tick.
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        again = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert again == {"checked": 0, "returns": 0, "unresolved": 0, "calls": 0}


async def test_amplitude_covered_silent_exposures_never_starve_the_due_query(
    db_session, httpx_mock: HTTPXMock, monkeypatch
) -> None:
    # A fully-covered silent exposure matches confirmed/no-outcome FOREVER and
    # the set of them grows without bound — they must be excluded in SQL or
    # they eventually fill the FETCH_LIMIT head and stall all measurement.
    monkeypatch.setattr(amplitude_source, "FETCH_LIMIT", 1)
    done_sent = NOW - timedelta(days=40)
    await seed_exposure(db_session, "exp-done", sent_at=done_sent)
    exposure = await db_session.get(ExposureRow, "exp-done")
    exposure.returns_checked_at = done_sent + timedelta(days=31)
    await db_session.commit()
    await seed_exposure(db_session, "exp-due", sent_at=SENT_OLD)
    httpx_mock.add_response(url=USERSEARCH, json=usersearch_body())
    httpx_mock.add_response(url=USERACTIVITY, json=activity_body())
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        result = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert result["checked"] == 1
    assert (await db_session.get(ExposureRow, "exp-due")).returns_checked_at is not None


async def test_pollers_spend_no_calls_when_nothing_is_due(
    db_session, httpx_mock: HTTPXMock
) -> None:
    # Iterable: under MIN_WINDOW of new data means ZERO HTTP calls, however
    # fast the worker ticks. Amplitude: no exposure with a closed, unfetched
    # horizon means the same.
    await save_cursor(db_session, "iterable", {"since": (NOW - timedelta(hours=1)).isoformat()})
    await seed_exposure(db_session, sent_at=NOW - timedelta(hours=2))  # 1d not closed
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        result = await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert result == {"exposures": 0, "outcomes": 0, "deferred": 0, "ignored": 0}
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        result = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert result == {"checked": 0, "returns": 0, "unresolved": 0, "calls": 0}
    assert not httpx_mock.get_requests()
    assert await load_cursor(db_session, "iterable") == {
        "since": (NOW - timedelta(hours=1)).isoformat()
    }


async def test_sweep_gates_on_the_exposure_coverage_stamp(db_session) -> None:
    # A backfilled send must not be stamped a negative before the amplitude
    # poller has provably fetched that pro's returns past the horizon.
    from waypoint.checkpoints import sweep_if_enabled
    from waypoint.exposures import register
    from waypoint.models import ExposureIn

    sent = NOW - timedelta(days=8)
    await register(db_session, [ExposureIn(
        exposure_id="exp-old", pro_id="pro-uuid-1", channel="sms",
        routing="route-to-pro", send_status="confirmed", sent_at=sent,
    )])
    # The poller's heartbeat row activates per-exposure gating.
    await save_cursor(db_session, "amplitude", {"mode": "user_activity"})
    result = await sweep_if_enabled(db_session, NOW)
    # The silent exposure synthesizes its outcome row, but with no coverage
    # stamp no horizon may be graded.
    assert result == {"resolved": 0, "synthesized": 1}
    outcome = (await db_session.execute(select(TouchOutcomeRow))).scalars().one()
    assert outcome.returned_1d is None and outcome.returned_7d is None

    # Coverage through sent+1d proves exactly the 1d horizon.
    exposure = await db_session.get(ExposureRow, "exp-old")
    exposure.returns_checked_at = sent + timedelta(days=1)
    await db_session.commit()
    result = await sweep_if_enabled(db_session, NOW)
    assert result is not None and result["resolved"] == 1
    await db_session.refresh(outcome)
    assert outcome.returned_1d is False
    assert outcome.returned_7d is None  # 7d returns not fetched yet

    # Coverage through now proves the rest.
    exposure.returns_checked_at = NOW
    await db_session.commit()
    await sweep_if_enabled(db_session, NOW)
    await db_session.refresh(outcome)
    assert outcome.returned_7d is False


# --- worker wiring -----------------------------------------------------------


def test_missing_keys_disable_pollers() -> None:
    assert poller_specs(TEST_SETTINGS) == []  # no keys: worker runs with zero pollers
    names = [name for name, _, _ in poller_specs(POLLER_SETTINGS)]
    assert names == ["iterable", "amplitude"]
    # Amplitude needs BOTH halves of the credential.
    half = POLLER_SETTINGS.model_copy(update={"AMPLITUDE_SECRET_KEY": None})
    assert [name for name, _, _ in poller_specs(half)] == ["iterable"]


# --- end to end --------------------------------------------------------------


async def test_end_to_end_send_plus_return_validates_the_winner(
    db_session, httpx_mock: HTTPXMock
) -> None:
    winner_id = await seed_winner(db_session)
    httpx_mock.add_response(url=ITERABLE_EXPORT, text=ndjson(send_event()))
    httpx_mock.add_response(url=ITERABLE_EXPORT, text="", is_reusable=True)
    httpx_mock.add_response(url=USERSEARCH, json=usersearch_body())
    httpx_mock.add_response(url=USERACTIVITY, json=activity_body(
        amp_event(at=SENT + timedelta(hours=1)),
    ))
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        # The exposure's first horizon closes a day after the send.
        await amplitude_source.poll(
            db_session, client, POLLER_SETTINGS, SENT + timedelta(days=2)
        )
    winner = await db_session.get(WinnerRow, winner_id)
    await db_session.refresh(winner)
    assert winner.validation_status == "validated"
    assert winner.warm_start_eligible is True
    outcome = (await db_session.execute(select(TouchOutcomeRow))).scalars().one()
    assert outcome.evidence_limitation is None
    assert outcome.routing == "route-to-pro"
