"""identity.classroom.deleted.v1: when a classroom is deleted, its chat threads and their messages are removed.
Messages that don't match the contract are dead-lettered; learning_transport.kafka runs the consumer."""

import threading
from typing import Any, Literal
from uuid import UUID

from learning_transport import Contract
from learning_transport.kafka import Envelope, consume, parse
from psycopg_pool import ConnectionPool
from pydantic import Field, model_validator

TOPIC = "identity.classroom.deleted.v1"
GROUP = "question-chatbot-agent-v1"


class ClassroomDeleted(Contract):
    classroom_id: UUID
    org_id: str = Field(min_length=1, max_length=200)


class ClassroomDeletedEvent(Envelope):
    event_type: Literal["identity.classroom.deleted.v1"]
    data: ClassroomDeleted

    @model_validator(mode="after")
    def deleted(self) -> "ClassroomDeletedEvent":
        if self.resource_id != self.data.classroom_id:
            raise ValueError("Not this classroom's deletion")
        return self


def purge(pool: ConnectionPool[Any], value: bytes | None) -> bool:
    """Delete the classroom's threads (their messages go with them). False when the message is not a valid
    deletion. Deleting what is already gone changes nothing, so a repeated event is harmless."""
    event = parse(ClassroomDeletedEvent, value)
    if event is None:
        return False
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM threads WHERE classroom_id=%s AND org_id=%s",
            (event.data.classroom_id, event.data.org_id),
        )
    return True


def run(pool: ConnectionPool[Any], stop: threading.Event, connected: threading.Event) -> None:
    consume(GROUP, [TOPIC], lambda _, value: purge(pool, value), stop, connected)
