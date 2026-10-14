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
