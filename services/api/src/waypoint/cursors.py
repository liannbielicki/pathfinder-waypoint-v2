"""Durable poll cursors shared by the outcome pollers.

One row per source in `poll_cursors`. A cursor is saved only after the
window it covers has been ingested successfully, so a failed tick replays
the same window instead of skipping it — the pollers are idempotent per
(recommendation_id, source), so replays are safe.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from waypoint.tables import PollCursorRow


def parse_time(value: Any) -> datetime | None:
    """Tolerant, UTC-normalizing timestamp parse shared by the pollers — for
    cursor values and source event times alike. Anything unparseable is None,
    so a hand-edited cursor degrades to the source's bounded lookback instead
    of crash-looping the poller; a naive value is taken as UTC so it can never
    poison aware-datetime comparisons."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace(" +", "+").replace(" Z", "Z"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def load_cursor(session: AsyncSession, source: str) -> dict[str, Any]:
    row = await session.get(PollCursorRow, source)
    return dict(row.cursor) if row is not None else {}


async def save_cursor(session: AsyncSession, source: str, cursor: dict[str, Any]) -> None:
    row = await session.get(PollCursorRow, source)
    if row is None:
        session.add(PollCursorRow(source=source, cursor=cursor))
    else:
        row.cursor = cursor
    await session.commit()
