"""The question chatbot (ADR 0002 stage 4): students and teachers chat about a classroom's materials. Ported from
fsd_test's assistant. Reached through Kong at /api/chatbot with the user's own token."""

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, Query, Request
from learning_transport import (
    OIDC_SETTINGS,
    STUDENT,
    TEACHER,
    Claims,
    ErrorResponse,
    UpstreamError,
    application,
    failure,
    require_env,
    require_relation,
    requires,
)
from learning_transport.db import open_pool

import config
from chat import HISTORY_TURNS, SSEResponse, Turn, TurnRequest
from clients import Services, billed
from contracts import MAX_MESSAGES, MessageList, NewMessage, NewThread, Thread, ThreadList, Usage
from purge import run as purge_deleted_classrooms
from store import MIGRATIONS, PostgresStore, Store

ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 503)
}
SAVE_GRACE = 10.0  # seconds the shutdown waits for interrupted answers still being saved
CONSUMER_STOP_GRACE = 35  # seconds shutdown waits for the classroom-deletion consumer


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    require_env(*OIDC_SETTINGS, "KAFKA_BROKERS")
    settings = config.load()
    store = await PostgresStore.open(settings.database_url)
    # Deleted classrooms' threads are purged by a consumer thread, with its own small synchronous pool.
    purge_pool = open_pool(settings.database_url, MIGRATIONS)
    stop, connected = threading.Event(), threading.Event()
    consumer = threading.Thread(
        target=purge_deleted_classrooms, args=(purge_pool, stop, connected), daemon=True
    )
    async with httpx.AsyncClient() as http:
        app.state.settings, app.state.store = settings, store
        app.state.services = Services(settings, http)
        app.state.pending = set()
        consumer.start()
        yield
        if app.state.pending:
            await asyncio.wait(app.state.pending, timeout=SAVE_GRACE)
        stop.set()
        await asyncio.to_thread(consumer.join, CONSUMER_STOP_GRACE)
    purge_pool.close()
    await store.close()


app = application("question-chatbot-agent", lifespan=lifespan)


# Students and teachers chat; service accounts may not.
PERSON = requires(TEACHER, STUDENT)


def store_of(request: Request) -> Store:
    store: Store = request.app.state.store
    return store


async def owned_thread(store: Store, claims: Claims, thread_id: UUID) -> dict[str, Any]:
    """The thread if the caller owns it; 404 otherwise, so ids don't leak."""
    thread = await store.thread(claims.org, claims.subject, str(thread_id))
    if thread is None:
        raise failure(404, "NOT_FOUND", "Thread not found.")
    return thread


async def require_access(request: Request, classroom_id: str) -> str:
    """The caller's relation to the classroom (teacher or student), or 403 unless Identity says the caller may use
    the classroom now. Enrollment can end, so this is asked on every read and turn, not only when a thread is
    created. The relation, not the token's role, decides the prompt and the allowance."""
    services: Services = request.app.state.services
    return await require_relation(services.identity, request, classroom_id)


async def accessible_thread(request: Request, claims: Claims, thread_id: UUID) -> tuple[dict[str, Any], str]:
    """The caller's thread, if they still have access to its classroom, and their relation to that classroom."""
    thread = await owned_thread(store_of(request), claims, thread_id)
    relation = await require_access(request, str(thread["classroom_id"]))
    return thread, relation


@app.get("/health/ready")
async def ready(request: Request) -> dict[str, str]:
    try:
        await asyncio.wait_for(request.app.state.store.ping(), 2)
    except Exception:
        raise failure(503, "DEPENDENCY_UNAVAILABLE", "Database unavailable.") from None
    return {"status": "ready"}


@app.post("/v1/threads", status_code=201, response_model=Thread, responses=ERRORS)
async def create_thread(body: NewThread, request: Request, claims: Claims = Depends(PERSON)) -> Any:
    relation = await require_access(request, str(body.classroom_id))
    thread = await store_of(request).create_thread(
        claims.org, claims.subject, relation, str(body.classroom_id)
    )
    if thread is None:  # the classroom was deleted (and purged) after Identity allowed it
        raise failure(403, "FORBIDDEN", "You do not have access to this classroom.")
    return thread


@app.get("/v1/threads", response_model=ThreadList, responses=ERRORS)
async def list_threads(
    request: Request, classroom_id: UUID | None = Query(default=None), claims: Claims = Depends(PERSON)
) -> Any:
    if classroom_id is not None:
        await require_access(request, str(classroom_id))
    rows = await store_of(request).list_threads(
        claims.org, claims.subject, None if classroom_id is None else str(classroom_id)
    )
    if classroom_id is None:
        # Enrollment can end, so even the unfiltered list shows only threads (and their titles) in classrooms
        # Identity says the caller may still use.
        rooms = sorted({str(r["classroom_id"]) for r in rows})
        allowed = await asyncio.gather(*(may_use(request, room) for room in rooms))
        kept = {room for room, ok in zip(rooms, allowed, strict=True) if ok}
        rows = [r for r in rows if str(r["classroom_id"]) in kept]
    return {"threads": rows}


async def may_use(request: Request, classroom_id: str) -> bool:
    """Whether the caller may use the classroom now; Identity being unavailable is still a 503."""
    try:
        await require_access(request, classroom_id)
    except HTTPException as error:
        if error.status_code == 403:
            return False
        raise
    return True


@app.get("/v1/threads/{thread_id}/messages", response_model=MessageList, responses=ERRORS)
async def list_messages(
    thread_id: UUID,
    request: Request,
    limit: int = Query(default=MAX_MESSAGES, ge=1, le=MAX_MESSAGES),
    claims: Claims = Depends(PERSON),
) -> Any:
    """The thread's latest `limit` messages, oldest first."""
    await accessible_thread(request, claims, thread_id)
    return {"messages": await store_of(request).messages(str(thread_id), limit)}


@app.post(
    "/v1/threads/{thread_id}/messages",
    responses={**ERRORS, 200: {"description": "The answer as server-sent events: token, citations, done or error",
                               "content": {"text/event-stream": {}}}},
)  # fmt: skip
async def post_message(
    thread_id: UUID, body: NewMessage, request: Request, claims: Claims = Depends(PERSON)
) -> SSEResponse:
    store = store_of(request)
    thread, relation = await accessible_thread(request, claims, thread_id)
    # The relation now decides the prompt, so a thread from before the caller's relation changed follows it.
    thread = {**thread, "role": relation}
    history = await store.start_turn(str(thread_id), body.content, HISTORY_TURNS)
    if history is None:  # purged with its classroom or its user since it was read
        raise failure(404, "NOT_FOUND", "Thread not found.")
    asked = TurnRequest(
        thread=thread,
        authorization=request.headers["authorization"],
        correlation=request.state.correlation_id,
        content=body.content,
        history=history,
        billed_to=billed(claims, relation),
    )
    state = request.app.state
    turn = Turn(state.services, store, state.settings, state.pending, asked)
    return SSEResponse(turn.events(), turn.finish)


@app.get("/v1/usage", response_model=Usage, responses=ERRORS)
async def usage(request: Request, claims: Claims = Depends(PERSON)) -> Any:
    """The caller's AI spend today against their daily allowance, from ai-core's ledger."""
    services: Services = request.app.state.services
    try:
        return await services.usage(billed(claims), request.state.correlation_id)
    except UpstreamError:
        raise failure(503, "DEPENDENCY_UNAVAILABLE", "AI usage is unavailable right now.") from None
