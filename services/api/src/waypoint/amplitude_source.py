"""Direct Amplitude return-event source (no n8n).

Polls the Amplitude Export API (hour-granular zip of gzipped NDJSON) for the
return event named by settings.AMPLITUDE_RETURN_EVENT, keyed by user_id —
which IS the pro_uuid. For each confirmed exposure of that Pro whose 30-day
observation window contains the event, it emits a TouchOutcomeIn with
first_return_at and source "amplitude"; outcomes.ingest derives the return
horizons from the confirmed send (this module never asserts returned_*), and
keeps the minimum first_return_at per (recommendation_id, source), so only
the FIRST qualifying event per pro per exposure window matters and re-sends
are safe.

Catch-up is bounded: at most MAX_HOURS_PER_TICK hourly files per tick,
stopping LAG behind now (export data for the current hour is incomplete),
first run starts INITIAL_LOOKBACK ago. The cursor advances only after a
successful ingest, so a failed tick replays its window.
"""

import gzip
import json
import logging
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from typing import IO, Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.cursors import load_cursor, parse_time, save_cursor
from waypoint.models import TouchOutcomeIn
from waypoint.outcomes import ingest
from waypoint.settings import Settings
from waypoint.tables import ExposureRow, FleetControlRow

log = logging.getLogger("waypoint.amplitude_source")

SOURCE = "amplitude"
BASE_URL = "https://amplitude.com"


class SizeCapError(Exception):
    """Amplitude refused the export: the response would exceed its size cap."""
HOUR = timedelta(hours=1)
LAG = HOUR  # the export for an hour is complete only once the hour is over
# Amplitude 400s an export whose response would be too large; staging hit
# that on a 24h window of the full product's events. Small windows keep every
# response under the cap — catch-up just takes more ticks.
MAX_HOURS_PER_TICK = 6
INITIAL_LOOKBACK = timedelta(hours=24)
# Call-volume floor: fetch only once this many complete NEW hours exist.
# One multi-hour export call covers the whole window, so this caps the
# source at 8 calls/day while total ingest lag (window + 1h export LAG)
# stays inside the checkpoint sweep's 6h GRACE.
MIN_WINDOW = timedelta(hours=3)
# Longest return horizon: an event after sent_at + 30d measures nothing.
WINDOW = timedelta(days=30)


def make_client(settings: Settings) -> httpx.AsyncClient:
    assert settings.AMPLITUDE_API_KEY is not None
    assert settings.AMPLITUDE_SECRET_KEY is not None
    return httpx.AsyncClient(
        base_url=BASE_URL,
        # A multi-hour export of a busy project is a large zip; 120s read
        # proved too short in staging (ReadTimeout on the 24h catch-up).
        timeout=httpx.Timeout(600.0, connect=15.0),
        follow_redirects=False,
        auth=(
            settings.AMPLITUDE_API_KEY.get_secret_value(),
            settings.AMPLITUDE_SECRET_KEY.get_secret_value(),
        ),
    )


def _floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def _event_time(value: Any) -> datetime | None:
    """Amplitude event_time is naive UTC ("2026-08-31 12:00:00.123456")."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_archive(spool: IO[bytes], return_event: str) -> list[dict[str, Any]]:
    """Zip of .json.gz NDJSON files -> matching event dicts; malformed lines
    and members are skipped with a log, never a crash. Members are streamed
    line-by-line and filtered to return_event as they're read: an export under
    the 4GB cap can still be GBs decompressed, so nothing else may accumulate
    in memory."""
    events: list[dict[str, Any]] = []
    with zipfile.ZipFile(spool) as archive:
        for name in archive.namelist():
            try:
                with archive.open(name) as member, gzip.open(member) as lines:
                    for line in lines:
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            log.warning(
                                "amplitude export: skipping malformed line in %s", name
                            )
                            continue
                        if isinstance(event, dict) and event.get("event_type") == return_event:
                            events.append(event)
            except (OSError, zipfile.BadZipFile):
                log.warning("amplitude export: skipping unreadable member %s", name)
                continue
    return events


async def _fetch_events(
    client: httpx.AsyncClient, since: datetime, until: datetime, return_event: str
) -> list[dict[str, Any]]:
    """Export hours [since, until) — Amplitude's start/end are inclusive
    hour stamps. 404 means no data for the range, which is a quiet window.
    The body is streamed to a temp file, never held in memory: responses run
    up to Amplitude's 4GB cap."""
    params = {
        "start": since.strftime("%Y%m%dT%H"),
        "end": (until - HOUR).strftime("%Y%m%dT%H"),
    }
    async with client.stream("GET", "/api/2/export", params=params) as response:
        if response.status_code == 404:
            return []
        if response.status_code >= 400:
            # Surface Amplitude's own complaint (size cap, retention, bad
            # range) before raising — the held cursor retries the window, and
            # without the body the operator can't tell WHY it keeps failing.
            body = (await response.aread()).decode("utf-8", errors="replace")
            log.warning(
                "amplitude export %d for %s..%s: %.300s",
                response.status_code,
                params["start"],
                params["end"],
                body,
            )
            if response.status_code == 400 and "Too much data" in body:
                raise SizeCapError(f"{params['start']}..{params['end']}")
            response.raise_for_status()
        with tempfile.TemporaryFile() as spool:
            async for chunk in response.aiter_bytes():
                spool.write(chunk)
            spool.seek(0)
            try:
                return _parse_archive(spool, return_event)
            except zipfile.BadZipFile:
                log.warning(
                    "amplitude export: response was not a zip archive; skipping window"
                )
                return []


def _returns_by_pro(
    events: list[dict[str, Any]], return_event: str
) -> dict[str, list[datetime]]:
    """pro_uuid -> sorted qualifying event times."""
    times: dict[str, list[datetime]] = {}
    for event in events:
        if event.get("event_type") != return_event:
            continue
        pro_id = str(event.get("user_id") or "")
        moment = _event_time(event.get("event_time"))
        if not pro_id or moment is None:
            log.warning("amplitude return event skipped: missing user_id/event_time")
            continue
        times.setdefault(pro_id, []).append(moment)
    return {pro: sorted(moments) for pro, moments in times.items()}


async def _outcomes_for(
    session: AsyncSession, returns: dict[str, list[datetime]]
) -> list[TouchOutcomeIn]:
    """First qualifying event inside each confirmed exposure's window."""
    if not returns:
        return []
    all_times = [moment for moments in returns.values() for moment in moments]
    exposures = (
        await session.execute(
            select(ExposureRow).where(
                ExposureRow.pro_id.in_(returns),
                ExposureRow.send_status == "confirmed",
                # Only sends whose 30d window could contain a batch event —
                # without this bound the query hydrates every exposure ever
                # sent to the returning pros, every tick, forever.
                ExposureRow.sent_at >= min(all_times) - WINDOW,
                ExposureRow.sent_at <= max(all_times),
            )
        )
    ).scalars().all()
    outcomes = []
    for exposure in exposures:
        assert exposure.sent_at is not None  # excluded by the query
        first = next(
            (
                moment
                for moment in returns[exposure.pro_id]
                if exposure.sent_at <= moment <= exposure.sent_at + WINDOW
            ),
            None,
        )
        if first is not None:
            outcomes.append(
                TouchOutcomeIn(exposure_id=exposure.id, source=SOURCE, first_return_at=first)
            )
    return outcomes


async def poll(
    session: AsyncSession, client: httpx.AsyncClient, settings: Settings, now: datetime
) -> dict[str, int]:
    """One bounded tick over complete hours. An HTTP failure raises before
    the cursor moves, so the next tick replays the same window (ingest is
    idempotent per (recommendation_id, source))."""
    cursor = await load_cursor(session, SOURCE)
    # Tolerant parse: a hand-edited cursor degrades to the bounded lookback
    # instead of crash-looping the poller every tick.
    since = parse_time(cursor.get("until")) or _floor_hour(now - LAG - INITIAL_LOOKBACK)
    until = min(_floor_hour(now - LAG), since + HOUR * MAX_HOURS_PER_TICK)
    if settings.ITERABLE_API_KEY is not None:
        # Never read past the hours whose exposures are fully registered: a
        # return matched against a not-yet-registered send would be dropped
        # while this cursor advanced past its hour — an unrecoverable false
        # negative once the checkpoint sweep closes the horizon. Holding
        # behind the iterable cursor makes a lagging/failing Iterable poller
        # pause return ingestion instead of corrupting it.
        it_since = parse_time((await load_cursor(session, "iterable")).get("since"))
        if it_since is not None:
            until = min(until, _floor_hour(it_since))
    if until - since < MIN_WINDOW:
        return {"returns": 0, "outcomes": 0}  # window too small; no call spent

    while True:
        try:
            events = await _fetch_events(
                client, since, until, settings.AMPLITUDE_RETURN_EVENT
            )
            break
        except SizeCapError:
            hours = int((until - since) / HOUR)
            if hours <= 1:
                raise  # even one hour is over the cap; hold the cursor
            until = since + HOUR * (hours // 2)
            log.warning("amplitude size cap: shrinking window to %dh", hours // 2)
    returns = _returns_by_pro(events, settings.AMPLITUDE_RETURN_EVENT)
    outcomes = await _outcomes_for(session, returns)
    if outcomes:
        await ingest(session, outcomes)

    await save_cursor(session, SOURCE, {"until": until.isoformat()})
    return {"returns": sum(len(v) for v in returns.values()), "outcomes": len(outcomes)}


async def poll_if_enabled(
    session: AsyncSession, client: httpx.AsyncClient, settings: Settings, now: datetime
) -> dict[str, int] | None:
    """One tick, gated by the learning kill switch ONLY (same rule as the
    checkpoint sweep — the fleet kill switch never stops measurement)."""
    fleet = await session.get(FleetControlRow, 1)
    if fleet is not None and fleet.learning_killed:
        return None
    return await poll(session, client, settings, now)
