"""performance interpretation (adr 0014): a classroom's teacher asks the agent to interpret one student's finalized results; it investigates with read-only tools and drafts observations, possible misconceptions, interventions and topics to practise, each checked against the evidence. nothing is saved and no grade changes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, Request
from learning_transport import (
    OIDC_SETTINGS,
    TEACHER,
    Claims,
    ErrorResponse,
    application,
    require_env,
    requires,
)

import config
from agent import Agent
from clients import Services
from contracts import RequestContract, ResponseContract

ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 422, 429, 502, 503)
}


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    require_env(*OIDC_SETTINGS)
    settings = config.load()
    async with httpx.AsyncClient() as http:
        app.state.settings = settings
        app.state.services = Services(settings, http)
        yield


app = application("student-performance-agent", lifespan=lifespan)

# teachers only; students and service accounts may not (adr 0014).
TEACHER_ONLY = requires(TEACHER)


@app.get("/health/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready"}


@app.post("/v1/performance_interpretation", response_model=ResponseContract, responses=ERRORS)
async def interpret(body: RequestContract, request: Request, claims: Claims = Depends(TEACHER_ONLY)) -> Any:
    """a draft interpretation of the student's finalized results, for their teacher to review; nothing is saved."""
    state = request.app.state
    return await Agent(state.services, state.settings, request, claims).interpret(body)
