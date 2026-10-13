"""The question chatbot (ADR 0002 stage 4): students and teachers chat about a classroom's materials. Ported from
fsd_test's assistant. Reached through Kong at /api/chatbot with the user's own token."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
from fastapi import Depends, Query, Request
from learning_transport import ErrorResponse, actor, application, failure

import config
from chat import HISTORY_TURNS, SSEResponse, Turn, TurnRequest
from clients import Services, UpstreamError, billed
from contracts import MessageList, NewMessage, NewThread, Thread, ThreadList, Usage
from store import PostgresStore, Store

ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 503)
}
SAVE_GRACE = 10.0  # seconds the shutdown waits for interrupted answers still being saved


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    for name in ("OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_JWKS_URL"):
        if not os.environ.get(name):
            raise RuntimeError(f"Missing configuration: {name}")
    settings = config.load()
    store = await PostgresStore.open(settings.database_url)
    async with httpx.AsyncClient() as http:
        app.state.settings, app.state.store = settings, store
        app.state.services = Services(settings, http)
        app.state.pending = set()
        yield
        if app.state.pending:
            await asyncio.wait(app.state.pending, timeout=SAVE_GRACE)
    await store.close()


app = application("question-chatbot-agent", lifespan=lifespan)


def user_role(claims: dict[str, Any]) -> str:
    roles = claims.get("realm_access", {}).get("roles", [])
    if "teacher" in roles:
        return "teacher"
    if "student" in roles:
        return "student"
    raise failure(403, "FORBIDDEN", "Only students and teachers can use the chat.")


def store_of(request: Request) -> Store:
    store: Store = request.app.state.store
    return store


async def owned_thread(store: Store, claims: dict[str, Any], thread_id: UUID) -> dict[str, Any]:
    """The thread if the caller owns it; 404 otherwise, so ids don't leak."""
    thread = await store.thread(claims["org_id"], claims["sub"], str(thread_id))
    if thread is None:
        raise failure(404, "NOT_FOUND", "Thread not found.")
    return thread


@app.get("/health/ready")
async def ready(request: Request) -> dict[str, str]:
    try:
        await asyncio.wait_for(request.app.state.store.ping(), 2)
    except Exception:
        raise failure(503, "DEPENDENCY_UNAVAILABLE", "Database unavailable.") from None
    return {"status": "ready"}


@app.post("/v1/threads", status_code=201, response_model=Thread, responses=ERRORS)
async def create_thread(body: NewThread, request: Request, claims: dict[str, Any] = Depends(actor)) -> Any:
    role = user_role(claims)
    services: Services = request.app.state.services
    try:
        allowed = await services.classroom_access(
            request.headers["authorization"], str(body.classroom_id), request.state.correlation_id
        )
    except UpstreamError:
        raise failure(503, "DEPENDENCY_UNAVAILABLE", "Authorization is unavailable; retry later.") from None
    if not allowed:
        raise failure(403, "FORBIDDEN", "You do not have access to this classroom.")
    return await store_of(request).create_thread(
        claims["org_id"], claims["sub"], role, str(body.classroom_id)
    )


@app.get("/v1/threads", response_model=ThreadList, responses=ERRORS)
async def list_threads(
    request: Request, classroom_id: UUID | None = Query(default=None), claims: dict[str, Any] = Depends(actor)
) -> Any:
    user_role(claims)
    rows = await store_of(request).list_threads(
        claims["org_id"], claims["sub"], None if classroom_id is None else str(classroom_id)
    )
    return {"threads": rows}


@app.get("/v1/threads/{thread_id}/messages", response_model=MessageList, responses=ERRORS)
async def list_messages(thread_id: UUID, request: Request, claims: dict[str, Any] = Depends(actor)) -> Any:
    user_role(claims)
    store = store_of(request)
    await owned_thread(store, claims, thread_id)
    return {"messages": await store.messages(str(thread_id))}


@app.post(
    "/v1/threads/{thread_id}/messages",
    responses={**ERRORS, 200: {"description": "The answer as server-sent events: token, citations, done or error",
                               "content": {"text/event-stream": {}}}},
)  # fmt: skip
async def post_message(
    thread_id: UUID, body: NewMessage, request: Request, claims: dict[str, Any] = Depends(actor)
) -> SSEResponse:
    user_role(claims)
    store = store_of(request)
    thread = await owned_thread(store, claims, thread_id)
    history = await store.start_turn(str(thread_id), body.content, HISTORY_TURNS)
    asked = TurnRequest(
        thread=thread,
        authorization=request.headers["authorization"],
        correlation=request.state.correlation_id,
        content=body.content,
        history=history,
        billed_to=billed(claims),
    )
    state = request.app.state
    turn = Turn(state.services, store, state.settings, state.pending, asked)
    return SSEResponse(turn.events())


@app.get("/v1/usage", response_model=Usage, responses=ERRORS)
async def usage(request: Request, claims: dict[str, Any] = Depends(actor)) -> Any:
    """The caller's AI spend today against their daily allowance, from ai-core's ledger."""
    user_role(claims)
    services: Services = request.app.state.services
    try:
        return await services.usage(billed(claims), request.state.correlation_id)
    except UpstreamError:
        raise failure(503, "DEPENDENCY_UNAVAILABLE", "AI usage is unavailable right now.") from None
