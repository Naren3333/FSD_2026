"""purge deleted classrooms and users; redact citations when their documents are deleted."""

import threading
from typing import Any, Literal
from uuid import UUID

from learning_transport import Contract
from learning_transport.db import LOCK_CLASSROOM, LOCK_USER, lock
from learning_transport.kafka import (
    CLASSROOM_DELETED,
    USER_DELETED,
    ClassroomDeletedEvent,
    Envelope,
    UserDeletedEvent,
    consume,
    parse,
)
from psycopg_pool import ConnectionPool
from pydantic import Field, model_validator

from store import LOCK_DOCUMENT

DOCUMENT_DELETED = "content.deleted.v1"
GROUP = "question-chatbot-agent-v1"
# blanks every citation of the document in one message's citations (%s: the document id), keeping their
# order.
REDACT = """(SELECT jsonb_agg(CASE WHEN c->>'document_id' = %s
    THEN c || '{"content": "", "image": null, "removed": true}'::jsonb ELSE c END ORDER BY n)
    FROM jsonb_array_elements(citations) WITH ORDINALITY AS e(c, n))"""


class DocumentDeleted(Contract):
    document_id: UUID
    classroom_id: UUID
    org_id: str = Field(min_length=1)


class DocumentDeletedEvent(Envelope):
    event_type: Literal["content.deleted.v1"]
    data: DocumentDeleted

    @model_validator(mode="after")
    def deleted(self) -> "DocumentDeletedEvent":
        if self.resource_id != self.data.document_id:
            raise ValueError("Not this document's deletion")
        return self


def purge(pool: ConnectionPool[Any], value: bytes | None) -> bool:
    """delete the classroom's threads (their messages go with them) and remember the classroom, so no thread is created in it afterwards (store.create_thread takes the same lock)."""
    event = parse(ClassroomDeletedEvent, value)
    if event is None:
        return False
    data = event.data
    with pool.connection() as conn, conn.transaction():
        lock(conn, LOCK_CLASSROOM, data.classroom_id)
        conn.execute(
            "INSERT INTO purged_classrooms(classroom_id,org_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (data.classroom_id, data.org_id),
        )
        conn.execute(
            "DELETE FROM threads WHERE classroom_id=%s AND org_id=%s", (data.classroom_id, data.org_id)
        )
    return True


def redact(pool: ConnectionPool[Any], value: bytes | None) -> bool:
    """redact deleted-source citations and remember deletion for answers still being saved."""
    event = parse(DocumentDeletedEvent, value)
    if event is None:
        return False
    data = event.data
    document = str(data.document_id)
    with pool.connection() as conn, conn.transaction():
        lock(conn, LOCK_DOCUMENT, document)
        conn.execute(
            "INSERT INTO deleted_documents(document_id,classroom_id,org_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
            (data.document_id, data.classroom_id, data.org_id),
        )
        conn.execute(
            f"UPDATE messages m SET citations = {REDACT} FROM threads t"
            " WHERE t.id = m.thread_id AND t.org_id = %s AND t.classroom_id = %s"
            " AND m.citations @> jsonb_build_array(jsonb_build_object('document_id', %s::text))",
            (document, data.org_id, data.classroom_id, document),
        )
    return True


def purge_user(pool: ConnectionPool[Any], value: bytes | None) -> bool:
    """delete the threads (and so the messages) of a user who deleted their account and remember them, so no thread is created for them afterwards (store.create_thread takes the same lock)."""
    event = parse(UserDeletedEvent, value)
    if event is None:
        return False
    data = event.data
    user = str(data.user_id)
    with pool.connection() as conn, conn.transaction():
        lock(conn, LOCK_USER, user)
        conn.execute(
            "INSERT INTO deleted_users(user_id,org_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (user, data.org_id),
        )
        conn.execute("DELETE FROM threads WHERE user_id=%s AND org_id=%s", (user, data.org_id))
    return True


def handle(pool: ConnectionPool[Any], topic: str, value: bytes | None) -> bool:
    if topic == DOCUMENT_DELETED:
        return redact(pool, value)
    if topic == USER_DELETED:
        return purge_user(pool, value)
    return purge(pool, value)


def run(pool: ConnectionPool[Any], stop: threading.Event, connected: threading.Event) -> None:
    consume(
        GROUP,
        [CLASSROOM_DELETED, DOCUMENT_DELETED, USER_DELETED],
        lambda topic, value: handle(pool, topic, value),
        stop,
        connected,
    )
