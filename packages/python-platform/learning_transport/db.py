"""Postgres connection pools for services that own a database. Each opens with dict rows and a 10 s statement
timeout, and applies the service's idempotent migration before it is used. Import this module only from a
service that has psycopg installed."""

from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool

CONNECTION_OPTIONS: dict[str, Any] = {"row_factory": dict_row, "options": "-c statement_timeout=10000"}
MAX_CONNECTIONS = 8
CONNECT_TIMEOUT = 15  # seconds to wait for the first connection at startup


async def open_async_pool(url: str, migration: str) -> AsyncConnectionPool[Any]:
    pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        url, min_size=1, max_size=MAX_CONNECTIONS, timeout=5, open=False, kwargs=CONNECTION_OPTIONS
    )
    await pool.open(wait=True, timeout=CONNECT_TIMEOUT)
    async with pool.connection() as conn:
        await conn.execute(migration)
    return pool


def open_pool(url: str, migration: str) -> ConnectionPool[Any]:
    pool: ConnectionPool[Any] = ConnectionPool(
        url, min_size=1, max_size=MAX_CONNECTIONS, timeout=5, open=False, kwargs=CONNECTION_OPTIONS
    )
    pool.open(wait=True, timeout=CONNECT_TIMEOUT)
    with pool.connection() as conn:
        conn.execute(migration)
    return pool
