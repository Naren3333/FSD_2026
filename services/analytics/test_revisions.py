"""a reopened grade's evidence is withdrawn and replaced by its correction, whatever order the events arrive in (adr 0016)."""

from itertools import permutations
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from learning_transport.kafka import PERMANENT_ERRORS
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

import test_store
from consumer import (
    REOPENED,
    ReopenedEvent,
    handler,
    process,
    purge,
    purge_assessment,
    purge_user,
    withdraw,
)
from domain import Event
from main import MIGRATIONS
from test_domain import finalized
from test_store import assessment_deletion, count, deletion, user_deletion

pool = test_store.pool  # the database fixture


def revision(event: Event, number: int, awarded: int) -> Event:
    """the same evaluation finalized again at `number` with `awarded` of its 3 marks, as a new event."""
    data = event.data.model_copy(
        update={
            "revision": number,
            "evidence": [
                e.model_copy(update={"awarded": awarded, "correct": awarded == e.possible})
                for e in event.data.evidence
            ],
        }
    )
    return event.model_copy(update={"event_id": uuid4(), "data": data})


def reopening(event: Event, number: int) -> ReopenedEvent:
    data = event.data
    return ReopenedEvent.model_validate(
        {
            "event_id": str(uuid4()),
            "event_type": REOPENED,
            "schema_version": 1,
            "timestamp": "2026-10-26T08:00:00Z",
            "correlation_id": str(uuid4()),
            "resource_id": str(data.evaluation_id),
            "data": {
                "evaluation_id": str(data.evaluation_id),
                "assessment_id": str(data.assessment_id),
                "classroom_id": str(data.classroom_id),
                "org_id": data.org_id,
                "student_id": data.student_id,
                "revision": number,
            },
        }
    )


def awarded(pool: ConnectionPool[Any], event: Event) -> list[int]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT awarded FROM evidence WHERE evaluation_id=%s", (event.data.evaluation_id,)
        ).fetchall()
    return [row["awarded"] for row in rows]


def test_an_event_from_before_revisions_is_revision_1():
    assert Event.model_validate(finalized("grading.finalized.v3", 3)).data.revision == 1


def test_a_reopened_event_names_its_evaluation_and_a_later_revision():
    event = Event.model_validate(finalized("grading.finalized.v3", 3))
    body = reopening(event, 2).model_dump(mode="json")
    with pytest.raises(ValidationError):
        ReopenedEvent.model_validate({**body, "resource_id": str(uuid4())})
    with pytest.raises(ValidationError):
        ReopenedEvent.model_validate({**body, "data": {**body["data"], "revision": 1}})


def test_the_consumer_rejects_a_malformed_reopened_event():
    assert handler(None)(REOPENED, b"{}") is False  # type: ignore[arg-type]


def test_a_correction_replaces_the_evidence_in_any_order(pool):
    for order in permutations(["first", "reopen", "corrected"]):
        first = Event.model_validate(finalized("grading.finalized.v3", 3))
        events = {"first": first, "reopen": reopening(first, 2), "corrected": revision(first, 2, 1)}
        for name in order:
            event = events[name]
            if isinstance(event, ReopenedEvent):
                withdraw(pool, event)
            else:
                process(pool, event)
        assert awarded(pool, first) == [1], order


def test_a_reopen_withdraws_the_evidence_until_the_correction(pool):
    first = Event.model_validate(finalized("grading.finalized.v3", 3))
    process(pool, first)
    withdraw(pool, reopening(first, 2))
    assert awarded(pool, first) == []
    process(pool, first.model_copy(update={"event_id": uuid4()}))  # the first revision published again
    assert awarded(pool, first) == []
    process(pool, revision(first, 2, 2))
    assert awarded(pool, first) == [2]


def test_repeated_and_older_events_change_nothing(pool):
    first = Event.model_validate(finalized("grading.finalized.v3", 3))
    second, third = revision(first, 2, 1), revision(first, 3, 0)
    for event in (first, second, third):
        process(pool, event)
    withdraw(pool, reopening(first, 3))  # its correction already arrived
    for event in (third, second, first):
        process(pool, event)
        process(pool, event.model_copy(update={"event_id": uuid4()}))
    assert awarded(pool, first) == [0]
    reopened = reopening(first, 4)
    withdraw(pool, reopened)
    withdraw(pool, reopened)
    assert awarded(pool, first) == []
    assert count(pool, "SELECT count(*) FROM evaluations WHERE revision=4 AND withdrawn") == 1


def test_the_consumer_routes_a_reopen_to_its_withdrawal(pool):
    first = Event.model_validate(finalized("grading.finalized.v3", 3))
    process(pool, first)
    assert handler(pool)(REOPENED, reopening(first, 2).model_dump_json().encode()) is True
    assert awarded(pool, first) == []


def test_a_reopen_for_a_purged_classroom_is_not_stored(pool):
    first = Event.model_validate(finalized("grading.finalized.v3", 3))
    purge(pool, deletion(str(first.data.classroom_id)))
    withdraw(pool, reopening(first, 2))
    assert count(pool, "SELECT count(*) FROM evaluations") == 0


@pytest.mark.parametrize("deleted", ["classroom", "assessment", "student"])
def test_a_purge_removes_a_withdrawn_grade_with_no_evidence(pool, deleted):
    grades = []
    for _ in range(2):
        body = finalized("grading.finalized.v3", 3)
        body["data"]["student_id"] = str(uuid4())  # type: ignore[index]
        grades.append(Event.model_validate(body))
    first, other = grades
    for event in grades:
        process(pool, event)
        withdraw(pool, reopening(event, 2))
    if deleted == "classroom":
        purge(pool, deletion(str(first.data.classroom_id)))
    elif deleted == "assessment":
        purge_assessment(pool, assessment_deletion(first))
    else:
        purge_user(pool, user_deletion(first.data.student_id))
    assert (
        count(pool, "SELECT count(*) FROM evaluations WHERE evaluation_id=%s", first.data.evaluation_id) == 0
    )
    assert (
        count(pool, "SELECT count(*) FROM evaluations WHERE evaluation_id=%s", other.data.evaluation_id) == 1
    )


def test_migration_009_names_the_owners_of_evaluations_stored_before_008(pool):
    first = Event.model_validate(finalized("grading.finalized.v3", 3))
    process(pool, first)
    with pool.connection() as conn:
        conn.execute(
            "UPDATE evaluations SET assessment_id=NULL,classroom_id=NULL,org_id=NULL,student_id=NULL"
        )
        conn.execute((MIGRATIONS / "009_evaluation_owners_backfill.sql").read_text())  # type: ignore[arg-type]
    assert (
        count(
            pool,
            "SELECT count(*) FROM evaluations WHERE classroom_id=%s AND assessment_id=%s AND org_id=%s AND student_id=%s",
            first.data.classroom_id,
            first.data.assessment_id,
            first.data.org_id,
            first.data.student_id,
        )
        == 1
    )


def test_an_evaluation_without_owners_waits_for_a_writer_that_names_them(pool):
    """stored as an older replica would during a rolling upgrade: refused with an error the consumer retries rather than dead-letters, and stored by a replica that names its owners."""
    event = Event.model_validate(finalized("grading.finalized.v3", 3))
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState) as refused, pool.connection() as conn:
        conn.execute(
            "INSERT INTO evaluations(evaluation_id,event_id,revision,withdrawn) VALUES(%s,%s,1,true)",
            (event.data.evaluation_id, event.event_id),
        )
    assert not isinstance(refused.value, PERMANENT_ERRORS)
    process(pool, event)
    assert count(pool, "SELECT count(*) FROM evaluations WHERE classroom_id=%s", event.data.classroom_id) == 1
