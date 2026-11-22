"""question generation (adr 0006): a classroom's teacher generates assessment questions, or marking guidance for a question they wrote, from the classroom's documents they choose; an enrolled student generates practice questions for themselves (adr 0022)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import Depends, Request
from learning_transport import (
    OIDC_SETTINGS,
    STUDENT,
    TEACHER,
    Claims,
    ErrorResponse,
    application,
    require_env,
    requires,
)

import config
from clients import Services
from contracts import (
    GenerateRequest,
    GenerateResponse,
    Guidance,
    GuidanceRequest,
    PracticeRequest,
    PracticeResponse,
)
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
# practice is a student's own; teachers and service accounts may not (adr 0022).
STUDENT_ONLY = requires(STUDENT)


def pipeline(request: Request, claims: Claims) -> Pipeline:
    state = request.app.state
    return Pipeline(state.services, request, claims, state.settings)


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


@app.post("/v1/practice", response_model=PracticeResponse, responses=ERRORS)
async def practice(body: PracticeRequest, request: Request, claims: Claims = Depends(STUDENT_ONLY)) -> Any:
    """reviewed practice questions with their keys, from the classroom's documents the student chooses, for their own ungraded use; nothing is saved."""
    return await pipeline(request, claims).practice(body)
