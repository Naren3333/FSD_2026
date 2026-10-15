"""The chatbot's own database: threads and messages (ADR 0002). It holds conversations only, never business
records; what a turn may read comes from Content, with the user's token."""

import json
from pathlib import Path
from typing import Any, Protocol

from learning_transport.db import AsyncPooled
from psycopg.types.json import Jsonb

MIGRATIONS = Path(__file__).parent / "migrations"
TITLE_CHARS = 80
THREAD_COLUMNS = "id, classroom_id, role, title, created_at, last_message_at"
MESSAGE_COLUMNS = "id, role, content, outcome, citations, created_at"


class Store(Protocol):
    async def create_thread(self, org: str, user: str, role: str, classroom_id: str) -> dict[str, Any]: ...

    async def list_threads(self, org: str, user: str, classroom_id: str | None) -> list[dict[str, Any]]: ...

    async def thread(self, org: str, user: str, thread_id: str) -> dict[str, Any] | None: ...

    async def messages(self, thread_id: str) -> list[dict[str, Any]]: ...

    async def start_turn(self, thread_id: str, content: str, history_turns: int) -> list[dict[str, str]]:
        """The last `history_turns` messages (oldest first), then saves the user's message and, for the first,
        the thread's title."""
        ...

    async def save_answer(self, thread_id: str, answer: dict[str, Any]) -> str: ...


class PostgresStore(AsyncPooled):
    MIGRATIONS = MIGRATIONS

    async def create_thread(self, org: str, user: str, role: str, classroom_id: str) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"INSERT INTO threads (org_id, user_id, role, classroom_id) VALUES (%s,%s,%s,%s)"
                f" RETURNING {THREAD_COLUMNS}",
                (org, user, role, classroom_id),
            )
            return dict(await cur.fetchone() or {})

    async def list_threads(self, org: str, user: str, classroom_id: str | None) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {THREAD_COLUMNS} FROM threads WHERE org_id=%s AND user_id=%s"
                " AND (%s::uuid IS NULL OR classroom_id=%s::uuid) ORDER BY last_message_at DESC, id DESC LIMIT 100",
                (org, user, classroom_id, classroom_id),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def thread(self, org: str, user: str, thread_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {THREAD_COLUMNS} FROM threads WHERE id=%s AND org_id=%s AND user_id=%s",
                (thread_id, org, user),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def messages(self, thread_id: str) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE thread_id=%s ORDER BY created_at, id",
                (thread_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def start_turn(self, thread_id: str, content: str, history_turns: int) -> list[dict[str, str]]:
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT role, content FROM messages WHERE thread_id=%s ORDER BY created_at DESC, id DESC LIMIT %s",
                (thread_id, history_turns),
            )
            history = [{"role": r["role"], "content": r["content"]} for r in await cur.fetchall()][::-1]
            await conn.execute(
                "INSERT INTO messages (thread_id, role, content) VALUES (%s, 'user', %s)",
                (thread_id, content),
            )
            await conn.execute(
                "UPDATE threads SET title=left(%s, %s), last_message_at=now() WHERE id=%s AND title IS NULL",
                (content, TITLE_CHARS, thread_id),
            )
            return history

    async def save_answer(self, thread_id: str, answer: dict[str, Any]) -> str:
        citations = answer.get("citations")
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "INSERT INTO messages (thread_id, role, content, outcome, rewritten_query, is_on_topic,"
                " retrieved_chunk_ids, citations, prompt_version, model, usage_tokens, latency_ms)"
                " VALUES (%s, 'assistant', %s, %s, %s, %s, %s::uuid[], %s, %s, %s, %s, %s) RETURNING id",
                (
                    thread_id,
                    answer["content"],
                    answer["outcome"],
                    answer.get("rewritten_query"),
                    answer.get("is_on_topic"),
                    answer.get("retrieved_chunk_ids"),
                    None if citations is None else Jsonb(citations, dumps=json.dumps),
                    answer.get("prompt_version"),
                    answer.get("model"),
                    answer.get("usage_tokens"),
                    answer.get("latency_ms"),
                ),
            )
            row = await cur.fetchone()
            await conn.execute("UPDATE threads SET last_message_at=now() WHERE id=%s", (thread_id,))
            assert row is not None
            return str(row["id"])
