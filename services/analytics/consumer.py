"""keep each evaluation's newest released evidence, withdraw it while its teacher corrects it, and purge it when its classroom, assessment or account is deleted."""

import os
import threading
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from learning_transport import Contract
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
from pydantic import Field, model_validator

from domain import FINALIZED, AssessmentDeletedEvent, Event, Finalized

ASSESSMENT_DELETED = "assessment.deleted.v1"
REOPENED = "grading.reopened.v1"
DEFAULT_GROUP = "analytics-v1"  # KAFKA_GROUP overrides it, e.g. to rebuild from the start of the topics


class Reopened(Contract):
    evaluation_id: UUID
    assessment_id: UUID
    classroom_id: UUID
    org_id: str = Field(min_length=1)
    student_id: str = Field(min_length=1)
    revision: int = Field(ge=2, strict=True)


class ReopenedEvent(Envelope):
    """a released grade its teacher reopened to correct; a draft again at `revision` (adr 0016)."""

    event_type: Literal["grading.reopened.v1"]
    data: Reopened

    @model_validator(mode="after")
    def keyed(self) -> "ReopenedEvent":
        if self.resource_id != self.data.evaluation_id:
            raise ValueError("Not a reopened grading event")
        return self


def admit(conn: Connection[Any], event_id: UUID, data: Finalized | Reopened) -> bool:
    """whether an event is new and its classroom, assessment and student are not deleted; takes their locks."""
    inserted = conn.execute(
        "INSERT INTO inbox(event_id) VALUES(%s) ON CONFLICT DO NOTHING RETURNING event_id",
        (event_id,),
    ).fetchone()
    if inserted is None:
        return False
    lock(conn, LOCK_CLASSROOM, data.classroom_id)
    lock(conn, LOCK_USER, data.student_id)
    purged = conn.execute(
        "SELECT 1 FROM purged_classrooms WHERE classroom_id=%s"
        " UNION ALL SELECT 1 FROM deleted_assessments WHERE assessment_id=%s"
        " UNION ALL SELECT 1 FROM deleted_users WHERE user_id=%s",
        (data.classroom_id, data.assessment_id, data.student_id),
    ).fetchone()
    return purged is None


def process(pool: ConnectionPool[Any], event: Event) -> None:
    """store the evaluation's evidence at its revision, replacing an older revision's or a withdrawal of the same one; a repeated or older event, or one for a deleted classroom, assessment or account, changes nothing."""
    data = event.data
    with pool.connection() as conn, conn.transaction():
        if not admit(conn, event.event_id, data):
            return
        newer = conn.execute(
            "INSERT INTO evaluations(evaluation_id,event_id,revision) VALUES(%s,%s,%s)"
            " ON CONFLICT(evaluation_id) DO UPDATE SET event_id=EXCLUDED.event_id,revision=EXCLUDED.revision,withdrawn=false"
            " WHERE evaluations.revision<EXCLUDED.revision"
            " OR (evaluations.revision=EXCLUDED.revision AND evaluations.withdrawn)"
            " RETURNING evaluation_id",
            (data.evaluation_id, event.event_id, data.revision),
        ).fetchone()
        if newer is None:
            return
        conn.execute("DELETE FROM evidence WHERE evaluation_id=%s", (data.evaluation_id,))
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


def withdraw(pool: ConnectionPool[Any], event: ReopenedEvent) -> None:
    """remove a reopened grade's evidence and remember its revision, so only a finalization of that revision or newer stores it again; a repeated or older event changes nothing."""
    data = event.data
    with pool.connection() as conn, conn.transaction():
        if not admit(conn, event.event_id, data):
            return
        newer = conn.execute(
            "INSERT INTO evaluations(evaluation_id,event_id,revision,withdrawn) VALUES(%s,%s,%s,true)"
            " ON CONFLICT(evaluation_id) DO UPDATE SET event_id=EXCLUDED.event_id,revision=EXCLUDED.revision,withdrawn=true"
            " WHERE evaluations.revision<EXCLUDED.revision RETURNING evaluation_id",
            (data.evaluation_id, event.event_id, data.revision),
        ).fetchone()
        if newer is not None:
            conn.execute("DELETE FROM evidence WHERE evaluation_id=%s", (data.evaluation_id,))


def remove_evidence(conn: Connection[Any], where: str, params: tuple[Any, ...]) -> None:
    """delete the evidence `where` picks (a condition with %s placeholders for `params`) and the evaluations it was stored under."""
    evaluations = conn.execute(
        f"DELETE FROM evidence WHERE {where} RETURNING evaluation_id", params
    ).fetchall()
    if evaluations:
        conn.execute(
            "DELETE FROM evaluations WHERE evaluation_id = ANY(%s)",
            ([row["evaluation_id"] for row in evaluations],),
        )


def purge(pool: ConnectionPool[Any], event: ClassroomDeletedEvent) -> None:
    """remove a deleted classroom's evidence and remember the classroom, so a finalized grade that arrives later is not stored."""
    data = event.data
    with pool.connection() as conn, conn.transaction():
        lock(conn, LOCK_CLASSROOM, data.classroom_id)
        conn.execute(
            "INSERT INTO purged_classrooms(classroom_id,org_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (data.classroom_id, data.org_id),
        )
        remove_evidence(conn, "classroom_id=%s AND org_id=%s", (data.classroom_id, data.org_id))


def purge_assessment(pool: ConnectionPool[Any], event: AssessmentDeletedEvent) -> None:
    """remove a deleted assessment's evidence and remember the assessment, so a finalized grade for it that arrives later is not stored."""
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
    """remove the evidence of a student who deleted their account and remember them, so a finalized grade of theirs that arrives later is not stored."""
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
    """a deletion topic's handling: its message parsed as `model` and applied, or false when it is not one."""

    def run(pool: ConnectionPool[Any], value: bytes | None) -> bool:
        deleted = parse(model, value)
        if deleted is None:
            return False
        apply(pool, deleted)
        return True

    return run


# each deletion topic and the purge that applies its event, and the withdrawal of a reopened grade.
PURGES: dict[str, Purge] = {
    CLASSROOM_DELETED: parsed(ClassroomDeletedEvent, purge),
    ASSESSMENT_DELETED: parsed(AssessmentDeletedEvent, purge_assessment),
    USER_DELETED: parsed(UserDeletedEvent, purge_user),
    REOPENED: parsed(ReopenedEvent, withdraw),
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
        [FINALIZED, REOPENED, CLASSROOM_DELETED, ASSESSMENT_DELETED, USER_DELETED],
        handler(pool),
        stop,
        connected,
    )
