"""Learning Analytics: per-student skill accuracy from finalized grades only."""

import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import Depends, Request
from fastapi.concurrency import run_in_threadpool
from learning_transport import (
    OIDC_SETTINGS,
    TEACHER,
    Claims,
    IdentityClient,
    UpstreamError,
    application,
    failure,
    require_env,
    requires,
)
from learning_transport.db import open_pool
from psycopg_pool import ConnectionPool

from consumer import run
from domain import PerformanceResponse, summarize

MIGRATIONS = Path(__file__).parent / "migrations"
CONSUMER_STOP_GRACE = 35  # seconds shutdown waits for the consumer thread


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    require_env(*OIDC_SETTINGS, "IDENTITY_URL", "KAFKA_BROKERS", "DATABASE_URL")
    pool = open_pool(os.environ["DATABASE_URL"], MIGRATIONS)
    stop, connected = threading.Event(), threading.Event()
    thread = threading.Thread(target=run, args=(pool, stop, connected), daemon=True)
    async with httpx.AsyncClient() as http:
        app.state.pool, app.state.connected = pool, connected
        app.state.identity = IdentityClient(os.environ["IDENTITY_URL"], http)
        thread.start()
        yield
        stop.set()
        thread.join(timeout=CONSUMER_STOP_GRACE)
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


def skill_evidence(pool: ConnectionPool[Any], org: str, classroom_id: UUID, student_id: str) -> list[Any]:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT skill_id,correct FROM evidence WHERE org_id=%s AND classroom_id=%s AND student_id=%s",
            (org, classroom_id, student_id),
        ).fetchall()


@app.get("/v1/performance/{student_id}")
async def performance(
    student_id: str,
    classroom_id: UUID,
    request: Request,
    claims: Claims = Depends(requires("teacher", "student")),
) -> PerformanceResponse:
    if claims.person_role != TEACHER and student_id != claims.subject:
        raise failure(403, "FORBIDDEN", "Students can only view their own performance.")
    identity: IdentityClient = request.app.state.identity
    try:
        relation = await identity.relation(
            request.headers["authorization"], str(classroom_id), request.state.correlation_id, student_id
        )
    except UpstreamError:
        raise failure(503, "DEPENDENCY_UNAVAILABLE", "Authorization is unavailable.") from None
    if relation is None:
        raise failure(403, "ACCESS_DENIED", "Classroom or student access could not be verified.")
    rows = await run_in_threadpool(
        skill_evidence, request.app.state.pool, claims.org, classroom_id, student_id
    )
    return PerformanceResponse.model_validate(
        {
            "student_id": student_id,
            "classroom_id": str(classroom_id),
            "skills": summarize(rows),
            "evidence_status": "no_finalized_evidence" if not rows else "available",
            "interpretation": "Observed accuracy describes finalized question evidence, not a mastery probability.",
        }
    )
