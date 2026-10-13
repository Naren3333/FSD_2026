"""Contracts and error envelopes shared by every service: errors are {"error": {"code", "message"}}."""

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict


class Contract(BaseModel):
    """A request or response body that rejects fields it does not declare."""

    model_config = ConfigDict(extra="forbid")


class ErrorBody(Contract):
    code: str
    message: str
    capability: str | None = None


class ErrorResponse(Contract):
    error: ErrorBody


def failure(status: int, code: str, message: str) -> HTTPException:
    """An HTTP error the application's handler renders as the standard envelope."""
    return HTTPException(status, {"code": code, "message": message})


def unavailable(capability: str) -> JSONResponse:
    """501 for an AI capability whose implementation has not been authorized yet."""
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
    """Another service answered with an error or could not be reached (status 0). `code` is its error code, if
    it sent one; `resets_at` comes with AI_SPEND_LIMIT: the user's daily AI allowance is used up until then."""

    def __init__(self, service: str, status: int, code: str = "", resets_at: str | None = None) -> None:
        super().__init__(f"{service} {status} {code}".strip())
        self.service, self.status, self.code, self.resets_at = service, status, code, resets_at
