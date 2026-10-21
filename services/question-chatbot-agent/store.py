"""the chatbot's own database: threads and messages (adr 0002)."""

import json
from pathlib import Path
from typing import Any, Protocol

from learning_transport.db import LOCK_CLASSROOM, LOCK_USER, AsyncPooled, lock_async
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

MIGRATIONS = Path(__file__).parent / "migrations"
# a deleted document's redaction (purge.redact) against an answer citing it being saved.
LOCK_DOCUMENT = "document"
TITLE_CHARS = 80
THREAD_COLUMNS = "id, classroom_id, role, title, created_at, last_message_at"
MESSAGE_COLUMNS = "id, role, content, outcome, citations, created_at"
# an answer still marked in progress after this was lost (its process died before saving it), so a new one
# may start.
TURN_STALE = "3 minutes"


class Store(Protocol):
    async def create_thread(self, org: str, user: str, role: str, classroom_id: str) -> dict[str, Any] | None:
        """the new thread, or none when the classroom was deleted and purged."""
        ...

    async def list_threads(self, org: str, user: str, classroom_id: str | None) -> list[dict[str, Any]]: ...

    async def thread(self, org: str, user: str, thread_id: str) -> dict[str, Any] | None: ...

    async def messages(self, thread_id: str, limit: int) -> list[dict[str, Any]]:
        """the thread's latest `limit` messages, oldest first."""
        ...

    async def claim_turn(self, thread_id: str) -> bool:
        """marks an answer in progress for the thread's user, until save_answer saves it."""
        ...

    async def start_turn(
        self, thread_id: str, content: str, history_turns: int
    ) -> list[dict[str, str]] | None:
        """the last `history_turns` messages (oldest first), then saves the user's message and, for the first, the thread's title."""
        ...

    async def save_answer(self, thread_id: str, answer: dict[str, Any]) -> str | None:
        """the saved answer's id; none, saving nothing, when the thread is gone."""
        ...


class PostgresStore(AsyncPooled):
    MIGRATIONS = MIGRATIONS

    async def create_thread(self, org: str, user: str, role: str, classroom_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as conn, conn.transaction():
            # the classroom's purge and a deleted account's take the same locks, so a thread is never
            # created in a classroom, or for a user, whose purge is committing.
            await lock_async(conn, LOCK_CLASSROOM, classroom_id)
            await lock_async(conn, LOCK_USER, user)
            cur = await conn.execute(
                f"INSERT INTO threads (org_id, user_id, role, classroom_id) SELECT %s,%s,%s,%s"
                f" WHERE NOT EXISTS (SELECT 1 FROM purged_classrooms WHERE classroom_id=%s)"
                f" AND NOT EXISTS (SELECT 1 FROM deleted_users WHERE user_id=%s)"
                f" RETURNING {THREAD_COLUMNS}",
                (org, user, role, classroom_id, classroom_id, user),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

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

    async def messages(self, thread_id: str, limit: int) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE thread_id=%s ORDER BY created_at DESC, id DESC LIMIT %s",
                (thread_id, limit),
            )
            return [dict(r) for r in await cur.fetchall()][::-1]

    async def claim_turn(self, thread_id: str) -> bool:
        async with self.pool.connection() as conn, conn.transaction():
            if not await _hold_thread(conn, thread_id):
                return True
            # a concurrent claim for the same user waits on the primary key, then sees this one and is
            # refused.
            cur = await conn.execute(
                "INSERT INTO active_turns (user_id, thread_id) SELECT user_id, id FROM threads WHERE id=%s"
                " ON CONFLICT (user_id) DO UPDATE SET thread_id=excluded.thread_id, started_at=now()"
                f" WHERE active_turns.started_at < now() - interval '{TURN_STALE}' RETURNING 1",
                (thread_id,),
            )
            return await cur.fetchone() is not None

    async def start_turn(
        self, thread_id: str, content: str, history_turns: int
    ) -> list[dict[str, str]] | None:
        async with self.pool.connection() as conn, conn.transaction():
            if not await _hold_thread(conn, thread_id):
                return None
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

    async def save_answer(self, thread_id: str, answer: dict[str, Any]) -> str | None:
        citations = answer.get("citations")
        async with self.pool.connection() as conn, conn.transaction():
            if not await _hold_thread(conn, thread_id):
                return None
            if citations:
                # a document deleted while the answer was written is cited as removed (purge.redact).
                documents = sorted({str(c["document_id"]) for c in citations})
                for document in documents:
                    await lock_async(conn, LOCK_DOCUMENT, document)
                cur = await conn.execute(
                    "SELECT document_id::text AS id FROM deleted_documents WHERE document_id = ANY(%s::uuid[])",
                    (documents,),
                )
                deleted = {row["id"] for row in await cur.fetchall()}
                citations = [
                    {**c, "content": "", "image": None, "removed": True}
                    if str(c["document_id"]) in deleted
                    else c
                    for c in citations
                ]
            cur = await conn.execute(
                "INSERT INTO messages (thread_id, role, content, outcome, rewritten_query, sub_queries, is_on_topic,"
                " retrieved_chunk_ids, citations, prompt_version, model, usage_tokens, latency_ms)"
                " VALUES (%s, 'assistant', %s, %s, %s, %s, %s, %s::uuid[], %s, %s, %s, %s, %s) RETURNING id",
                (
                    thread_id,
                    answer["content"],
                    answer["outcome"],
                    answer.get("rewritten_query"),
                    answer.get("sub_queries"),
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
            # in the answer's own transaction, so the user's next question is accepted once this one is
            # saved.
            await conn.execute("DELETE FROM active_turns WHERE thread_id=%s", (thread_id,))
            assert row is not None
            return str(row["id"])


async def _hold_thread(conn: AsyncConnection[Any], thread_id: str) -> bool:
    """whether the thread still exists, holding it until the transaction ends."""
    cur = await conn.execute("SELECT 1 FROM threads WHERE id=%s FOR KEY SHARE", (thread_id,))
    return await cur.fetchone() is not None
