"""Direct Amplitude return source via per-pro User Activity lookups.

The Export API cannot serve this project: staging measured ~4.0GB compressed
for the QUIETEST hour of the day, so peak hours provably exceed the API's 4GB
response cap and no window size works (hourly is its minimum granularity).
Instead of the whole product's firehose, this source asks about the ~hundred
Pros Waypoint actually touched (Dashboard REST API, verified contract in
docs/n8n/outcome-ingestion.md assumptions 7-9):

    usersearch    pro_uuid -> amplitude_id   (cached in amplitude_ids)
    useractivity  amplitude_id -> events, newest-first pages

A lookup at time T returns the pro's history BEFORE T, so one check settles
every horizon whose window closed before T. Coverage is stamped per exposure
(exposures.returns_checked_at); the checkpoint sweep stamps a measured
negative for a horizon only once the exposure's stamp proves returns were
fetched past that horizon's close (checkpoints.py) — replacing the export
cursor's global clock cap.

Fail-closed edges: a pro whose amplitude identity doesn't resolve to exactly
one EXACT user_id match (usersearch is a partial match; matches[0] could be a
different person) is cached unresolved and never stamped — their horizons
stay unmeasured rather than becoming false negatives. Call volume is bounded
by CALL_BUDGET per tick: 25 x 12 ticks/hour stays under the Dashboard REST
API's 360 queries/hour limit.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.checkpoints import GRACE, HORIZONS
from waypoint.cursors import save_cursor
from waypoint.models import TouchOutcomeIn
from waypoint.outcomes import ingest
from waypoint.settings import Settings
from waypoint.tables import AmplitudeIdRow, ExposureRow, FleetControlRow, TouchOutcomeRow

log = logging.getLogger("waypoint.amplitude_source")

SOURCE = "amplitude"
BASE_URL = "https://amplitude.com"
# Longest return horizon: an event after sent_at + 30d measures nothing.
WINDOW = timedelta(days=30)
# Dashboard REST API budget. The check runs BEFORE each pro's lookups, so a
# tick's true worst case is CALL_BUDGET - 1 + 1 (usersearch) + PAGE_CAP = 29
# calls; at 12 ticks/hour (POLL_SECONDS=300) that is 348/hour against the
# documented 360/hour — raise PAGE_CAP or CALL_BUDGET only in step.
CALL_BUDGET = 25
PAGE_SIZE = 1000  # useractivity's documented maximum
# ponytail: a pro with >5 pages (5000 events) since the oldest pending send is
# hyper-active; returns found are still ingested, but no coverage is stamped
# (an earlier return may hide past the cap), so their silent horizons stay
# unmeasured rather than becoming negatives. Raise the cap if that bites.
PAGE_CAP = 5
# An unresolved amplitude identity is retried this often, not every tick.
RETRY_UNRESOLVED = timedelta(hours=24)
# Out-paged pros back off on the same cadence — without it a hyper-active
# silent pro re-spends usersearch + PAGE_CAP calls EVERY tick, and a handful
# of them would eat the whole budget and starve everyone else.
# ponytail: in-memory, resets on worker restart — durable next_check_at on
# the exposure if restarts ever churn enough to matter.
_OUTPAGED_UNTIL: dict[str, datetime] = {}
# Overfetch factor for the due query: rows the Python horizon filter drops
# (stamped for now, next horizon not yet due) still occupy the SQL limit.
# ponytail: fine at ~100 sends/day; revisit if the pending set outgrows it.
FETCH_LIMIT = 500


def make_client(settings: Settings) -> httpx.AsyncClient:
    assert settings.AMPLITUDE_API_KEY is not None
    assert settings.AMPLITUDE_SECRET_KEY is not None
    return httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=httpx.Timeout(60.0, connect=15.0),
        follow_redirects=False,
        auth=(
            settings.AMPLITUDE_API_KEY.get_secret_value(),
            settings.AMPLITUDE_SECRET_KEY.get_secret_value(),
        ),
    )


def _event_time(value: Any) -> datetime | None:
    """Amplitude event_time is naive UTC ("2026-08-31 12:00:00.123456")."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        return None


def _due_horizon(exposure: ExposureRow, now: datetime) -> bool:
    """True if some horizon's window has closed — plus GRACE, because
    Amplitude indexes late and a lookup right at the close would miss a
    just-inside-the-window return, minting a permanent false negative — that
    the exposure's coverage stamp doesn't prove was fetched."""
    assert exposure.sent_at is not None  # excluded by the due query
    checked = exposure.returns_checked_at
    return any(
        exposure.sent_at + window + GRACE <= now
        and (checked is None or checked < exposure.sent_at + window)
        for _, window in HORIZONS
    )


async def _due_exposures(session: AsyncSession, now: datetime) -> list[ExposureRow]:
    """Confirmed exposures with an unfetched, closed horizon and no amplitude
    outcome yet, oldest send first. The terminal states are excluded in SQL,
    not just in Python: a fully-covered silent exposure matches the confirmed/
    no-outcome predicates FOREVER, and the set of those grows without bound —
    left in the query they would eventually fill the FETCH_LIMIT head and
    permanently starve every newer send of measurement."""
    rows = (
        await session.execute(
            select(ExposureRow)
            .where(
                ExposureRow.send_status == "confirmed",
                ExposureRow.sent_at.is_not(None),
                ExposureRow.sent_at <= now - HORIZONS[0][1] - GRACE,
                (ExposureRow.returns_checked_at.is_(None))
                | (ExposureRow.returns_checked_at < ExposureRow.sent_at + WINDOW),
                # A found return settles every horizon (flags derive from
                # first_return_at), so the exposure is done being checked.
                ~select(TouchOutcomeRow.id)
                .where(
                    TouchOutcomeRow.exposure_id == ExposureRow.id,
                    TouchOutcomeRow.source == SOURCE,
                )
                .exists(),
            )
            .order_by(ExposureRow.sent_at)
            .limit(FETCH_LIMIT)
        )
    ).scalars().all()
    return [e for e in rows if _due_horizon(e, now)]


async def _amplitude_id(
    session: AsyncSession, client: httpx.AsyncClient, pro_id: str, now: datetime
) -> tuple[str | None, int]:
    """Cached pro_uuid -> amplitude_id, else one usersearch call. Requires
    exactly one EXACT user_id match — usersearch matches partially, and
    matches[0] could be a different person (contract doc, assumption 7).
    Returns (amplitude_id or None, calls spent)."""
    row = await session.get(AmplitudeIdRow, pro_id)
    if row is not None:
        fresh = row.updated_at is not None and now - row.updated_at < RETRY_UNRESOLVED
        if row.amplitude_id is not None or fresh:
            return row.amplitude_id, 0
    response = await client.get("/api/2/usersearch", params={"user": pro_id})
    response.raise_for_status()
    matches = response.json().get("matches") or []
    exact = [m for m in matches if m.get("user_id") == pro_id]
    amplitude_id = str(exact[0]["amplitude_id"]) if len(exact) == 1 else None
    if amplitude_id is None:
        log.warning(
            "amplitude usersearch: %d exact match(es) for pro %s; cannot measure returns",
            len(exact), pro_id,
        )
    if row is None:
        session.add(AmplitudeIdRow(pro_id=pro_id, amplitude_id=amplitude_id, updated_at=now))
    else:
        row.amplitude_id = amplitude_id
        row.updated_at = now
    return amplitude_id, 1


async def _session_starts(
    client: httpx.AsyncClient,
    amplitude_id: str,
    oldest_needed: datetime,
    return_event: str,
) -> tuple[list[datetime], int, bool]:
    """Qualifying return-event times, paging newest-first until the page
    reaches past oldest_needed, history is exhausted, or PAGE_CAP. Returns
    (sorted times, calls spent, covered) — covered=False means PAGE_CAP was
    exhausted before reaching oldest_needed, so an earlier return may be
    hiding past the cap and NO coverage may be stamped off this lookup."""
    times: list[datetime] = []
    calls = 0
    covered = False
    for page in range(PAGE_CAP):
        response = await client.get(
            "/api/2/useractivity",
            params={"user": amplitude_id, "limit": PAGE_SIZE, "offset": page * PAGE_SIZE},
        )
        calls += 1
        response.raise_for_status()
        events = response.json().get("events") or []
        page_times = [
            moment for e in events if isinstance(e, dict)
            if (moment := _event_time(e.get("event_time"))) is not None
        ]
        times.extend(
            moment for e in events if isinstance(e, dict)
            if e.get("event_type") == return_event
            and (moment := _event_time(e.get("event_time"))) is not None
        )
        if len(events) < PAGE_SIZE:
            covered = True  # start of the pro's history
            break
        if page_times and min(page_times) < oldest_needed:
            covered = True  # paged past every pending send
            break
    if not covered:
        log.warning(
            "amplitude useractivity: %s out-paged PAGE_CAP before reaching "
            "%s; returns ingested but no coverage stamped (see PAGE_CAP note)",
            amplitude_id, oldest_needed,
        )
    return sorted(times), calls, covered


async def poll(
    session: AsyncSession, client: httpx.AsyncClient, settings: Settings, now: datetime
) -> dict[str, int]:
    """One bounded tick: for each pro with a due exposure, one activity
    lookup settles every pending exposure of that pro. Ingest is idempotent
    per (recommendation_id, source) and the coverage stamp is monotonic, so a
    failed tick replays safely."""
    due = await _due_exposures(session, now)
    by_pro: dict[str, list[ExposureRow]] = {}
    for exposure in due:
        by_pro.setdefault(exposure.pro_id, []).append(exposure)

    calls = checked = found = unresolved = 0
    for pro_id, exposures in by_pro.items():
        if calls >= CALL_BUDGET:
            break
        if _OUTPAGED_UNTIL.get(pro_id, now) > now:
            continue
        amplitude_id, spent = await _amplitude_id(session, client, pro_id, now)
        calls += spent
        if amplitude_id is None:
            unresolved += 1
            continue
        if calls >= CALL_BUDGET:
            break
        assert all(e.sent_at is not None for e in exposures)
        oldest_needed = min(e.sent_at for e in exposures)  # type: ignore[type-var]
        returns, spent, covered = await _session_starts(
            client, amplitude_id, oldest_needed, settings.AMPLITUDE_RETURN_EVENT
        )
        calls += spent
        outcomes = []
        for exposure in exposures:
            first = next(
                (m for m in returns if exposure.sent_at <= m <= exposure.sent_at + WINDOW),
                None,
            )
            if first is not None:
                outcomes.append(
                    TouchOutcomeIn(exposure_id=exposure.id, source=SOURCE, first_return_at=first)
                )
                found += 1
        if outcomes:
            await ingest(session, outcomes)  # commits; stamps below re-commit
        if covered:
            for exposure in exposures:
                # The lookup covered this pro's history before `now` — minus
                # GRACE, because an event that happened just before the lookup
                # may not be indexed yet; claiming it was fetched would let
                # the sweep mint a false negative off a late-indexed return.
                exposure.returns_checked_at = now - GRACE
                checked += 1
        else:
            _OUTPAGED_UNTIL[pro_id] = now + RETRY_UNRESOLVED
        await session.commit()

    # Heartbeat: the row's existence tells the checkpoint sweep that return
    # coverage is per-exposure stamps now (checkpoints.sweep_if_enabled).
    await save_cursor(session, SOURCE, {"mode": "user_activity", "at": now.isoformat()})
    return {"checked": checked, "returns": found, "unresolved": unresolved, "calls": calls}


async def poll_if_enabled(
    session: AsyncSession, client: httpx.AsyncClient, settings: Settings, now: datetime
) -> dict[str, int] | None:
    """One tick, gated by the learning kill switch ONLY (same rule as the
    checkpoint sweep — the fleet kill switch never stops measurement)."""
    fleet = await session.get(FleetControlRow, 1)
    if fleet is not None and fleet.learning_killed:
        return None
    return await poll(session, client, settings, now)
