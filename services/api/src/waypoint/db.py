"""Engine and transaction boundary."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(database_url: str, pool_size: int | None = None) -> AsyncEngine:
    # Worker sizes the pool for its concurrent loops (each holds a permanent
    # fleet-slot connection plus, while busy, two sessions); the API leaves it
    # at the SQLAlchemy default.
    if pool_size is None:
        return create_async_engine(database_url, pool_pre_ping=True)
    return create_async_engine(
        database_url, pool_pre_ping=True, pool_size=pool_size, max_overflow=pool_size
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
