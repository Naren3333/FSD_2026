import json
from uuid import uuid4

from purge import purge


def test_malformed_classroom_deletions_are_dead_lettered() -> None:
    deletion = {
        "event_id": str(uuid4()),
        "event_type": "identity.classroom.deleted.v1",
        "schema_version": 1,
        "timestamp": "2026-10-14T00:00:00Z",
        "correlation_id": str(uuid4()),
        "resource_id": str(uuid4()),  # not the deleted classroom
        "data": {"classroom_id": str(uuid4()), "org_id": "o"},
    }
    # The pool is never reached: the message is rejected first.
    assert purge(None, json.dumps(deletion).encode()) is False  # type: ignore[arg-type]
    assert purge(None, b"{}") is False  # type: ignore[arg-type]


def test_a_purge_locks_the_classroom_and_leaves_a_tombstone() -> None:
    from store import CLASSROOM_LOCK

    statements: list[str] = []

    class Conn:
        def __enter__(self) -> "Conn":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def connection(self) -> "Conn":
            return self

        def transaction(self) -> "Conn":
            return self

        def execute(self, sql: str, params: object = None) -> None:
            statements.append(sql)

    room = str(uuid4())
    deletion = {
        "event_id": str(uuid4()),
        "event_type": "identity.classroom.deleted.v1",
        "schema_version": 1,
        "timestamp": "2026-10-14T00:00:00Z",
        "correlation_id": str(uuid4()),
        "resource_id": room,
        "data": {"classroom_id": room, "org_id": "o" * 300},  # the event schema sets no length limit
    }
    assert purge(Conn(), json.dumps(deletion).encode()) is True  # type: ignore[arg-type]
    assert statements[0] == CLASSROOM_LOCK
    assert "purged_classrooms" in statements[1] and statements[2].startswith("DELETE FROM threads")
