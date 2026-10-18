import json
import threading
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from learning_transport import kafka


class FakeMessage:
    def __init__(self, value: bytes) -> None:
        self._value = value

    def topic(self) -> str:
        return "test.happened"

    def key(self) -> bytes:
        return b"r1"

    def value(self) -> bytes:
        return self._value

    def error(self) -> None:
        return None


class Stop(threading.Event):
    """A shutdown flag whose retry waits return at once, recording how long they would have been."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout or 0)
        return self.is_set()


class Broker:
    """Fake confluent_kafka Consumer and Producer sharing one log of what happened, in order."""

    def __init__(self, monkeypatch: Any, stop: Stop, messages: list[FakeMessage], dlq: str) -> None:
        self.log: list[str] = []
        broker = self

        class Consumer:
            def __init__(self, _: dict[str, Any]) -> None:
                self.queue = list(messages)

            def subscribe(self, _: list[str]) -> None: ...

            def list_topics(self, timeout: float) -> None: ...

            def poll(self, _: float) -> FakeMessage | None:
                if not self.queue:
                    stop.set()
                    return None
                return self.queue.pop(0)

            def commit(self, message: FakeMessage, asynchronous: bool) -> None:
                broker.log.append("commit " + message.value().decode())

            def close(self) -> None: ...

        class Producer:
            def __init__(self, _: dict[str, Any]) -> None:
                self.pending: list[Callable[[Any, Any], None]] = []

            def produce(
                self, topic: str, key: bytes, value: bytes, on_delivery: Callable[[Any, Any], None]
            ) -> None:
                broker.log.append(f"produce {topic} {value.decode()}")
                self.pending.append(on_delivery)

            def flush(self, _: float) -> int:
                for delivered in self.pending:
                    delivered("broker said no" if dlq == "refused" else None, None)
                return 1 if dlq == "timeout" else 0

        monkeypatch.setenv("KAFKA_BROKERS", "kafka.test:9092")
        monkeypatch.setattr(kafka, "Consumer", Consumer)
        monkeypatch.setattr(kafka, "Producer", Producer)


def session(
    monkeypatch: Any, handle: kafka.Handler, values: list[bytes], dlq: str = "ok"
) -> tuple[bool, Broker, Stop]:
    stop = Stop()
    broker = Broker(monkeypatch, stop, [FakeMessage(v) for v in values], dlq)
    progressed = kafka.consume_session("group", ["test.happened"], handle, stop, threading.Event())
    return progressed, broker, stop


def test_an_invalid_event_is_committed_only_after_it_is_dead_lettered(monkeypatch: Any) -> None:
    progressed, broker, _ = session(monkeypatch, lambda _, value: value != b"bad", [b"bad", b"good"])
    assert progressed
    assert broker.log == ["produce test.happened.dlq bad", "commit bad", "commit good"]


@pytest.mark.parametrize("dlq", ["timeout", "refused"])
def test_nothing_is_committed_when_dead_lettering_fails(monkeypatch: Any, dlq: str) -> None:
    progressed, broker, _ = session(monkeypatch, lambda _, value: value != b"bad", [b"bad", b"good"], dlq)
    assert not progressed
    assert broker.log == ["produce test.happened.dlq bad"]


def test_a_failing_handler_is_retried_and_its_message_left_uncommitted(monkeypatch: Any) -> None:
    calls: list[bytes | None] = []

    def handle(_: str, value: bytes | None) -> bool:
        calls.append(value)
        raise ConnectionError("database down")

    progressed, broker, stop = session(monkeypatch, handle, [b"first", b"second"])
    assert not progressed
    assert calls == [b"first"] * kafka.PROCESS_ATTEMPTS
    assert stop.waits == [1, 2, 4, 8]
    assert broker.log == []


def test_a_handler_that_recovers_is_committed_once(monkeypatch: Any) -> None:
    failures = [ConnectionError("database down")] * 2

    def handle(_: str, value: bytes | None) -> bool:
        if failures:
            raise failures.pop()
        return True

    progressed, broker, _ = session(monkeypatch, handle, [b"first"])
    assert progressed
    assert broker.log == ["commit first"]


def identity_event(event_type: str, resource: str, data: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "event_id": str(uuid4()),
            "event_type": event_type,
            "schema_version": 1,
            "timestamp": "2026-10-14T00:00:00Z",
            "correlation_id": str(uuid4()),
            "resource_id": resource,
            "data": data,
        }
    ).encode()


def test_identity_deletions_parse_or_are_dead_lettered() -> None:
    room, user = str(uuid4()), str(uuid4())
    classroom = {"classroom_id": room, "org_id": "o1"}
    deleted = kafka.parse(
        kafka.ClassroomDeletedEvent, identity_event(kafka.CLASSROOM_DELETED, room, classroom)
    )
    assert deleted is not None and str(deleted.data.classroom_id) == room
    assert (
        kafka.parse(kafka.ClassroomDeletedEvent, identity_event(kafka.CLASSROOM_DELETED, user, classroom))
        is None
    )
    assert (
        kafka.parse(kafka.ClassroomDeletedEvent, identity_event(kafka.USER_DELETED, room, classroom)) is None
    )
    account = {"user_id": user, "org_id": "o1", "role": "student"}
    gone = kafka.parse(kafka.UserDeletedEvent, identity_event(kafka.USER_DELETED, user, account))
    assert gone is not None and gone.data.role == "student"
    assert kafka.parse(kafka.UserDeletedEvent, identity_event(kafka.USER_DELETED, room, account)) is None
    admin = {**account, "role": "admin"}
    assert kafka.parse(kafka.UserDeletedEvent, identity_event(kafka.USER_DELETED, user, admin)) is None
