"""Engine and transaction boundary."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(
    database_url: str, pool_size: int = 10, *, pool_timeout: float = 30.0
) -> AsyncEngine:
    """Every caller sizes its own pool. There is no "default" size: the
    SQLAlchemy default (5 + 10 overflow) is a number nobody chose, and it is
    what starved the API in the QueuePool-timeout outage."""
    return create_async_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_timeout=pool_timeout,
        # Railway closes long-idle backends; recycle before the server does so
        # pre_ping does not pay a round trip on every checkout of a stale one.
        pool_recycle=1800,
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction per unit of work: commit on success, rollback on error."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
