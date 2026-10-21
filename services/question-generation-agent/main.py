"""question generation (adr 0006): a classroom's teacher generates assessment questions, or marking guidance for a question they wrote, from the classroom's documents they choose."""

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
from clients import Services
from contracts import GenerateRequest, GenerateResponse, Guidance, GuidanceRequest
from pipeline import Pipeline

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


app = application("question-generation-agent", lifespan=lifespan)

# teachers only; students and service accounts may not (adr 0006).
TEACHER_ONLY = requires(TEACHER)


def pipeline(request: Request, claims: Claims) -> Pipeline:
    state = request.app.state
    return Pipeline(state.services, request, claims, state.settings.relevance_floor)


@app.get("/health/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready"}


@app.post("/v1/questions", response_model=GenerateResponse, responses=ERRORS)
async def generate(body: GenerateRequest, request: Request, claims: Claims = Depends(TEACHER_ONLY)) -> Any:
    """questions from the chosen documents, checked, for the teacher to review; nothing is saved."""
    return await pipeline(request, claims).generate(body)


@app.post("/v1/marking-guidance", response_model=Guidance, responses=ERRORS)
async def marking_guidance(
    body: GuidanceRequest, request: Request, claims: Claims = Depends(TEACHER_ONLY)
) -> Any:
    """a rubric or accepted answers, and marking points, for a question the teacher wrote; nothing is saved."""
    return await pipeline(request, claims).guidance(body)
