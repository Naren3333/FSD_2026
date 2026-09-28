"""Shared authentication, errors and request telemetry. No business models."""

import json
import logging
import os
import time
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(Contract):
    code: str
    message: str
    capability: str | None = None


class ErrorResponse(Contract):
    error: ErrorBody


def failure(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


@lru_cache
def keyset() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(os.environ["OIDC_JWKS_URL"], timeout=5, lifespan=300)


def actor(request: Request) -> dict[str, Any]:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise failure(401, "UNAUTHENTICATED", "Sign in to continue.")
    try:
        token = auth[7:]
        key = keyset().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=os.environ["OIDC_AUDIENCE"],
            issuer=os.environ["OIDC_ISSUER"],
            options={"require": ["exp", "iat", "sub", "org_id"]},
        )
    except (jwt.PyJWTError, jwt.PyJWKClientError):
        raise failure(
            401, "UNAUTHENTICATED", "The session is invalid or expired."
        ) from None
    if not claims.get("sub") or not claims.get("org_id"):
        raise failure(401, "UNAUTHENTICATED", "Identity claims are missing.")
    return dict(claims)


def require_role(claims: dict[str, Any], role: str) -> None:
    if role not in claims.get("realm_access", {}).get("roles", []):
        raise failure(403, "FORBIDDEN", "Your role cannot perform this operation.")


def unavailable(capability: str) -> JSONResponse:
    return JSONResponse(
        status_code=501,
        content={
            "error": {
                "code": "AI_CAPABILITY_NOT_IMPLEMENTED",
                "message": "This AI capability is not implemented in the foundation milestone.",
                "capability": capability,
            }
        },
    )


def application(name: str, **kwargs: Any) -> FastAPI:
    app = FastAPI(title=name, version="1.0.0", **kwargs)
    logger = logging.getLogger(name)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        detail = (
            error.detail
            if isinstance(error.detail, dict)
            else {"code": "HTTP_ERROR", "message": str(error.detail)}
        )
        return JSONResponse(status_code=error.status_code, content={"error": detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
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
        try:
            response = await call_next(request)
        except Exception:
            logger.error(
                json.dumps(
                    {
                        "service": name,
                        "request_id": request_id,
                        "correlation_id": correlation,
                        "error_category": "internal_error",
                    }
                )
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "The operation could not be completed.",
                    }
                },
            )
        response.headers["X-Correlation-ID"] = correlation
        response.headers["X-Request-ID"] = request_id
        logger.info(
            json.dumps(
                {
                    "service": name,
                    "request_id": request_id,
                    "correlation_id": correlation,
                    "duration_ms": round((time.monotonic() - start) * 1000),
                    "status": response.status_code,
                    "error_category": "http_error"
                    if response.status_code >= 400
                    else None,
                }
            )
        )
        return response

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    return app
