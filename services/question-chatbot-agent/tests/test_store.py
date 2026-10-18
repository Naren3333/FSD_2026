"""PostgresStore against a real PostgreSQL. Needs CHATBOT_TEST_DATABASE_URL, a scratch database (the tests add
rows under unique users and leave them); skipped without it. See docs/development/operations.md."""

import json
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from learning_transport.db import open_pool

from purge import purge_user, redact
from store import MIGRATIONS, PostgresStore

pytestmark = pytest.mark.anyio
URL = os.environ.get("CHATBOT_TEST_DATABASE_URL")
ROOM = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def store() -> AsyncIterator[PostgresStore]:
    if not URL:
        pytest.skip("CHATBOT_TEST_DATABASE_URL is not set")
    store = await PostgresStore.open(URL)
    await (await PostgresStore.open(URL)).close()  # the migration is idempotent
    try:
        yield store
    finally:
        await store.close()


async def test_threads_belong_to_their_user_and_organization(store: PostgresStore) -> None:
    user = str(uuid4())
    thread = await store.create_thread("org", user, "student", ROOM)
    assert (thread["role"], str(thread["classroom_id"]), thread["title"]) == ("student", ROOM, None)
    tid = str(thread["id"])
    assert await store.thread("org", user, tid) is not None
    assert await store.thread("other-org", user, tid) is None
    assert await store.thread("org", "someone-else", tid) is None
    assert [str(t["id"]) for t in await store.list_threads("org", user, None)] == [tid]
    assert await store.list_threads("org", user, str(uuid4())) == []


async def test_turns_keep_history_title_and_answers(store: PostgresStore) -> None:
    user = str(uuid4())
    tid = str((await store.create_thread("org", user, "teacher", ROOM))["id"])
    assert await store.start_turn(tid, "What is photosynthesis? " * 10, 6) == []
    chunk = str(uuid4())
    citation = {"chunk_id": chunk, "document_id": str(uuid4()), "title": "Bio", "page_start": 1, "page_end": 1,
                "kind": "text", "source": "pdf", "content": "Light.", "image": None}  # fmt: skip
    answer_id = await store.save_answer(tid, {
        "content": f"Light [doc:{chunk}].", "outcome": "answered", "rewritten_query": "photosynthesis",
        "is_on_topic": True, "retrieved_chunk_ids": [chunk], "citations": [citation],
        "prompt_version": "teacher.v1", "model": "gpt-test", "usage_tokens": 42, "latency_ms": 900,
    })  # fmt: skip
    history = await store.start_turn(tid, "and then?", 6)
    assert [h["role"] for h in history] == ["user", "assistant"]
    messages = await store.messages(tid, 200)
    assert [(m["role"], m["outcome"]) for m in messages] == [
        ("user", None),
        ("assistant", "answered"),
        ("user", None),
    ]
    assert str(messages[1]["id"]) == answer_id and messages[1]["citations"] == [citation]
    thread = await store.thread("org", user, tid)
    assert thread is not None and thread["title"] == ("What is photosynthesis? " * 10)[:80]


async def test_an_error_answer_without_retrieval_is_saved(store: PostgresStore) -> None:
    tid = str((await store.create_thread("org", str(uuid4()), "student", ROOM))["id"])
    await store.start_turn(tid, "q", 6)
    await store.save_answer(tid, {"content": "", "outcome": "error"})
    assert [m["outcome"] for m in await store.messages(tid, 200)] == [None, "error"]


async def test_latest_messages_oldest_first(store: PostgresStore) -> None:
    tid = str((await store.create_thread("org", str(uuid4()), "student", ROOM))["id"])
    for n in range(3):
        await store.start_turn(tid, f"q{n}", 6)
    assert [m["content"] for m in await store.messages(tid, 2)] == ["q1", "q2"]


async def test_no_thread_in_a_purged_classroom(store: PostgresStore) -> None:
    room = str(uuid4())
    assert await store.create_thread("org", str(uuid4()), "student", room) is not None
    async with store.pool.connection() as conn:
        await conn.execute("INSERT INTO purged_classrooms(classroom_id, org_id) VALUES (%s, 'org')", (room,))
    assert await store.create_thread("org", str(uuid4()), "student", room) is None


def deletion(document: str, room: str) -> bytes:
    return json.dumps({
        "event_id": str(uuid4()), "event_type": "content.deleted.v1", "schema_version": 1,
        "timestamp": "2026-10-18T00:00:00Z", "correlation_id": str(uuid4()), "resource_id": document,
        "data": {"document_id": document, "classroom_id": room, "org_id": "org"},
    }).encode()  # fmt: skip


async def test_a_deleted_documents_passages_leave_past_and_later_answers(store: PostgresStore) -> None:
    assert URL is not None
    room = str(uuid4())
    tid = str((await store.create_thread("org", str(uuid4()), "student", room))["id"])
    deleted, kept = str(uuid4()), str(uuid4())

    def cite(document: str) -> dict[str, Any]:
        return {"chunk_id": str(uuid4()), "document_id": document, "title": "Bio", "page_start": 1, "page_end": 1,
                "kind": "figure", "source": "vision", "content": "Light.", "image": "0123456789abcdef.png"}  # fmt: skip

    async def answer(*documents: str) -> None:
        await store.start_turn(tid, "q", 6)
        await store.save_answer(
            tid, {"content": "A.", "outcome": "answered", "citations": [cite(d) for d in documents]}
        )

    await answer(deleted, kept)
    pool = open_pool(URL, MIGRATIONS)
    try:
        for _ in range(2):  # a repeated event changes nothing more
            assert redact(pool, deletion(deleted, room)) is True
    finally:
        pool.close()
    await answer(deleted)  # written while the document was being deleted, saved after
    citations = [m["citations"] for m in await store.messages(tid, 200) if m["role"] == "assistant"]
    removed = {"content": "", "image": None, "removed": True}
    first, later = citations
    assert {k: first[0][k] for k in removed} == removed and first[0]["title"] == "Bio"  # what was cited stays
    assert first[1]["content"] == "Light." and not first[1].get("removed")  # another document is untouched
    assert {k: later[0][k] for k in removed} == removed


async def test_a_deleted_accounts_threads_go_and_none_is_created_for_it(store: PostgresStore) -> None:
    assert URL is not None
    gone, kept = str(uuid4()), str(uuid4())
    for user in (gone, kept):
        tid = str((await store.create_thread("org", user, "student", ROOM))["id"])
        await store.start_turn(tid, "q", 6)
    deletion = json.dumps({
        "event_id": str(uuid4()), "event_type": "identity.user.deleted.v1", "schema_version": 1,
        "timestamp": "2026-10-18T00:00:00Z", "correlation_id": str(uuid4()), "resource_id": gone,
        "data": {"user_id": gone, "org_id": "org", "role": "student"},
    }).encode()  # fmt: skip
    pool = open_pool(URL, MIGRATIONS)
    try:
        for _ in range(2):  # a repeated event changes nothing more
            assert purge_user(pool, deletion) is True
    finally:
        pool.close()
    assert await store.list_threads("org", gone, None) == []
    assert len(await store.list_threads("org", kept, None)) == 1
    assert await store.create_thread("org", gone, "student", ROOM) is None
