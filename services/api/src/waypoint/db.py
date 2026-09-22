"""Engine and transaction boundary."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# A session left open in a transaction holds its table locks until it is
# closed. One leaked for 2.5 hours holding ACCESS SHARE on runs, which blocked
# every "ALTER TABLE runs" a deploy attempted -- and each failed deploy left
# its own queued ALTER behind, so the jam compounded and could not self-clear.
# Postgres kills such a session for us; the app must never rely on tidiness.
IDLE_IN_TRANSACTION_TIMEOUT_MS = int(
    os.environ.get("IDLE_IN_TRANSACTION_TIMEOUT_MS", "120000")
)
# A single statement should never run for minutes either. Generous enough for
# the slowest legitimate query, short enough that a wedged one cannot outlive
# a deploy.
STATEMENT_TIMEOUT_MS = int(os.environ.get("STATEMENT_TIMEOUT_MS", "300000"))


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
        connect_args={
            "server_settings": {
                "idle_in_transaction_session_timeout": str(
                    IDLE_IN_TRANSACTION_TIMEOUT_MS
                ),
                "statement_timeout": str(STATEMENT_TIMEOUT_MS),
            }
        },
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
