import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_engine_from_config

from alembic import context
from waypoint.tables import Base

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False: the default silently DISABLES every
    # already-configured logger (waypoint.*), so an in-process migration run
    # would mute the app's own logging for the rest of the process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# DATABASE_URL wins over alembic.ini so Railway and CI need no ini edits.
_env_url = os.environ.get("DATABASE_URL")
if _env_url:
    config.set_main_option("sqlalchemy.url", _env_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# A migration that alters a live table needs ACCESS EXCLUSIVE, so any open
# transaction touching it blocks us. Without a timeout that block is silent and
# unbounded: the Railway preDeployCommand hangs, the deploy never finishes, and
# the logs stop after "Running upgrade". Fail fast and retry instead -- a
# transient holder clears within a poll or two, and a real one fails loudly.
# Short timeout, many attempts: each try gives up quickly so a queued
# ACCESS EXCLUSIVE never stalls the live app's own queries behind it, and we
# keep trying long enough to land in a gap between the holder's transactions.
# Railway runs preDeployCommand while the OLD instance still serves traffic,
# so a holder is expected -- we wait it out rather than demand an idle database.
LOCK_TIMEOUT_MS = int(os.environ.get("MIGRATION_LOCK_TIMEOUT_MS", "5000"))
LOCK_ATTEMPTS = int(os.environ.get("MIGRATION_LOCK_ATTEMPTS", "40"))
RETRY_CAP_SECONDS = int(os.environ.get("MIGRATION_RETRY_CAP_SECONDS", "15"))

# The API and the worker deploy from one railway.json, so both run
# "alembic upgrade head" against one database seconds apart. This session-level
# advisory lock makes them take turns: the loser waits, then finds the schema
# already at head and no-ops. Postgres frees it when the connection closes, so
# a killed pre-deploy cannot strand it. It is deliberately NOT on the migration
# connection -- issuing SQL there opens a transaction alembic never commits.
MIGRATION_LOCK_KEY = 0x7761_7970


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # As a connect-time server setting, not a SET statement: issuing SQL on
        # the connection here would open a transaction alembic never commits,
        # and every migration would silently roll back at close.
        connect_args={"server_settings": {"lock_timeout": f"{LOCK_TIMEOUT_MS}"}},
    )
    # No lock_timeout here: waiting our turn behind the other service is the
    # whole point, and that wait must not be cut short by the table-lock budget.
    gate = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with gate.connect() as gate_connection:
            await gate_connection.exec_driver_sql(
                f"SELECT pg_advisory_lock({MIGRATION_LOCK_KEY})"
            )
            await _upgrade_with_retry(connectable)
    finally:
        await gate.dispose()
        await connectable.dispose()


async def _upgrade_with_retry(connectable: AsyncEngine) -> None:
    for attempt in range(1, LOCK_ATTEMPTS + 1):
        try:
            async with connectable.connect() as connection:
                await connection.run_sync(do_run_migrations)
            return
        except DBAPIError as error:
            # 55P03 lock_not_available. Catch DBAPIError, not OperationalError:
            # asyncpg raises LockNotAvailableError, which SQLAlchemy wraps as a
            # bare DBAPIError, so an OperationalError clause silently never
            # matches and the "retry" gives up on the first attempt.
            if _sqlstate(error) != "55P03":
                raise
            if attempt == LOCK_ATTEMPTS:
                await _report_blockers(connectable)
                raise
            delay = min(2**attempt, RETRY_CAP_SECONDS)
            print(
                f"migration blocked on a table lock "
                f"(attempt {attempt}/{LOCK_ATTEMPTS}); retrying in {delay}s",
                flush=True,
            )
            if attempt == 1 or attempt % 10 == 0:
                await _report_blockers(connectable)
            await asyncio.sleep(delay)


def _sqlstate(error: DBAPIError) -> str | None:
    orig = error.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        state = getattr(candidate, "sqlstate", None) or getattr(
            candidate, "pgcode", None
        )
        if state:
            return str(state)
    return None


async def _report_blockers(connectable: AsyncEngine) -> None:
    """Name who is holding the table, into the deploy log.

    Without this the failure says only "lock timeout", which is true and
    useless: the holder is in the database, not in this process, and nobody
    reading a failed deploy can see it after the fact.
    """
    try:
        async with connectable.connect() as connection:
            rows = (
                await connection.exec_driver_sql(
                    r"""
                    SELECT pid, application_name, state,
                           round(extract(epoch from now()-xact_start)) AS xact_s,
                           left(regexp_replace(query, '\s+', ' ', 'g'), 80) AS q
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND xact_start IS NOT NULL
                    ORDER BY xact_start
                    LIMIT 10
                    """
                )
            ).fetchall()
        for row in rows:
            print(f"  holder: {tuple(row)}", flush=True)
    except Exception as error:  # noqa: BLE001 - diagnostics must never fail a deploy
        print(f"  (could not read pg_stat_activity: {error})", flush=True)


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
