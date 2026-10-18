"""Postgres connection pools for services that own a database. Each opens with dict rows and a 10 s statement
timeout, and applies the service's numbered migrations before it is used. Import this module only from a
service that has psycopg installed."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Self

from psycopg import AsyncConnection, Connection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool

CONNECTION_OPTIONS: dict[str, Any] = {"row_factory": dict_row, "options": "-c statement_timeout=10000"}
MAX_CONNECTIONS = 8
CONNECT_TIMEOUT = 15  # seconds to wait for the first connection at startup

MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
# Lock scopes, as in the Go platform package (lock.go): a scope and its key hash to one transaction-scoped advisory
# lock, derived exactly as the Go services derive it.
LOCK_MIGRATIONS = "migrations"  # no key: serializes replicas migrating at once
LOCK_CLASSROOM = "classroom"  # a classroom's purge against new rows in it
LOCK_USER = "user"  # a deleted account's purge against new rows for it
LOCK = "SELECT pg_advisory_xact_lock(hashtextextended(%s::text || ':' || %s::text, 0))"
CREATE_LEDGER = (
    "CREATE TABLE IF NOT EXISTS schema_migrations"
    "(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
)
APPLIED = "SELECT version FROM schema_migrations"
RECORD = "INSERT INTO schema_migrations(version) VALUES(%s) ON CONFLICT DO NOTHING"


@dataclass(frozen=True)
class Migration:
    """One numbered SQL file, NNN_name.sql, applied once and recorded in schema_migrations."""

    version: int
    name: str
    sql: str


def migrations(directory: Path) -> list[Migration]:
    """The NNN_name.sql files in directory in version order. Versions must run 1..n without gaps or duplicates,
    so a missing or misnamed file fails startup instead of silently skipping a schema change."""
    found = []
    for path in directory.glob("*.sql"):
        match = MIGRATION_NAME.match(path.name)
        if match is None:
            raise ValueError(f"migration {path.name!r} must be named NNN_name.sql")
        found.append(Migration(int(match[1]), path.name, path.read_text()))
    found.sort(key=lambda m: m.version)
    if not found:
        raise ValueError(f"no migrations found in {directory}")
    for position, m in enumerate(found, start=1):
        if m.version != position:
            raise ValueError(f"migration versions must run 1..n without gaps or duplicates; found {m.name}")
    return found


def lock(conn: Connection[Any], scope: str, key: object) -> None:
    """Take the exclusive advisory lock on (scope, key) until conn's transaction ends."""
    conn.execute(LOCK, (scope, str(key)))


async def lock_async(conn: AsyncConnection[Any], scope: str, key: object) -> None:
    """lock, on an async connection."""
    await conn.execute(LOCK, (scope, str(key)))


def pending(steps: list[Migration], applied: set[int]) -> list[Migration]:
    return [m for m in steps if m.version not in applied]


async def open_async_pool(url: str, directory: Path) -> AsyncConnectionPool[Any]:
    steps = migrations(directory)
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        url, min_size=1, max_size=MAX_CONNECTIONS, timeout=5, open=False, kwargs=CONNECTION_OPTIONS
    )
    await pool.open(wait=True, timeout=CONNECT_TIMEOUT)
    async with pool.connection() as conn, conn.transaction():
        await lock_async(conn, LOCK_MIGRATIONS, "")
        await conn.execute(CREATE_LEDGER)
        cur = await conn.execute(APPLIED)
        for m in pending(steps, {row["version"] for row in await cur.fetchall()}):
            await conn.execute(m.sql)
            await conn.execute(RECORD, (m.version,))
    return pool


def open_pool(url: str, directory: Path) -> ConnectionPool[Any]:
    steps = migrations(directory)
    pool: ConnectionPool[Any] = ConnectionPool(
        url, min_size=1, max_size=MAX_CONNECTIONS, timeout=5, open=False, kwargs=CONNECTION_OPTIONS
    )
    pool.open(wait=True, timeout=CONNECT_TIMEOUT)
    with pool.connection() as conn, conn.transaction():
        lock(conn, LOCK_MIGRATIONS, "")
        conn.execute(CREATE_LEDGER)
        applied = {row["version"] for row in conn.execute(APPLIED).fetchall()}
        for m in pending(steps, applied):
            conn.execute(m.sql)
            conn.execute(RECORD, (m.version,))
    return pool


class AsyncPooled:
    """A service's own database behind an async pool, opened with the service's MIGRATIONS applied."""

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
