"""the consumer's sql against a real postgresql."""

import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from learning_transport.db import LOCK_CLASSROOM, lock, migrations
from learning_transport.kafka import ClassroomDeletedEvent, UserDeletedEvent
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from consumer import process, purge, purge_assessment, purge_user
from domain import AssessmentDeletedEvent, Event
from main import MIGRATIONS
from test_domain import finalized

URL = os.environ.get("ANALYTICS_TEST_DATABASE_URL")


@pytest.fixture
def pool() -> Iterator[ConnectionPool[Any]]:
    if not URL:
        pytest.skip("ANALYTICS_TEST_DATABASE_URL is not set")
    schema = "test_" + uuid4().hex[:12]
    with psycopg.connect(URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        options = {"row_factory": dict_row, "options": f"-c search_path={schema}"}
        test_pool: ConnectionPool[Any] = ConnectionPool(
            URL, min_size=1, max_size=4, kwargs=options, open=True
        )
        try:
            with test_pool.connection() as conn, conn.transaction():
                for m in migrations(MIGRATIONS):
                    conn.execute(m.sql)  # type: ignore[arg-type]  # the service's own migration files
            yield test_pool
        finally:
            test_pool.close()
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


def grade(classroom: str | None = None) -> Event:
    event = finalized("grading.finalized.v3", 3)
    if classroom:
        event["data"]["classroom_id"] = classroom  # type: ignore[index]
    return Event.model_validate(event)


def deletion(classroom: str) -> ClassroomDeletedEvent:
    return ClassroomDeletedEvent.model_validate(
        {
            "event_id": str(uuid4()),
            "event_type": "identity.classroom.deleted.v1",
            "schema_version": 1,
            "timestamp": "2026-10-14T00:00:00Z",
            "correlation_id": str(uuid4()),
            "resource_id": classroom,
            "data": {"classroom_id": classroom, "org_id": "org"},
        }
    )


def assessment_deletion(event: Event) -> AssessmentDeletedEvent:
    """the deletion of the assessment a finalized grade is for."""
    assessment = str(event.data.assessment_id)
    return AssessmentDeletedEvent.model_validate(
        {
            "event_id": str(uuid4()),
            "event_type": "assessment.deleted.v1",
            "schema_version": 1,
            "timestamp": "2026-10-18T00:00:00Z",
            "correlation_id": str(uuid4()),
            "resource_id": assessment,
            "data": {
                "assessment_id": assessment,
                "classroom_id": str(event.data.classroom_id),
                "org_id": "org",
            },
        }
    )


def user_deletion(student: str) -> UserDeletedEvent:
    return UserDeletedEvent.model_validate(
        {
            "event_id": str(uuid4()),
            "event_type": "identity.user.deleted.v1",
            "schema_version": 1,
            "timestamp": "2026-10-18T00:00:00Z",
            "correlation_id": str(uuid4()),
            "resource_id": student,
            "data": {"user_id": student, "org_id": "org", "role": "student"},
        }
    )


def count(pool: ConnectionPool[Any], sql: str, *params: object) -> int:
    with pool.connection() as conn:
        row = conn.execute(sql, params).fetchone()
        assert row is not None
        return int(row["count"])


def evidence_of(pool: ConnectionPool[Any], classroom: str) -> int:
    return count(pool, "SELECT count(*) FROM evidence WHERE classroom_id=%s", classroom)


def test_a_grade_is_stored_once(pool):
    event = grade()
    process(pool, event)
    process(pool, event)
    again = event.model_copy(update={"event_id": uuid4()})  # the same evaluation, published twice
    process(pool, again)
    room = str(event.data.classroom_id)
    assert evidence_of(pool, room) == 1
    assert count(pool, "SELECT count(*) FROM inbox") == 2


def test_a_grade_for_a_purged_classroom_is_not_stored_but_is_recorded(pool):
    room = str(uuid4())
    purge(pool, deletion(room))
    event = grade(room)
    process(pool, event)
    assert evidence_of(pool, room) == 0
    assert count(pool, "SELECT count(*) FROM evaluations") == 0
    assert count(pool, "SELECT count(*) FROM inbox WHERE event_id=%s", event.event_id) == 1


def test_purging_removes_the_classrooms_evidence_only(pool):
    room, other = str(uuid4()), str(uuid4())
    process(pool, grade(room))
    process(pool, grade(other))
    purge(pool, deletion(room))
    purge(pool, deletion(room))  # a repeated deletion changes nothing
    assert (evidence_of(pool, room), evidence_of(pool, other)) == (0, 1)
    assert count(pool, "SELECT count(*) FROM evaluations") == 1


def test_a_deleted_assessments_evidence_goes_and_stays_gone(pool):
    room = str(uuid4())
    deleted, kept = grade(room), grade(room)
    process(pool, deleted)
    process(pool, kept)
    purge_assessment(pool, assessment_deletion(deleted))
    purge_assessment(pool, assessment_deletion(deleted))  # a repeated deletion changes nothing
    assert evidence_of(pool, room) == 1
    assert count(pool, "SELECT count(*) FROM evaluations") == 1
    # another grade for the deleted assessment, published before the deletion but consumed after it.
    late = grade(room)
    late.data.assessment_id = deleted.data.assessment_id
    process(pool, late)
    assert evidence_of(pool, room) == 1
    assert count(pool, "SELECT count(*) FROM inbox WHERE event_id=%s", late.event_id) == 1


def test_a_deleted_accounts_evidence_goes_and_stays_gone(pool):
    room = str(uuid4())
    deleted, kept = grade(room), grade(room)
    student = str(uuid4())
    deleted.data.student_id = student
    process(pool, deleted)
    process(pool, kept)
    purge_user(pool, user_deletion(student))
    purge_user(pool, user_deletion(student))  # a repeated deletion changes nothing
    assert evidence_of(pool, room) == 1
    assert count(pool, "SELECT count(*) FROM evaluations") == 1
    # another of the student's grades, published before the deletion but consumed after it.
    late = grade(room)
    late.data.student_id = student
    process(pool, late)
    assert count(pool, "SELECT count(*) FROM evidence WHERE student_id=%s", student) == 0
    assert count(pool, "SELECT count(*) FROM inbox WHERE event_id=%s", late.event_id) == 1


def wait_for_lock(pool: ConnectionPool[Any], done: threading.Event) -> None:
    """waits until some session is waiting on an advisory lock, failing if the work finished without waiting."""
    deadline = time.monotonic() + 5
    while count(pool, "SELECT count(*) FROM pg_stat_activity WHERE wait_event='advisory'") == 0:
        assert not done.is_set(), "it did not wait for the classroom lock"
        assert time.monotonic() < deadline, "it never waited for the classroom lock"
        time.sleep(0.01)


def in_thread(work: Callable[[], None]) -> tuple[threading.Thread, threading.Event]:
    done = threading.Event()

    def run() -> None:
        work()
        done.set()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, done


def test_a_grade_waits_for_a_purge_of_its_classroom_in_progress(pool):
    """a purge holding the classroom's lock commits its tombstone first; the grade then sees it."""
    room = str(uuid4())
    event = grade(room)
    with pool.connection() as conn, conn.transaction():
        lock(conn, LOCK_CLASSROOM, room)
        conn.execute("INSERT INTO purged_classrooms(classroom_id,org_id) VALUES(%s,'org')", (room,))
        thread, done = in_thread(lambda: process(pool, event))
        wait_for_lock(pool, done)
    thread.join(5)
    assert done.is_set() and evidence_of(pool, room) == 0


def test_a_purge_waits_for_a_grade_of_its_classroom_in_progress(pool):
    """a grade holding the classroom's lock commits its evidence first; the purge then removes it."""
    room = str(uuid4())
    with pool.connection() as conn, conn.transaction():
        lock(conn, LOCK_CLASSROOM, room)
        thread, done = in_thread(lambda: purge(pool, deletion(room)))
        wait_for_lock(pool, done)
        event = grade(room)
        conn.execute(
            "INSERT INTO evaluations(evaluation_id,event_id) VALUES(%s,%s)",
            (event.data.evaluation_id, event.event_id),
        )
        e = event.data.evidence[0]
        conn.execute(
            "INSERT INTO evidence(evaluation_id,question_id,assessment_id,classroom_id,org_id,student_id,topic_id,"
            "correct,awarded,possible) VALUES(%s,%s,%s,%s,'org','s1',%s,true,3,3)",
            (event.data.evaluation_id, e.question_id, event.data.assessment_id, room, e.topic_id),
        )
    thread.join(5)
    assert done.is_set() and evidence_of(pool, room) == 0


def test_the_consumer_routes_an_assessment_deletion_to_its_purge(pool):
    from consumer import ASSESSMENT_DELETED, handler

    room = str(uuid4())
    deleted, kept = grade(room), grade(room)
    process(pool, deleted)
    process(pool, kept)
    message = assessment_deletion(deleted).model_dump_json().encode()
    assert handler(pool)(ASSESSMENT_DELETED, message) is True
    assert evidence_of(pool, room) == 1
    assert count(pool, "SELECT count(*) FROM deleted_assessments") == 1
