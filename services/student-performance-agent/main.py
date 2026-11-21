"""performance interpretation (adr 0014): a classroom's teacher asks the agent to interpret one student's finalized results; it investigates with read-only tools and drafts observations, possible misconceptions, interventions and topics to practise, each checked against the evidence. class insights (adr 0021): the teacher asks one question about the whole class and gets a checked answer on the same loop. nothing is saved and no grade changes."""

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
from class_agent import ClassAgent
from clients import Services
from contracts import ClassInsightsRequest, ClassInsightsResponse, RequestContract, ResponseContract

# 504 is ai-core's AI_TIMEOUT, which either route can pass on.
ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 422, 429, 502, 503, 504)
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

# teachers only; students and service accounts may not (adrs 0014, 0021).
TEACHER_ONLY = requires(TEACHER)


@app.get("/health/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready"}


@app.post("/v1/performance_interpretation", response_model=ResponseContract, responses=ERRORS)
async def interpret(body: RequestContract, request: Request, claims: Claims = Depends(TEACHER_ONLY)) -> Any:
    """a draft interpretation of the student's finalized results, for their teacher to review; nothing is saved."""
    state = request.app.state
    return await Agent(state.services, state.settings, request, claims).interpret(body)


@app.post("/v1/class_insights", response_model=ClassInsightsResponse, responses=ERRORS)
async def class_insights(
    body: ClassInsightsRequest, request: Request, claims: Claims = Depends(TEACHER_ONLY)
) -> Any:
    """a draft answer to the teacher's question about their class, for them to review; students are named only after the checks, and nothing is saved."""
    state = request.app.state
    return await ClassAgent(state.services, state.settings, request, claims).answer(body)
