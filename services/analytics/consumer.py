"""The grading.finalized.v1 consumer: stores each finalized evaluation's evidence once (inbox), dead-letters
messages that don't match the contract, and restarts itself with a backoff when a session stops."""

import logging
import os
import threading
from typing import Any

from confluent_kafka import Consumer, KafkaException, Message, Producer
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from domain import Event

TOPIC = "grading.finalized.v1"
DEAD_LETTER_TOPIC = TOPIC + ".dlq"
RESTART_FIRST, RESTART_MAX = 5.0, 60.0  # seconds a stopped consumer waits before its next session
STORE_ATTEMPTS = 5  # tries to store an event before the session stops uncommitted
DEAD_LETTER_TIMEOUT = 12  # seconds to wait for the broker to confirm a dead-lettered message

log = logging.getLogger("analytics.consumer")


def process(pool: ConnectionPool[Any], event: Event) -> None:
    """Store the evaluation's evidence, unless this event or this evaluation was already stored."""
    with pool.connection() as conn, conn.transaction():
        inserted = conn.execute(
            "INSERT INTO inbox(event_id) VALUES(%s) ON CONFLICT DO NOTHING RETURNING event_id",
            (event.event_id,),
        ).fetchone()
        if inserted is None:
            return
        data = event.data
        inserted = conn.execute(
            "INSERT INTO evaluations(evaluation_id,event_id) VALUES(%s,%s)"
            " ON CONFLICT(evaluation_id) DO NOTHING RETURNING evaluation_id",
            (data.evaluation_id, event.event_id),
        ).fetchone()
        if inserted is None:
            return
        for e in data.evidence:
            conn.execute(
                "INSERT INTO evidence(evaluation_id,question_id,assessment_id,classroom_id,org_id,student_id,"
                "skill_id,correct,awarded,possible) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    data.evaluation_id,
                    e.question_id,
                    data.assessment_id,
                    data.classroom_id,
                    data.org_id,
                    data.student_id,
                    e.skill_id,
                    e.correct,
                    e.awarded,
                    e.possible,
                ),
            )


def restart_wait(previous: float | None, progressed: bool) -> float:
    """How long to wait before the next session: the first wait after a session that made progress, otherwise
    double the previous wait, up to a minute."""
    if progressed or previous is None:
        return RESTART_FIRST
    return min(2 * previous, RESTART_MAX)


def consume(pool: ConnectionPool[Any], stop: threading.Event, connected: threading.Event) -> None:
    """Keep consuming until shutdown. A session that stops on an event it cannot store (the database is down, say)
    or on a broker error is followed, after a backoff, by a new one from the last committed offset: the event is
    retried rather than skipped, and the consumer never stays down while the process looks alive."""
    wait: float | None = None
    while not stop.is_set():
        progressed = consume_session(pool, stop, connected)
        if stop.is_set():
            return
        wait = restart_wait(wait, progressed)
        log.error("consumer_restarting", extra={"after_seconds": wait})
        if stop.wait(wait):
            return


def parse_event(value: bytes | None) -> Event | None:
    """The event, or None when the message does not match the contract."""
    try:
        return Event.model_validate_json(value or b"")
    except (ValidationError, ValueError):
        return None


def dead_letter(producer: Producer, msg: Message) -> None:
    """Send an unreadable message to the dead-letter topic, raising unless the broker confirmed it."""
    delivery_errors: list[Any] = []

    def delivered(error: Any, _: Any) -> None:
        if error:
            delivery_errors.append(error)

    producer.produce(DEAD_LETTER_TOPIC, key=msg.key(), value=msg.value(), on_delivery=delivered)
    if producer.flush(DEAD_LETTER_TIMEOUT) or delivery_errors:
        raise RuntimeError("Dead-letter delivery failed")
    log.error("invalid_event", extra={"dead_lettered": True})


def store(pool: ConnectionPool[Any], event: Event, stop: threading.Event) -> bool:
    """Store the event, retrying with a doubling wait. False when every attempt failed or shutdown began."""
    for attempt in range(STORE_ATTEMPTS):
        try:
            process(pool, event)
            return True
        except Exception:
            log.error(
                "consumer_retry",
                extra={
                    "event_id": str(event.event_id),
                    "correlation_id": str(event.correlation_id),
                    "attempt": attempt + 1,
                },
            )
            if attempt == STORE_ATTEMPTS - 1 or stop.wait(2**attempt):
                return False
    return False


def consume_session(pool: ConnectionPool[Any], stop: threading.Event, connected: threading.Event) -> bool:
    """One consumer session; returns whether it committed anything."""
    progressed = False
    consumer = Consumer(
        {
            "bootstrap.servers": os.environ["KAFKA_BROKERS"],
            "group.id": os.getenv("KAFKA_GROUP", "analytics-v1"),
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
        }
    )
    producer = Producer(
        {"bootstrap.servers": os.environ["KAFKA_BROKERS"], "acks": "all", "message.timeout.ms": 10000}
    )
    consumer.subscribe([TOPIC])
    try:
        consumer.list_topics(timeout=10)
        connected.set()
        while not stop.is_set():
            msg = consumer.poll(1)
            if msg is None:
                continue
            if msg.error():
                raise KafkaException(msg.error())
            event = parse_event(msg.value())
            if event is None:
                dead_letter(producer, msg)
            elif not store(pool, event, stop):
                return progressed
            consumer.commit(message=msg, asynchronous=False)
            progressed = True
    except Exception:
        log.error("consumer_stopped_uncommitted")
    finally:
        connected.clear()
        consumer.close()
    return progressed
