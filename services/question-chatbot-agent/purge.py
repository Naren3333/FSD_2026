"""identity.classroom.deleted.v1: when a classroom is deleted, its chat threads and their messages are removed.
Messages that don't match the contract are dead-lettered; learning_transport.kafka runs the consumer."""

import threading
from typing import Any, Literal
from uuid import UUID

from learning_transport import Contract
from learning_transport.kafka import Envelope, consume, parse
from psycopg_pool import ConnectionPool
from pydantic import Field, model_validator

from store import CLASSROOM_LOCK

TOPIC = "identity.classroom.deleted.v1"
GROUP = "question-chatbot-agent-v1"


class ClassroomDeleted(Contract):
    classroom_id: UUID
    org_id: str = Field(min_length=1)


class ClassroomDeletedEvent(Envelope):
    event_type: Literal["identity.classroom.deleted.v1"]
    data: ClassroomDeleted

    @model_validator(mode="after")
    def deleted(self) -> "ClassroomDeletedEvent":
        if self.resource_id != self.data.classroom_id:
            raise ValueError("Not this classroom's deletion")
        return self


def purge(pool: ConnectionPool[Any], value: bytes | None) -> bool:
    """Delete the classroom's threads (their messages go with them) and remember the classroom, so no thread is
    created in it afterwards (store.create_thread takes the same lock). False when the message is not a valid
    deletion. Deleting what is already gone changes nothing, so a repeated event is harmless."""
    event = parse(ClassroomDeletedEvent, value)
    if event is None:
        return False
    data = event.data
    with pool.connection() as conn, conn.transaction():
        conn.execute(CLASSROOM_LOCK, (data.classroom_id,))
        conn.execute(
            "INSERT INTO purged_classrooms(classroom_id,org_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (data.classroom_id, data.org_id),
        )
        conn.execute(
            "DELETE FROM threads WHERE classroom_id=%s AND org_id=%s", (data.classroom_id, data.org_id)
        )
    return True


def run(pool: ConnectionPool[Any], stop: threading.Event, connected: threading.Event) -> None:
    consume(GROUP, [TOPIC], lambda _, value: purge(pool, value), stop, connected)
