"""Analytics' events: grading.finalized.v3 (and v2 and v1, still read for queued or replayed events, with their
evidence's older field mapped onto topic_id) stores each finalized evaluation's evidence once (inbox), and
identity.classroom.deleted.v1 purges a deleted classroom's evidence, assessment.deleted.v1 a deleted assessment's
(ADR 0008), and identity.user.deleted.v1 the evidence of a student who deleted their account (ADR 0011). Messages
that don't match the contract, or whose event type is not their Kafka topic's, are dead-lettered;
learning_transport.kafka runs the consumer.

Storing and purging take the same per-classroom lock (an assessment's purge takes its classroom's), so a purge and a
finalized grade for its classroom never interleave across replicas: either the evidence is stored first and the purge
removes it, or the purge commits first and the grade sees the tombstone. A deleted account's purge and the student's
grades take the student's lock the same way."""

import os
import threading
from collections.abc import Callable
from typing import Any

from learning_transport.db import LOCK_CLASSROOM, LOCK_USER, lock
from learning_transport.kafka import (
    CLASSROOM_DELETED,
    USER_DELETED,
    ClassroomDeletedEvent,
    Envelope,
    Handler,
    UserDeletedEvent,
    consume,
    parse,
)
from psycopg import Connection
from psycopg_pool import ConnectionPool

from domain import FINALIZED, AssessmentDeletedEvent, Event

ASSESSMENT_DELETED = "assessment.deleted.v1"
DEFAULT_GROUP = "analytics-v1"  # KAFKA_GROUP overrides it, e.g. to rebuild from the start of the topics


def process(pool: ConnectionPool[Any], event: Event) -> None:
    """Store the evaluation's evidence, unless this event or this evaluation was already stored, or its classroom or
    assessment was deleted, or its student deleted their account."""
    with pool.connection() as conn, conn.transaction():
        inserted = conn.execute(
            "INSERT INTO inbox(event_id) VALUES(%s) ON CONFLICT DO NOTHING RETURNING event_id",
            (event.event_id,),
        ).fetchone()
        if inserted is None:
            return
        data = event.data
        lock(conn, LOCK_CLASSROOM, data.classroom_id)
        lock(conn, LOCK_USER, data.student_id)
        purged = conn.execute(
            "SELECT 1 FROM purged_classrooms WHERE classroom_id=%s"
            " UNION ALL SELECT 1 FROM deleted_assessments WHERE assessment_id=%s"
            " UNION ALL SELECT 1 FROM deleted_users WHERE user_id=%s",
            (data.classroom_id, data.assessment_id, data.student_id),
        ).fetchone()
        if purged is not None:
            return
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
                "topic_id,correct,awarded,possible) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    data.evaluation_id,
                    e.question_id,
                    data.assessment_id,
                    data.classroom_id,
                    data.org_id,
                    data.student_id,
                    e.topic_id,
                    e.correct,
                    e.awarded,
                    e.possible,
                ),
            )


def remove_evidence(conn: Connection[Any], where: str, params: tuple[Any, ...]) -> None:
    """Delete the evidence `where` picks (a condition with %s placeholders for `params`) and the evaluations it was
    stored under."""
    evaluations = conn.execute(
        f"DELETE FROM evidence WHERE {where} RETURNING evaluation_id", params
    ).fetchall()
    if evaluations:
        conn.execute(
            "DELETE FROM evaluations WHERE evaluation_id = ANY(%s)",
            ([row["evaluation_id"] for row in evaluations],),
        )


def purge(pool: ConnectionPool[Any], event: ClassroomDeletedEvent) -> None:
    """Remove a deleted classroom's evidence and remember the classroom, so a finalized grade that arrives later is
    not stored. Deleting what is already gone changes nothing, so a repeated event is harmless."""
    data = event.data
    with pool.connection() as conn, conn.transaction():
        lock(conn, LOCK_CLASSROOM, data.classroom_id)
        conn.execute(
            "INSERT INTO purged_classrooms(classroom_id,org_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (data.classroom_id, data.org_id),
        )
        remove_evidence(conn, "classroom_id=%s AND org_id=%s", (data.classroom_id, data.org_id))


def purge_assessment(pool: ConnectionPool[Any], event: AssessmentDeletedEvent) -> None:
    """Remove a deleted assessment's evidence and remember the assessment, so a finalized grade for it that arrives
    later is not stored. Deleting what is already gone changes nothing, so a repeated event is harmless."""
    data = event.data
    with pool.connection() as conn, conn.transaction():
        lock(conn, LOCK_CLASSROOM, data.classroom_id)
        conn.execute(
            "INSERT INTO deleted_assessments(assessment_id,classroom_id,org_id) VALUES(%s,%s,%s)"
            " ON CONFLICT DO NOTHING",
            (data.assessment_id, data.classroom_id, data.org_id),
        )
        remove_evidence(
            conn,
            "assessment_id=%s AND classroom_id=%s AND org_id=%s",
            (data.assessment_id, data.classroom_id, data.org_id),
        )


def purge_user(pool: ConnectionPool[Any], event: UserDeletedEvent) -> None:
    """Remove the evidence of a student who deleted their account and remember them, so a finalized grade of theirs
    that arrives later is not stored. A teacher's account holds no evidence. Deleting what is already gone changes
    nothing, so a repeated event is harmless."""
    data = event.data
    with pool.connection() as conn, conn.transaction():
        lock(conn, LOCK_USER, str(data.user_id))
        conn.execute(
            "INSERT INTO deleted_users(user_id,org_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (str(data.user_id), data.org_id),
        )
        remove_evidence(conn, "student_id=%s AND org_id=%s", (str(data.user_id), data.org_id))


Purge = Callable[[ConnectionPool[Any], bytes | None], bool]


def parsed[E: Envelope](model: type[E], apply: Callable[[ConnectionPool[Any], E], None]) -> Purge:
    """A deletion topic's handling: its message parsed as `model` and applied, or False when it is not one."""

    def run(pool: ConnectionPool[Any], value: bytes | None) -> bool:
        deleted = parse(model, value)
        if deleted is None:
            return False
        apply(pool, deleted)
        return True

    return run


# Each deletion topic and the purge that applies its event.
PURGES: dict[str, Purge] = {
    CLASSROOM_DELETED: parsed(ClassroomDeletedEvent, purge),
    ASSESSMENT_DELETED: parsed(AssessmentDeletedEvent, purge_assessment),
    USER_DELETED: parsed(UserDeletedEvent, purge_user),
}


def handler(pool: ConnectionPool[Any]) -> Handler:
    def handle(topic: str, value: bytes | None) -> bool:
        if topic in PURGES:
            return PURGES[topic](pool, value)
        finalized = parse(Event, value)
        if finalized is None or finalized.event_type != topic:
            return False
        process(pool, finalized)
        return True

    return handle


def run(pool: ConnectionPool[Any], stop: threading.Event, connected: threading.Event) -> None:
    group = os.environ.get("KAFKA_GROUP") or DEFAULT_GROUP
    consume(
        group,
        [*FINALIZED, CLASSROOM_DELETED, ASSESSMENT_DELETED, USER_DELETED],
        handler(pool),
        stop,
        connected,
    )
