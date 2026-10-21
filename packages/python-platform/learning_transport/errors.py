"""contracts and error envelopes shared by every service: errors are {\"error\": {\"code\", \"message\"}}."""

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    """a request or response body that rejects fields it does not declare."""

    model_config = ConfigDict(extra="forbid")


class ErrorBody(Contract):
    code: str
    message: str
    capability: str | None = None


class ErrorResponse(Contract):
    error: ErrorBody


def failure(status: int, code: str, message: str) -> HTTPException:
    """an http error the application's handler renders as the standard envelope."""
    return HTTPException(status, {"code": code, "message": message})


def unavailable(capability: str) -> JSONResponse:
    """501 for an ai capability whose implementation has not been authorized yet."""
    return JSONResponse(
        status_code=501,
        content={
            "error": {
                "code": "AI_CAPABILITY_NOT_IMPLEMENTED",
                "message": "This AI capability is not implemented yet.",
                "capability": capability,
            }
        },
    )


class UpstreamError(Exception):
    """carry upstream error details, including allowance reset times; zero status means unreachable."""

    def __init__(self, service: str, status: int, code: str = "", resets_at: str | None = None) -> None:
        super().__init__(f"{service} {status} {code}".strip())
        self.service, self.status, self.code, self.resets_at = service, status, code, resets_at


def upstream_error(service: str, resp: httpx.Response) -> UpstreamError:
    """a refused call as an upstreamerror: the service's error code and, for ai_spend_limit, when the allowance resets."""
    try:
        error = resp.json()["error"]
        code, resets_at = str(error.get("code") or ""), error.get("resets_at")
    except (ValueError, KeyError, TypeError, AttributeError):
        code, resets_at = "", None
    return UpstreamError(service, resp.status_code, code, str(resets_at) if resets_at else None)
