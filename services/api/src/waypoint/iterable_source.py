"""Direct Iterable outcome source (no n8n).

Polls Iterable's export API for SMS events since a durable cursor and feeds
the existing receiving side: each send becomes an exposure
(exposures.register — the authoritative send confirmation that starts the
measurement clock) and each bounce/click becomes a TouchOutcomeIn
(outcomes.ingest). Both receivers are idempotent per caller key, so a
replayed window is safe.

Attribution uses the natural key: the LCM batch id IS the run id (stamped in
the send event's dataFields) and the Iterable recipient IS the pro_uuid.
Sends resolve to their winner through `uq_winners_run_pro` exactly as
outcomes._resolve_run_pro does. A send whose run id is present but whose
winner is not yet visible is DEFERRED — the cursor holds and the window
replays next tick — rather than registered winner-less, because exposure
identity is immutable and an unlinked exposure could otherwise never reach
its winner. Deferral is bounded by DEFER_LIMIT, after which the send is
registered unlinked (and logged) so one bogus run id cannot stall the poller.

Routing is fail-closed (outcomes.REAL_SEND_ROUTING): an explicit dataFields
claim is taken as-is; a guardrail marker in the campaign/template name maps
to a non-evidence value; anything undeterminable stays "" and never counts
as evidence. The LCM's arm stamp (dataFields.lcmVariant, "A"/"B") rides onto
the exposure so warm-start promotion sees the causal arms.

Catch-up is bounded: at most CATCHUP per tick, first run starts
INITIAL_LOOKBACK ago — never an unbounded backfill. The window trails `now`
by LAG so events Iterable's export indexes late are still inside an unread
window. The cursor advances only after a successful ingest, so a failed
tick replays its window.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.cursors import load_cursor, parse_time, save_cursor
from waypoint.exposures import register
from waypoint.models import ExposureIn, TouchOutcomeIn
from waypoint.outcomes import ingest
from waypoint.settings import Settings
from waypoint.tables import ExposureRow, FleetControlRow, WinnerRow

log = logging.getLogger("waypoint.iterable_source")

SOURCE = "iterable"
BASE_URL = "https://api.iterable.com"
SEND_TYPE = "smsSend"
# dataTypeName -> (TouchOutcomeIn flag, value). Iterable's export has no
# smsDelivered/smsUnsubscribe types (a 400 in prod proved it): a bounce is a
# failed delivery, a click is engagement, and SMS opt-outs never export.
OUTCOME_TYPES: dict[str, tuple[str, bool]] = {
    "smsBounce": ("delivered", False),
    "smsClick": ("clicked", True),
}
CATCHUP = timedelta(hours=24)
INITIAL_LOOKBACK = timedelta(hours=24)
# The export index lags real time; the window trails `now` so an event
# indexed after the poll instant is still inside a window no tick has read.
LAG = timedelta(minutes=15)
# Call-volume floor: fetch only once this much NEW window has accumulated.
# 3h keeps total ingest lag inside the checkpoint sweep's 6h GRACE while
# capping this source at 8 windows x 3 data types = 24 calls/day, whatever
# POLL_SECONDS is.
MIN_WINDOW = timedelta(hours=3)
# How long a send with an unresolvable run id holds the cursor before being
# registered winner-less. A real winner exists before its send, so this only
# stalls on a bogus/foreign run id — and for at most one day.
DEFER_LIMIT = timedelta(hours=24)
# Guardrail marker in campaign/template names, used only when no explicit
# routing claim exists. Whole-word "test" (never a substring — "Latest",
# "Greatest" are real campaigns); "guardrail" in any form. Guardrailed sends
# carry a real Pro's context but go to an internal inbox — never evidence.
GUARDRAIL_PATTERN = re.compile(r"guardrail|\btest\b")
_RUN_ID_KEYS = ("run_id", "batch_id", "batchId")


def make_client(settings: Settings) -> httpx.AsyncClient:
    assert settings.ITERABLE_API_KEY is not None
    return httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=httpx.Timeout(60.0, connect=15.0),
        follow_redirects=False,
        headers={"Api-Key": settings.ITERABLE_API_KEY.get_secret_value()},
    )


def _routing(event: dict[str, Any]) -> str:
    data = event.get("dataFields") or {}
    claim = data.get("routing")
    if claim:
        return str(claim)
    names = " ".join(
        str(event.get(key) or "") for key in ("campaignName", "templateName")
    ).lower()
    if GUARDRAIL_PATTERN.search(names):
        return "guardrailed-test"
    return ""  # undeterminable: fails closed, never evidence


def _run_pro(event: dict[str, Any]) -> tuple[str, str]:
    data = event.get("dataFields") or {}
    run_id = next((str(data[k]) for k in _RUN_ID_KEYS if data.get(k)), "")
    return run_id, str(event.get("userId") or "")


async def _fetch_events(
    client: httpx.AsyncClient, data_type: str, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """One export call, newline-delimited JSON. Malformed lines are skipped
    with a log, never a crash. Timestamps carry an explicit +00:00 offset so
    the window cannot be re-read in the Iterable project's local timezone."""
    response = await client.get(
        "/api/export/data.json",
        params={
            "dataTypeName": data_type,
            "startDateTime": since.strftime("%Y-%m-%d %H:%M:%S") + " +00:00",
            "endDateTime": until.strftime("%Y-%m-%d %H:%M:%S") + " +00:00",
        },
    )
    if response.status_code == 400:
        # An export type this project doesn't recognize must cost ONE data
        # type's events, never the tick — a raise here would hold the cursor
        # and starve send ingestion behind a name mismatch.
        log.warning("iterable %s: export rejected the request (400); skipping", data_type)
        return []
    response.raise_for_status()
    events: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            log.warning("iterable %s: skipping malformed line", data_type)
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            log.warning("iterable %s: skipping non-object row", data_type)
    return events


async def _resolve_winners(
    session: AsyncSession, pairs: set[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    """(run_id, pro_id) -> winner id, exact via uq_winners_run_pro."""
    if not pairs:
        return {}
    rows = (
        await session.execute(
            select(WinnerRow.id, WinnerRow.run_id, WinnerRow.pro_id).where(
                WinnerRow.run_id.in_({run for run, _ in pairs}),
                WinnerRow.pro_id.in_({pro for _, pro in pairs}),
                WinnerRow.kind == "winner",
            )
        )
    ).all()
    return {(row.run_id, row.pro_id): row.id for row in rows}


def _send_to_exposure(event: dict[str, Any], winner_id: str | None) -> ExposureIn | None:
    message_id = event.get("messageId")
    sent_at = parse_time(event.get("createdAt"))
    _, pro_id = _run_pro(event)
    if not message_id or sent_at is None or not (winner_id or pro_id):
        log.warning("iterable send skipped: missing messageId/createdAt/recipient")
        return None
    variant = str((event.get("dataFields") or {}).get("lcmVariant") or "")
    return ExposureIn(
        exposure_id=str(message_id),  # Iterable's id: retries never fork
        recommendation_id=winner_id,
        pro_id=pro_id,
        # The LCM's arm stamp: "A" treated / "B" control. Anything else stays
        # None (legacy), so promotion's A+B causal gate sees the real arms.
        arm=variant if variant in ("A", "B") else None,  # type: ignore[arg-type]
        channel="sms",
        routing=_routing(event),
        send_status="confirmed",
        sent_at=sent_at,
    )


def _sends_to_exposures(
    sends: list[dict[str, Any]], winners: dict[tuple[str, str], str], now: datetime
) -> tuple[list[ExposureIn], int]:
    """Map send events to exposures; a recent send whose stamped run id
    resolved to no winner yet is deferred (counted, not registered) so the
    replayed window can link it once the winner is visible."""
    exposures: list[ExposureIn] = []
    deferred = ignored = 0
    for event in sends:
        pair = _run_pro(event)
        if not pair[0]:
            # The run_id gate: a send with no LCM batch stamp is not a
            # Waypoint send (the export covers ALL of the project's SMS) —
            # it can never be evidence, so it is not stored at all.
            ignored += 1
            continue
        winner_id = winners.get(pair)
        if winner_id is None and pair[1]:
            sent_at = parse_time(event.get("createdAt"))
            if sent_at is not None and sent_at > now - DEFER_LIMIT:
                deferred += 1
                continue
            log.warning(
                "iterable send older than %s still has no winner for run %s; "
                "registering winner-less", DEFER_LIMIT, pair[0],
            )
        if (exposure := _send_to_exposure(event, winner_id)) is not None:
            exposures.append(exposure)
    if ignored:
        log.debug("iterable: ignored %d send(s) with no LCM batch stamp", ignored)
    return exposures, deferred


async def poll(
    session: AsyncSession, client: httpx.AsyncClient, settings: Settings, now: datetime
) -> dict[str, int]:
    """One bounded tick: sends -> exposures, bounce/click -> outcomes,
    then advance the cursor. An HTTP failure raises — and a deferred send
    returns — before the cursor moves, so the next tick replays the same
    window (receivers are idempotent)."""
    cursor = await load_cursor(session, SOURCE)
    since = parse_time(cursor.get("since")) or (now - INITIAL_LOOKBACK)
    until = min(now - LAG, since + CATCHUP)
    if until - since < MIN_WINDOW:
        return {"exposures": 0, "outcomes": 0, "deferred": 0}  # too small; no calls spent

    sends = await _fetch_events(client, SEND_TYPE, since, until)
    pairs = {pair for pair in (_run_pro(e) for e in sends) if pair[0] and pair[1]}
    winners = await _resolve_winners(session, pairs)
    exposures, deferred = _sends_to_exposures(sends, winners, now)
    if exposures:
        await register(session, exposures)

    outcomes: list[TouchOutcomeIn] = []
    for data_type, (flag, value) in OUTCOME_TYPES.items():
        for event in await _fetch_events(client, data_type, since, until):
            if (outcome := _event_to_outcome(event, flag, value)) is not None:
                outcomes.append(outcome)
    outcomes = await _known_outcomes(session, outcomes, {e.exposure_id for e in exposures})
    if outcomes:
        await ingest(session, outcomes)

    if deferred:
        # Hold the cursor: the deferred sends' window replays next tick, and
        # register/ingest above are idempotent for everything already stored.
        log.warning("iterable: %d send(s) deferred awaiting winner; window will replay", deferred)
    else:
        await save_cursor(session, SOURCE, {"since": until.isoformat()})
    return {"exposures": len(exposures), "outcomes": len(outcomes), "deferred": deferred}


async def _known_outcomes(
    session: AsyncSession, outcomes: list[TouchOutcomeIn], registered: set[str]
) -> list[TouchOutcomeIn]:
    """The run_id gate for outcome events: keep only events for a Waypoint
    send — a messageId that names a known exposure (registered this tick or
    earlier), or an event carrying the LCM batch stamp itself. Everything
    else is the project's unrelated SMS traffic and is not stored."""
    message_ids = {o.exposure_id for o in outcomes if o.exposure_id} - registered
    known = set(registered)
    if message_ids:
        rows = (
            await session.execute(
                select(ExposureRow.id).where(ExposureRow.id.in_(message_ids))
            )
        ).scalars().all()
        known.update(rows)
    kept = [o for o in outcomes if (o.exposure_id in known) or o.run_id]
    if len(kept) < len(outcomes):
        log.debug("iterable: ignored %d outcome event(s) for non-Waypoint sends",
                  len(outcomes) - len(kept))
    return kept


def _event_to_outcome(event: dict[str, Any], flag: str, value: bool) -> TouchOutcomeIn | None:
    message_id = event.get("messageId")
    run_id, pro_id = _run_pro(event)
    fields: dict[str, Any] = {"source": SOURCE, flag: value}
    if message_id:
        fields["exposure_id"] = str(message_id)
    else:
        fields.update(run_id=run_id, pro_id=pro_id)
    try:
        return TouchOutcomeIn(**fields)
    except ValidationError:
        log.warning("iterable %s event skipped: no usable key", flag)
        return None


async def poll_if_enabled(
    session: AsyncSession, client: httpx.AsyncClient, settings: Settings, now: datetime
) -> dict[str, int] | None:
    """One tick, gated by the learning kill switch ONLY (same rule as the
    checkpoint sweep — the fleet kill switch never stops measurement)."""
    fleet = await session.get(FleetControlRow, 1)
    if fleet is not None and fleet.learning_killed:
        return None
    return await poll(session, client, settings, now)
