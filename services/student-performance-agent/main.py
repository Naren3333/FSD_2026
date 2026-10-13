from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends
from fastapi.responses import JSONResponse
from learning_transport import (
    AI_CLIENT,
    OIDC_SETTINGS,
    Claims,
    ErrorResponse,
    application,
    require_env,
    requires,
    unavailable,
)

from contracts import RequestContract, ResponseContract


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    require_env(*OIDC_SETTINGS)
    yield


app = application("student-performance-agent", lifespan=lifespan)


@app.get("/health/ready")
def ready() -> dict[str, str]:
    return {"status": "ready", "capability": "not_implemented"}


@app.post(
    "/v1/performance_interpretation",
    response_model=ResponseContract,
    responses={501: {"model": ErrorResponse}},
)
def execute(request: RequestContract, _caller: Claims = Depends(requires(AI_CLIENT))) -> JSONResponse:
    # References are untrusted until future workflow authorization resolves them.
    # No private data is fetched and no inference is attempted until this capability is authorized.
    return unavailable("performance_interpretation")
