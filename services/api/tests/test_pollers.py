"""Direct Iterable/Amplitude outcome pollers (no n8n).

Pollers feed the existing tested receivers (exposures.register /
outcomes.ingest); these tests cover the polling contract itself: bounded
cursor pagination, idempotent re-polls, fail-closed routing, missing-key
disable, malformed-response tolerance, and one end-to-end path from a mocked
Iterable send plus a mocked Amplitude return to a validated winner.
"""

import gzip
import io
import json
import re
import zipfile
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
AMPLITUDE_EXPORT = re.compile(r"https://amplitude\.com/api/2/export.*")


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


def amplitude_zip(*events: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("export/hour.json.gz", gzip.compress(ndjson(*events).encode()))
    return buffer.getvalue()


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


async def test_amplitude_never_reads_past_the_iterable_cursor(
    db_session, httpx_mock: HTTPXMock
) -> None:
    # Returns must not be consumed for hours whose exposures may not be
    # registered yet — a lagging Iterable poller pauses return ingestion
    # instead of dropping returns into irreversible false negatives.
    await save_cursor(
        db_session, "amplitude",
        {"until": (NOW - timedelta(hours=9)).replace(minute=0).isoformat()},
    )
    await save_cursor(db_session, "iterable", {"since": (NOW - timedelta(hours=6)).isoformat()})
    httpx_mock.add_response(url=AMPLITUDE_EXPORT, status_code=404)
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    bound = (NOW - timedelta(hours=6)).replace(minute=0)
    assert await load_cursor(db_session, "amplitude") == {"until": bound.isoformat()}
    request = httpx_mock.get_requests()[0]
    assert request.url.params["end"] == (bound - timedelta(hours=1)).strftime("%Y%m%dT%H")


async def test_amplitude_repoll_keeps_one_outcome_per_exposure(
    db_session, httpx_mock: HTTPXMock
) -> None:
    await seed_winner(db_session)
    db_session.add(ExposureRow(
        id="exp-a", winner_id="win-run-lcm", run_id="run-lcm", pro_id="pro-uuid-1",
        routing="route-to-pro", send_status="confirmed", sent_at=SENT, channel="sms",
    ))
    await db_session.commit()
    httpx_mock.add_response(
        url=AMPLITUDE_EXPORT, content=amplitude_zip(return_event()), is_reusable=True
    )
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        first = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
        await save_cursor(db_session, "amplitude", {})  # replay the window
        second = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert first == {"returns": 1, "outcomes": 1}
    assert second == {"returns": 1, "outcomes": 1}
    outcomes = (await db_session.execute(select(TouchOutcomeRow))).scalars().all()
    assert len(outcomes) == 1
    assert outcomes[0].source == "amplitude"
    assert outcomes[0].first_return_at == SENT + timedelta(hours=1)
    assert outcomes[0].returned_7d is True  # derived, never caller-asserted


async def test_amplitude_return_before_the_send_is_not_a_qualifying_event(
    db_session, httpx_mock: HTTPXMock
) -> None:
    db_session.add(ExposureRow(
        id="exp-b", pro_id="pro-uuid-1", routing="route-to-pro",
        send_status="confirmed", sent_at=SENT, channel="sms",
    ))
    await db_session.commit()
    httpx_mock.add_response(
        url=AMPLITUDE_EXPORT,
        content=amplitude_zip(return_event(at=SENT - timedelta(hours=1))),
    )
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        result = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert result == {"returns": 1, "outcomes": 0}


async def test_amplitude_404_and_malformed_archive_are_quiet_skips(
    db_session, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=AMPLITUDE_EXPORT, status_code=404)
    httpx_mock.add_response(url=AMPLITUDE_EXPORT, content=b"this is not a zip")
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        first = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
        await save_cursor(db_session, "amplitude", {})
        second = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert first == {"returns": 0, "outcomes": 0}
    assert second == {"returns": 0, "outcomes": 0}


async def test_amplitude_catchup_is_bounded_per_tick(db_session, httpx_mock: HTTPXMock) -> None:
    # A week behind: one tick covers at most MAX_HOURS_PER_TICK (6) hours.
    since = (NOW - timedelta(days=7)).replace(minute=0)
    await save_cursor(db_session, "amplitude", {"until": since.isoformat()})
    httpx_mock.add_response(url=AMPLITUDE_EXPORT, status_code=404)
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    cursor = await load_cursor(db_session, "amplitude")
    assert cursor == {"until": (since + timedelta(hours=6)).isoformat()}
    request = httpx_mock.get_requests()[0]
    assert request.url.params["start"] == since.strftime("%Y%m%dT%H")
    assert request.url.params["end"] == (since + timedelta(hours=5)).strftime("%Y%m%dT%H")


async def test_pollers_spend_no_calls_until_a_min_window_accumulates(
    db_session, httpx_mock: HTTPXMock
) -> None:
    # Call-volume floor: under MIN_WINDOW of new data means ZERO HTTP calls,
    # however fast the worker ticks.
    await save_cursor(db_session, "iterable", {"since": (NOW - timedelta(hours=1)).isoformat()})
    small = (NOW - timedelta(hours=1)).replace(minute=0) - timedelta(hours=2)
    await save_cursor(db_session, "amplitude", {"until": small.isoformat()})
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        result = await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert result == {"exposures": 0, "outcomes": 0, "deferred": 0, "ignored": 0}
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        result = await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    assert result == {"returns": 0, "outcomes": 0}
    assert not httpx_mock.get_requests()
    # Neither cursor moved: the small window is still owed, not skipped.
    assert await load_cursor(db_session, "iterable") == {
        "since": (NOW - timedelta(hours=1)).isoformat()
    }
    assert await load_cursor(db_session, "amplitude") == {"until": small.isoformat()}


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
    httpx_mock.add_response(url=AMPLITUDE_EXPORT, content=amplitude_zip(return_event()))
    async with iterable_source.make_client(POLLER_SETTINGS) as client:
        await iterable_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    async with amplitude_source.make_client(POLLER_SETTINGS) as client:
        await amplitude_source.poll(db_session, client, POLLER_SETTINGS, NOW)
    winner = await db_session.get(WinnerRow, winner_id)
    await db_session.refresh(winner)
    assert winner.validation_status == "validated"
    assert winner.warm_start_eligible is True
    outcome = (await db_session.execute(select(TouchOutcomeRow))).scalars().one()
    assert outcome.evidence_limitation is None
    assert outcome.routing == "route-to-pro"
