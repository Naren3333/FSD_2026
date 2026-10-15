"""The FastAPI application every Python service starts from: error envelopes, request telemetry, liveness."""

import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from learning_transport import logs
from learning_transport.auth import AI_CLIENT, Claims, requires
from learning_transport.errors import ErrorResponse, unavailable

OIDC_SETTINGS = ("OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_JWKS_URL")


def require_env(*names: str) -> None:
    """Fail startup when any of these environment variables is unset or empty."""
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Missing configuration: " + ", ".join(missing))


def application(name: str, **kwargs: Any) -> FastAPI:
    app = FastAPI(title=name, version="1.0.0", **kwargs)
    logger = logging.getLogger(name)
    logs.configure(name)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        detail = (
            error.detail
            if isinstance(error.detail, dict)
            else {"code": "HTTP_ERROR", "message": str(error.detail)}
        )
        return JSONResponse(status_code=error.status_code, content={"error": detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        # Never include Pydantic's input value: it can contain confidential documents.
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request does not match the versioned contract.",
                }
            },
        )

    @app.middleware("http")
    async def telemetry(request: Request, call_next: Any) -> Any:
        start = time.monotonic()
        try:
            correlation = str(UUID(request.headers.get("x-correlation-id", "")))
        except ValueError:
            correlation = str(uuid4())
        request_id = str(uuid4())
        request.state.correlation_id = correlation
        ids = {"request_id": request_id, "correlation_id": correlation}
        try:
            response = await call_next(request)
        except Exception:
            logger.error("request_failed", extra={**ids, "error_category": "internal_error"})
            response = JSONResponse(
                status_code=500,
                content={
                    "error": {"code": "INTERNAL_ERROR", "message": "The operation could not be completed."}
                },
            )
        response.headers["X-Correlation-ID"] = correlation
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request",
            extra={
                **ids,
                "duration_ms": round((time.monotonic() - start) * 1000),
                "status": response.status_code,
                "error_category": "http_error" if response.status_code >= 400 else None,
            },
        )
        return response

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    return app


def placeholder_app(
    name: str, path: str, capability: str, request_model: type[BaseModel], response_model: type[BaseModel]
) -> FastAPI:
    """An AI agent whose capability is not authorized yet: `POST path` validates the versioned request contract
    for an ai-client caller, then answers 501 for `capability`. No private data is fetched and no inference is
    attempted; references in the request stay untrusted until a future workflow authorizes and resolves them."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        require_env(*OIDC_SETTINGS)
        yield

    app = application(name, lifespan=lifespan)

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        return {"status": "ready", "capability": "not_implemented"}

    @app.post(path, response_model=response_model, responses={501: {"model": ErrorResponse}})
    def execute(
        request: request_model,  # type: ignore[valid-type]
        _caller: Claims = Depends(requires(AI_CLIENT)),
    ) -> JSONResponse:
        return unavailable(capability)

    return app
