"""postgres connection pools for services that own a database."""

import asyncio
import contextlib
import os
import re
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Self

from psycopg import AsyncConnection, Connection, errors, sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool

CONNECTION_OPTIONS: dict[str, Any] = {"row_factory": dict_row, "options": "-c statement_timeout=10000"}
MAX_CONNECTIONS = 8
CONNECT_TIMEOUT = 15  # seconds to wait for the first connection at startup
MIGRATION_TIMEOUT = 600  # seconds a migration, or waiting for the migration lock, may take
MIGRATION_LOCK_WAIT = 3  # seconds migration ddl may queue for a table lock ahead of its writers


def pool_limits() -> tuple[int, int]:
    minimum, maximum = (
        int(os.environ.get("DB_POOL_MIN", "1")),
        int(os.environ.get("DB_POOL_MAX", str(MAX_CONNECTIONS))),
    )
    if not 0 <= minimum <= maximum <= 1000 or maximum == 0:
        raise ValueError("Require 0 <= DB_POOL_MIN <= DB_POOL_MAX <= 1000 and DB_POOL_MAX > 0")
    return minimum, maximum


MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
# a migration whose first line is this directive builds one index outside a transaction.
CONCURRENT_DIRECTIVE = "-- migrate: concurrent-index"
CONCURRENT_INDEX = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)"
    r"\s+ON\s+([a-z_][a-z0-9_]*)\s*(?:USING\s+\w+\s*)?\([^;]+;?",
    re.IGNORECASE,
)
# lock scopes, as in the go platform package (lock.go): a scope and its key hash to one transaction-scoped
# advisory lock, derived exactly as the go services derive it.
LOCK_MIGRATIONS = "migrations"  # no key: serializes replicas migrating at once
LOCK_CLASSROOM = "classroom"  # a classroom's purge against new rows in it
LOCK_USER = "user"  # a deleted account's purge against new rows for it
LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s::text || ':' || %s::text, 0))"
TRY_SESSION_LOCK = "SELECT pg_try_advisory_lock(hashtextextended(%s::text || ':' || %s::text, 0)) AS locked"
CREATE_LEDGER = (
    "CREATE TABLE IF NOT EXISTS schema_migrations"
    "(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
)
APPLIED = "SELECT version FROM schema_migrations"
RECORD = "INSERT INTO schema_migrations(version) VALUES(%s) ON CONFLICT DO NOTHING"
LONG_MIGRATION = "SELECT set_config('statement_timeout', %s, true)"
BOUNDED_LOCK_WAIT = "SELECT set_config('lock_timeout', %s, true)"


@dataclass(frozen=True)
class Migration:
    """one numbered sql file, NNN_name.sql, applied once and recorded in schema_migrations."""

    version: int
    name: str
    sql: str
    # the (index, table) a concurrent-index migration builds.
    concurrent_index: tuple[str, str] | None = None


def concurrent_target(name: str, sql: str) -> tuple[str, str] | None:
    """the index and table of a migration marked with the directive; a misplaced concurrent build fails here."""
    lines = sql.strip().splitlines()
    if not lines or lines[0].strip() != CONCURRENT_DIRECTIVE:
        if re.search(r"\bCONCURRENTLY\b", sql, re.IGNORECASE):
            raise ValueError(
                f"migration {name!r} builds concurrently; start it with {CONCURRENT_DIRECTIVE!r}"
            )
        return None
    statement = "\n".join(lines[1:]).strip()
    match = CONCURRENT_INDEX.fullmatch(statement)
    if match is None:
        raise ValueError(f"migration {name!r} must hold exactly one CREATE INDEX CONCURRENTLY statement")
    return match[1].lower(), match[2].lower()


def migrations(directory: Path) -> list[Migration]:
    """the NNN_name.sql files in directory in version order."""
    found = []
    for path in directory.glob("*.sql"):
        match = MIGRATION_NAME.match(path.name)
        if match is None:
            raise ValueError(f"migration {path.name!r} must be named NNN_name.sql")
        sql = path.read_text()
        found.append(Migration(int(match[1]), path.name, sql, concurrent_target(path.name, sql)))
    found.sort(key=lambda m: m.version)
    if not found:
        raise ValueError(f"no migrations found in {directory}")
    for position, m in enumerate(found, start=1):
        if m.version != position:
            raise ValueError(f"migration versions must run 1..n without gaps or duplicates; found {m.name}")
    return found


def lock(conn: Connection[Any], scope: str, key: object) -> None:
    """take the exclusive advisory lock on (scope, key) until conn's transaction ends."""
    conn.execute(LOCK, (scope, str(key)))


async def lock_async(conn: AsyncConnection[Any], scope: str, key: object) -> None:
    """lock, on an async connection."""
    await conn.execute(LOCK, (scope, str(key)))


def pending(steps: list[Migration], applied: set[int]) -> list[Migration]:
    return [m for m in steps if m.version not in applied]


async def pending_async(conn: AsyncConnection[Any], steps: list[Migration]) -> list[Migration]:
    await conn.execute(CREATE_LEDGER)
    cur = await conn.execute(APPLIED)
    return pending(steps, {row["version"] for row in await cur.fetchall()})


async def pending_unlocked(conn: AsyncConnection[Any], steps: list[Migration]) -> list[Migration]:
    """a read-only look at what is pending, so an up-to-date database needs no migration lock."""
    row = await (
        await conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL AS ready")
    ).fetchone()
    if not row or not row["ready"]:
        return steps
    cur = await conn.execute(APPLIED)
    return pending(steps, {row["version"] for row in await cur.fetchall()})


async def apply_async(conn: AsyncConnection[Any], steps: Iterable[Migration]) -> None:
    for step in steps:
        await conn.execute(step.sql)
        await conn.execute(RECORD, (step.version,))


def long_migration() -> tuple[str]:
    """lets this transaction's migration statements, and its wait for the migration lock, run long."""
    return (f"{MIGRATION_TIMEOUT}s",)


def bounded_lock_waits() -> tuple[str]:
    """makes this transaction's ddl give up a table lock it cannot get quickly, set after the migration lock."""
    return (f"{MIGRATION_LOCK_WAIT}s",)


async def migrate_async(conn: AsyncConnection[Any], steps: list[Migration], *, locked: bool) -> None:
    """applies steps in one transaction, again whenever its ddl gave up a table lock, until the migration timeout.

    unless locked (this session already holds the migration lock and steps are pending), it takes the lock and
    applies whichever of steps are still pending.
    """

    async def attempt() -> None:
        # another replica may hold the migration lock for a long migration; only table locks are bounded.
        await conn.execute(LONG_MIGRATION, long_migration())
        waiting = steps
        if not locked:
            await lock_async(conn, LOCK_MIGRATIONS, "")
            waiting = await pending_async(conn, steps)
        await conn.execute(BOUNDED_LOCK_WAIT, bounded_lock_waits())
        await apply_async(conn, waiting)

    async with asyncio.timeout(MIGRATION_TIMEOUT):
        while True:
            try:
                async with conn.transaction():
                    await attempt()
                return
            except errors.LockNotAvailable:
                # writers queued behind the ddl's lock request run while it waits here.
                await asyncio.sleep(MIGRATION_LOCK_WAIT)


@contextlib.asynccontextmanager
async def long_statements(conn: AsyncConnection[Any]) -> AsyncIterator[None]:
    """lifts the statement timeout for a concurrent build, outside any transaction."""
    await conn.execute("SELECT set_config('statement_timeout', %s, false)", (f"{MIGRATION_TIMEOUT}s",))
    try:
        yield
    finally:
        await conn.execute("RESET statement_timeout")


async def apply_concurrent_index(conn: AsyncConnection[Any], step: Migration) -> None:
    assert step.concurrent_index is not None
    index, table = step.concurrent_index
    cur = await conn.execute(
        """SELECT i.indisvalid, n.nspname
           FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE i.indrelid = to_regclass(%s) AND c.relname = %s""",
        (table, index),
    )
    existing = await cur.fetchone()
    async with long_statements(conn):
        if existing is not None and not existing["indisvalid"]:
            # an interrupted build can leave an unusable index with the target name.
            await conn.execute(
                sql.SQL("DROP INDEX CONCURRENTLY {}").format(sql.Identifier(existing["nspname"], index))
            )
            existing = None
        if existing is None:
            await conn.execute(step.sql)
    await conn.execute(RECORD, (step.version,))


async def migrate_with_concurrent_indexes(url: str, steps: list[Migration]) -> None:
    # a dedicated session holds the migration lock across autocommit ddl and releases it on close.
    async with await AsyncConnection.connect(
        url, autocommit=True, connect_timeout=CONNECT_TIMEOUT, **CONNECTION_OPTIONS
    ) as conn:
        if not await pending_unlocked(conn, steps):
            return
        # waiting inside a blocking select would hold a snapshot needed by a concurrent index build.
        async with asyncio.timeout(MIGRATION_TIMEOUT):
            while True:
                row = await (await conn.execute(TRY_SESSION_LOCK, (LOCK_MIGRATIONS, ""))).fetchone()
                if row and row["locked"]:
                    break
                await asyncio.sleep(0.25)
        # each step commits on its own, so a long backfill holds no table lock an earlier step took.
        for step in await pending_async(conn, steps):
            if step.concurrent_index is not None:
                await apply_concurrent_index(conn, step)
            else:
                await migrate_async(conn, [step], locked=True)


async def open_async_pool(url: str, directory: Path) -> AsyncConnectionPool[Any]:
    steps = migrations(directory)
    minimum, maximum = pool_limits()
    online = any(step.concurrent_index is not None for step in steps)
    if online:
        await migrate_with_concurrent_indexes(url, steps)
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        url, min_size=minimum, max_size=maximum, timeout=5, open=False, kwargs=CONNECTION_OPTIONS
    )
    await pool.open(wait=True, timeout=CONNECT_TIMEOUT)
    if not online:
        try:
            async with pool.connection() as conn:
                await migrate_async(conn, steps, locked=False)
        except BaseException:
            await pool.close()
            raise
    return pool


def open_pool(url: str, directory: Path) -> ConnectionPool[Any]:
    steps = migrations(directory)
    if any(step.concurrent_index is not None for step in steps):
        raise ValueError("concurrent-index migrations need the async pool")
    minimum, maximum = pool_limits()
    pool: ConnectionPool[Any] = ConnectionPool(
        url, min_size=minimum, max_size=maximum, timeout=5, open=False, kwargs=CONNECTION_OPTIONS
    )
    pool.open(wait=True, timeout=CONNECT_TIMEOUT)
    try:
        with pool.connection() as conn:
            migrate(conn, steps)
    except BaseException:
        pool.close()
        raise
    return pool


def migrate(conn: Connection[Any], steps: list[Migration]) -> None:
    """migrate_async for a sync connection, unlocked: all pending steps in one transaction."""
    deadline = time.monotonic() + MIGRATION_TIMEOUT
    while True:
        try:
            with conn.transaction():
                conn.execute(LONG_MIGRATION, long_migration())
                lock(conn, LOCK_MIGRATIONS, "")
                conn.execute(CREATE_LEDGER)
                applied = {row["version"] for row in conn.execute(APPLIED).fetchall()}
                conn.execute(BOUNDED_LOCK_WAIT, bounded_lock_waits())
                for m in pending(steps, applied):
                    conn.execute(m.sql)
                    conn.execute(RECORD, (m.version,))
            return
        except errors.LockNotAvailable:
            if time.monotonic() >= deadline:
                raise
            time.sleep(MIGRATION_LOCK_WAIT)


class AsyncPooled:
    """a service's own database behind an async pool, opened with the service's migrations applied."""

    MIGRATIONS: ClassVar[Path]

    def __init__(self, pool: AsyncConnectionPool[Any]) -> None:
        self.pool = pool

    @classmethod
    async def open(cls, url: str) -> Self:
        return cls(await open_async_pool(url, cls.MIGRATIONS))

    async def ping(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("SELECT 1")

    async def close(self) -> None:
        await self.pool.close()
