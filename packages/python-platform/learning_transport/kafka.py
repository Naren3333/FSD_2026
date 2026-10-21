"""commit only processed or durably dead-lettered messages; restart failed sessions with backoff."""

import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from confluent_kafka import Consumer, KafkaException, Message, Producer
from psycopg import DataError, IntegrityError
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from learning_transport.errors import Contract

RESTART_FIRST, RESTART_MAX = 5.0, 60.0  # seconds a stopped consumer waits before its next session
PROCESS_ATTEMPTS = 5  # tries to process a message before the session stops uncommitted
DEAD_LETTER_TIMEOUT = 12  # seconds to wait for the broker to confirm a dead-lettered message
# database errors that retrying the same message cannot fix: a value the schema refuses (dataerror) or a
# broken constraint (integrityerror, checkviolation among them).
PERMANENT_ERRORS: tuple[type[Exception], ...] = (DataError, IntegrityError)

# handle(topic, value) processes one message durably.
Handler = Callable[[str, bytes | None], bool]
# what became of one message: processed, not matching its contract, failing permanently, or failing for now.
Outcome = Literal["processed", "invalid", "poison", "failed"]

log = logging.getLogger("learning_transport.kafka")


class Envelope(Contract):
    """the fields every business event carries (contracts/events)."""

    event_id: UUID
    event_type: str
    schema_version: int = Field(ge=1, le=1)
    timestamp: datetime
    correlation_id: UUID
    resource_id: UUID

    @field_validator("timestamp")
    @classmethod
    def zoned(cls, timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            raise ValueError("Timestamp must have a timezone")
        return timestamp


# identity's events that every service keeping data for a classroom or a user consumes to purge it
# (packages/platform/classrooms.go has the go side).
CLASSROOM_DELETED = "identity.classroom.deleted.v1"
USER_DELETED = "identity.user.deleted.v1"


class ClassroomDeleted(Contract):
    classroom_id: UUID
    org_id: str = Field(min_length=1)


class ClassroomDeletedEvent(Envelope):
    """a deleted classroom: every service that keeps data for it purges it."""

    event_type: Literal["identity.classroom.deleted.v1"]
    data: ClassroomDeleted

    @model_validator(mode="after")
    def deleted(self) -> "ClassroomDeletedEvent":
        if self.resource_id != self.data.classroom_id:
            raise ValueError("Not this classroom's deletion")
        return self


class UserDeleted(Contract):
    user_id: UUID
    org_id: str = Field(min_length=1)
    role: Literal["teacher", "student"]


class UserDeletedEvent(Envelope):
    """a user who deleted their own account (adr 0011): every service that keeps data for them purges it."""

    event_type: Literal["identity.user.deleted.v1"]
    data: UserDeleted

    @model_validator(mode="after")
    def deleted(self) -> "UserDeletedEvent":
        if self.resource_id != self.data.user_id:
            raise ValueError("Not this user's deletion")
        return self


def parse[M: BaseModel](model: type[M], value: bytes | None) -> M | None:
    """a message's value validated against its contract, or none when it does not match: the handler then returns false, so the message is dead-lettered rather than retried."""
    try:
        return model.model_validate_json(value or b"")
    except (ValidationError, ValueError):
        return None


def restart_wait(previous: float | None, progressed: bool) -> float:
    """how long to wait before the next session: the first wait after a session that made progress, otherwise double the previous wait, up to a minute."""
    if progressed or previous is None:
        return RESTART_FIRST
    return min(2 * previous, RESTART_MAX)


def consume(
    group: str, topics: list[str], handle: Handler, stop: threading.Event, connected: threading.Event
) -> None:
    """keep consuming until shutdown."""
    wait: float | None = None
    while not stop.is_set():
        progressed = consume_session(group, topics, handle, stop, connected)
        if stop.is_set():
            return
        wait = restart_wait(wait, progressed)
        log.error("consumer_restarting", extra={"after_seconds": wait})
        if stop.wait(wait):
            return


def dead_letter(producer: Producer, msg: Message, reason: str = "invalid_event") -> None:
    """send a message that cannot be processed to its topic's dead-letter topic, raising unless the broker confirmed it."""
    delivery_errors: list[Any] = []

    def delivered(error: Any, _: Any) -> None:
        if error:
            delivery_errors.append(error)

    producer.produce(msg.topic() + ".dlq", key=msg.key(), value=msg.value(), on_delivery=delivered)
    if producer.flush(DEAD_LETTER_TIMEOUT) or delivery_errors:
        raise RuntimeError("Dead-letter delivery failed")
    log.error(reason, extra={"topic": msg.topic(), "dead_lettered": True})


def attempt(handle: Handler, topic: str, value: bytes | None, stop: threading.Event) -> Outcome:
    """retry processing with backoff; dead-letter permanent failures and retain transient failures for replay."""
    permanent = True
    for n in range(PROCESS_ATTEMPTS):
        try:
            return "processed" if handle(topic, value) else "invalid"
        except Exception as error:
            permanent = permanent and isinstance(error, PERMANENT_ERRORS)
            log.error(
                "consumer_retry", extra={"topic": topic, "attempt": n + 1, "error": type(error).__name__}
            )
            if n == PROCESS_ATTEMPTS - 1:
                return "poison" if permanent else "failed"
            if stop.wait(2**n):
                return "failed"
    return "failed"


def consume_session(
    group: str, topics: list[str], handle: Handler, stop: threading.Event, connected: threading.Event
) -> bool:
    """one consumer session; returns whether it committed anything."""
    progressed = False
    consumer = Consumer(
        {
            "bootstrap.servers": os.environ["KAFKA_BROKERS"],
            "group.id": group,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
        }
    )
    producer = Producer(
        {"bootstrap.servers": os.environ["KAFKA_BROKERS"], "acks": "all", "message.timeout.ms": 10000}
    )
    consumer.subscribe(topics)
    try:
        consumer.list_topics(timeout=10)
        connected.set()
        while not stop.is_set():
            msg = consumer.poll(1)
            if msg is None:
                continue
            if msg.error():
                raise KafkaException(msg.error())
            outcome = attempt(handle, msg.topic(), msg.value(), stop)
            if outcome == "failed":
                return progressed
            if outcome == "invalid":
                dead_letter(producer, msg)
            elif outcome == "poison":
                dead_letter(producer, msg, "unprocessable_event")
            # committed only once processed or confirmed in the dead-letter topic.
            consumer.commit(message=msg, asynchronous=False)
            progressed = True
    except Exception:
        log.error("consumer_stopped_uncommitted")
    finally:
        connected.clear()
        consumer.close()
    return progressed
