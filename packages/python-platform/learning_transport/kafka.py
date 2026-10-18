"""Kafka consumption for the Python services: one consumer group reading a few topics, committing each message only
after it was processed or dead-lettered, and restarting itself with a backoff when a session stops. Import this
module only from a service that has confluent-kafka installed."""

import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from confluent_kafka import Consumer, KafkaException, Message, Producer
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from learning_transport.errors import Contract

RESTART_FIRST, RESTART_MAX = 5.0, 60.0  # seconds a stopped consumer waits before its next session
PROCESS_ATTEMPTS = 5  # tries to process a message before the session stops uncommitted
DEAD_LETTER_TIMEOUT = 12  # seconds to wait for the broker to confirm a dead-lettered message

# handle(topic, value) processes one message durably. It returns False when the message does not match the topic's
# contract, which sends it to <topic>.dlq, and raises when it cannot be processed now, which retries it.
Handler = Callable[[str, bytes | None], bool]

log = logging.getLogger("learning_transport.kafka")


class Envelope(Contract):
    """The fields every business event carries (contracts/events). A service's event model extends this with its own
    `data` payload model, and narrows `event_type` and checks `resource_id` against its payload itself."""

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


# Identity's events that every service keeping data for a classroom or a user consumes to purge it
# (packages/platform/classrooms.go has the Go side).
CLASSROOM_DELETED = "identity.classroom.deleted.v1"
USER_DELETED = "identity.user.deleted.v1"


class ClassroomDeleted(Contract):
    classroom_id: UUID
    org_id: str = Field(min_length=1)


class ClassroomDeletedEvent(Envelope):
    """A deleted classroom: every service that keeps data for it purges it."""

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
    """A user who deleted their own account (ADR 0011): every service that keeps data for them purges it."""

    event_type: Literal["identity.user.deleted.v1"]
    data: UserDeleted

    @model_validator(mode="after")
    def deleted(self) -> "UserDeletedEvent":
        if self.resource_id != self.data.user_id:
            raise ValueError("Not this user's deletion")
        return self


def parse[M: BaseModel](model: type[M], value: bytes | None) -> M | None:
    """A message's value validated against its contract, or None when it does not match: the handler then returns
    False, so the message is dead-lettered rather than retried."""
    try:
        return model.model_validate_json(value or b"")
    except (ValidationError, ValueError):
        return None


def restart_wait(previous: float | None, progressed: bool) -> float:
    """How long to wait before the next session: the first wait after a session that made progress, otherwise
    double the previous wait, up to a minute."""
    if progressed or previous is None:
        return RESTART_FIRST
    return min(2 * previous, RESTART_MAX)


def consume(
    group: str, topics: list[str], handle: Handler, stop: threading.Event, connected: threading.Event
) -> None:
    """Keep consuming until shutdown. A session that stops on a message it cannot process (the database is down,
    say) or on a broker error is followed, after a backoff, by a new one from the last committed offset: the
    message is retried rather than skipped, and the consumer never stays down while the process looks alive."""
    wait: float | None = None
    while not stop.is_set():
        progressed = consume_session(group, topics, handle, stop, connected)
        if stop.is_set():
            return
        wait = restart_wait(wait, progressed)
        log.error("consumer_restarting", extra={"after_seconds": wait})
        if stop.wait(wait):
            return


def dead_letter(producer: Producer, msg: Message) -> None:
    """Send an unreadable message to its topic's dead-letter topic, raising unless the broker confirmed it."""
    delivery_errors: list[Any] = []

    def delivered(error: Any, _: Any) -> None:
        if error:
            delivery_errors.append(error)

    producer.produce(msg.topic() + ".dlq", key=msg.key(), value=msg.value(), on_delivery=delivered)
    if producer.flush(DEAD_LETTER_TIMEOUT) or delivery_errors:
        raise RuntimeError("Dead-letter delivery failed")
    log.error("invalid_event", extra={"topic": msg.topic(), "dead_lettered": True})


def attempt(handle: Handler, topic: str, value: bytes | None, stop: threading.Event) -> bool | None:
    """Process one message, retrying with a doubling wait: its validity, or None when every attempt failed or
    shutdown began."""
    for n in range(PROCESS_ATTEMPTS):
        try:
            return handle(topic, value)
        except Exception:
            log.error("consumer_retry", extra={"topic": topic, "attempt": n + 1})
            if n == PROCESS_ATTEMPTS - 1 or stop.wait(2**n):
                return None
    return None


def consume_session(
    group: str, topics: list[str], handle: Handler, stop: threading.Event, connected: threading.Event
) -> bool:
    """One consumer session; returns whether it committed anything."""
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
            valid = attempt(handle, msg.topic(), msg.value(), stop)
            if valid is None:
                return progressed
            if not valid:
                dead_letter(producer, msg)
            consumer.commit(message=msg, asynchronous=False)
            progressed = True
    except Exception:
        log.error("consumer_stopped_uncommitted")
    finally:
        connected.clear()
        consumer.close()
    return progressed
