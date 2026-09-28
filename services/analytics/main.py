from pathlib import Path
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from uuid import UUID

import httpx
from confluent_kafka import Consumer, KafkaException, Producer
from fastapi import Depends, Request
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import ValidationError
from learning_transport import actor, application, failure
from domain import Event, PerformanceResponse, summarize

MIGRATION = (Path(__file__).parent / "migrations/001_init.sql").read_text()
logger = logging.getLogger("analytics")


def process(pool: ConnectionPool[Any], event: Event) -> None:
    with pool.connection() as conn, conn.transaction():
        inserted = conn.execute(
            "INSERT INTO inbox(event_id) VALUES(%s) ON CONFLICT DO NOTHING RETURNING event_id",
            (event.event_id,),
        ).fetchone()
        if inserted is None:
            return
        data = event.data
        inserted = conn.execute(
            "INSERT INTO evaluations(evaluation_id,event_id) VALUES(%s,%s) ON CONFLICT(evaluation_id) DO NOTHING RETURNING evaluation_id",
            (data.evaluation_id, event.event_id),
        ).fetchone()
        if inserted is None:
            return
        for e in data.evidence:
            conn.execute(
                "INSERT INTO evidence(evaluation_id,question_id,assessment_id,classroom_id,org_id,student_id,skill_id,correct,awarded,possible) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
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


def consume(pool: ConnectionPool[Any], stop: threading.Event, connected: threading.Event) -> None:
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
    consumer.subscribe(["grading.finalized.v1"])
    try:
        consumer.list_topics(timeout=10)
        connected.set()
        while not stop.is_set():
            msg = consumer.poll(1)
            if msg is None:
                continue
            if msg.error():
                raise KafkaException(msg.error())
            try:
                event = Event.model_validate_json(msg.value())
            except (ValidationError, ValueError):
                delivery_errors: list[Any] = []
                producer.produce(
                    "grading.finalized.v1.dlq",
                    key=msg.key(),
                    value=msg.value(),
                    on_delivery=lambda error, _: delivery_errors.append(error) if error else None,
                )
                if producer.flush(12) or delivery_errors:
                    raise RuntimeError("Dead-letter delivery failed")
                logger.error(
                    json.dumps(
                        {"service": "analytics", "error_category": "invalid_event", "dead_lettered": True}
                    )
                )
                consumer.commit(message=msg, asynchronous=False)
                continue
            for attempt in range(5):
                try:
                    process(pool, event)
                    break
                except Exception:
                    logger.error(
                        json.dumps(
                            {
                                "service": "analytics",
                                "error_category": "consumer_retry",
                                "event_id": str(event.event_id),
                                "correlation_id": str(event.correlation_id),
                                "attempt": attempt + 1,
                            }
                        )
                    )
                    if attempt == 4 or stop.wait(2**attempt):
                        return
            consumer.commit(message=msg, asynchronous=False)
    except Exception:
        logger.error(json.dumps({"service": "analytics", "error_category": "consumer_stopped_uncommitted"}))
    finally:
        connected.clear()
        consumer.close()


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    for key in ("OIDC_ISSUER", "OIDC_JWKS_URL", "OIDC_AUDIENCE", "IDENTITY_URL", "KAFKA_BROKERS"):
        if not os.environ.get(key):
            raise RuntimeError(f"Missing configuration: {key}")
    pool = ConnectionPool(
        os.environ["DATABASE_URL"],
        min_size=1,
        max_size=8,
        timeout=5,
        kwargs={"row_factory": dict_row, "options": "-c statement_timeout=10000"},
    )
    pool.wait(timeout=15)
    with pool.connection() as conn:
        conn.execute(MIGRATION)
    stop, connected = threading.Event(), threading.Event()
    thread = threading.Thread(target=consume, args=(pool, stop, connected), daemon=True)
    app.state.pool, app.state.connected = pool, connected
    thread.start()
    yield
    stop.set()
    thread.join(timeout=35)
    pool.close()


app = application("analytics", lifespan=lifespan)


@app.get("/health/ready")
def ready(request: Request) -> dict[str, str]:
    try:
        with request.app.state.pool.connection() as conn:
            conn.execute("SELECT 1")
    except Exception:
        raise failure(503, "DEPENDENCY_UNAVAILABLE", "Database unavailable.") from None
    if not request.app.state.connected.is_set():
        raise failure(503, "DEPENDENCY_UNAVAILABLE", "Event consumer is not ready.")
    return {"status": "ready"}


@app.get("/v1/performance/{student_id}")
def performance(
    student_id: str, classroom_id: UUID, request: Request, claims: dict[str, Any] = Depends(actor)
) -> PerformanceResponse:
    roles = claims.get("realm_access", {}).get("roles", [])
    if "teacher" not in roles and ("student" not in roles or student_id != claims["sub"]):
        raise failure(403, "FORBIDDEN", "Students can only view their own performance.")
    try:
        res = httpx.get(
            f"{os.environ['IDENTITY_URL']}/v1/classrooms/{classroom_id}/access",
            params={"student_id": student_id},
            headers={
                "Authorization": request.headers["authorization"],
                "X-Correlation-ID": request.state.correlation_id,
            },
            timeout=5,
        )
    except httpx.HTTPError:
        raise failure(503, "DEPENDENCY_UNAVAILABLE", "Authorization is unavailable.") from None
    if res.status_code != 200:
        raise failure(
            503 if res.status_code >= 500 else 403,
            "ACCESS_DENIED",
            "Classroom or student access could not be verified.",
        )
    with request.app.state.pool.connection() as conn:
        rows = conn.execute(
            "SELECT skill_id,correct FROM evidence WHERE org_id=%s AND classroom_id=%s AND student_id=%s",
            (claims["org_id"], classroom_id, student_id),
        ).fetchall()
    return PerformanceResponse.model_validate(
        {
            "student_id": student_id,
            "classroom_id": str(classroom_id),
            "skills": summarize(rows),
            "evidence_status": "no_finalized_evidence" if not rows else "available",
            "interpretation": "Observed accuracy describes finalized question evidence, not a mastery probability.",
        }
    )
