"""Analytics' events: grading.finalized.v2 (and v1, from before points) stores each finalized evaluation's evidence
once (inbox), and identity.classroom.deleted.v1 purges a deleted classroom's evidence. Messages that don't match the
contract, or whose event type is not their topic's, are dead-lettered; learning_transport.kafka runs the consumer.

Storing and purging take the same per-classroom lock, so a purge and a finalized grade for its classroom never
interleave across replicas: either the evidence is stored first and the purge removes it, or the purge commits first
and the grade sees the tombstone."""

import os
import threading
from typing import Any

from learning_transport.db import LOCK_CLASSROOM, lock
from learning_transport.kafka import Handler, consume, parse
from psycopg_pool import ConnectionPool

from domain import FINALIZED_V1, FINALIZED_V2, ClassroomDeletedEvent, Event

CLASSROOM_DELETED = "identity.classroom.deleted.v1"
DEFAULT_GROUP = "analytics-v1"  # KAFKA_GROUP overrides it, e.g. to rebuild from the start of the topics


def process(pool: ConnectionPool[Any], event: Event) -> None:
    """Store the evaluation's evidence, unless this event or this evaluation was already stored, or its classroom
    was deleted."""
    with pool.connection() as conn, conn.transaction():
        inserted = conn.execute(
            "INSERT INTO inbox(event_id) VALUES(%s) ON CONFLICT DO NOTHING RETURNING event_id",
            (event.event_id,),
        ).fetchone()
        if inserted is None:
            return
        data = event.data
        lock(conn, LOCK_CLASSROOM, data.classroom_id)
        purged = conn.execute(
            "SELECT 1 FROM purged_classrooms WHERE classroom_id=%s", (data.classroom_id,)
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
        evaluations = conn.execute(
            "DELETE FROM evidence WHERE classroom_id=%s AND org_id=%s RETURNING evaluation_id",
            (data.classroom_id, data.org_id),
        ).fetchall()
        if evaluations:
            conn.execute(
                "DELETE FROM evaluations WHERE evaluation_id = ANY(%s)",
                ([row["evaluation_id"] for row in evaluations],),
            )


def handler(pool: ConnectionPool[Any]) -> Handler:
    def handle(topic: str, value: bytes | None) -> bool:
        if topic == CLASSROOM_DELETED:
            deleted = parse(ClassroomDeletedEvent, value)
            if deleted is None:
                return False
            purge(pool, deleted)
        else:
            finalized = parse(Event, value)
            if finalized is None or finalized.event_type != topic:
                return False
            process(pool, finalized)
        return True

    return handle


def run(pool: ConnectionPool[Any], stop: threading.Event, connected: threading.Event) -> None:
    group = os.environ.get("KAFKA_GROUP") or DEFAULT_GROUP
    consume(group, [FINALIZED_V1, FINALIZED_V2, CLASSROOM_DELETED], handler(pool), stop, connected)
