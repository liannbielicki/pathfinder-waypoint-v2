import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from waypoint.models import TouchOutcomeIn
from waypoint.outcomes import ingest
from waypoint.tables import TouchOutcomeRow


async def _ingest_with_new_session(
    factory: async_sessionmaker, body: list[TouchOutcomeIn]
) -> dict[str, int]:
    async with factory() as session:
        return await ingest(session, body)


async def test_concurrent_duplicate_outcomes_do_not_500_or_lose_siblings(
    db_session_factory,
) -> None:
    # Two sibling requests race to submit the SAME (recommendation_id, source)
    # pair concurrently. One batch also carries an unrelated sibling item —
    # the fix must not lose it when the race is resolved.
    dup = TouchOutcomeIn(recommendation_id="dup-winner", source="iterable_n8n", pro_id="pro_1")
    sibling = TouchOutcomeIn(
        recommendation_id="dup-winner-2", source="iterable_n8n", pro_id="pro_1"
    )
    results = await asyncio.gather(
        _ingest_with_new_session(db_session_factory, [dup, sibling]),
        _ingest_with_new_session(db_session_factory, [dup]),
    )
    for result in results:
        assert result["stored"] >= 1  # neither call raised / 500'd

    async with db_session_factory() as session:
        rows = (await session.execute(select(TouchOutcomeRow))).scalars().all()
    keys = {(r.recommendation_id, r.source) for r in rows}
    # Exactly one row per (recommendation_id, source) — the duplicate collapsed
    # into one row, and the sibling item survived the race.
    assert keys == {("dup-winner", "iterable_n8n"), ("dup-winner-2", "iterable_n8n")}
