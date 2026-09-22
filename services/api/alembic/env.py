import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
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
LOCK_TIMEOUT_MS = int(os.environ.get("MIGRATION_LOCK_TIMEOUT_MS", "10000"))
LOCK_ATTEMPTS = int(os.environ.get("MIGRATION_LOCK_ATTEMPTS", "5"))

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
        except OperationalError as error:
            # 55P03 lock_not_available: a process outside this deploy holds the
            # table. The transaction rolled back whole, so a retry is safe.
            if getattr(error.orig, "sqlstate", None) != "55P03":
                raise
            if attempt == LOCK_ATTEMPTS:
                raise
            delay = 2**attempt
            print(
                f"migration blocked on a table lock "
                f"(attempt {attempt}/{LOCK_ATTEMPTS}); retrying in {delay}s",
                flush=True,
            )
            await asyncio.sleep(delay)


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
