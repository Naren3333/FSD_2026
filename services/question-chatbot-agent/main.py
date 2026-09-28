import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from fastapi import Depends
from fastapi.responses import JSONResponse
from learning_transport import ErrorResponse, actor, application, require_role, unavailable
from contracts import RequestContract, ResponseContract


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    for name in ("OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_JWKS_URL"):
        if not os.environ.get(name):
            raise RuntimeError(f"Missing configuration: {name}")
    yield


app = application("question-chatbot-agent", lifespan=lifespan)


@app.get("/health/ready")
def ready() -> dict[str, str]:
    return {"status": "ready", "capability": "not_implemented"}


@app.post(
    "/v1/grounded_question_answering",
    response_model=ResponseContract,
    responses={501: {"model": ErrorResponse}},
)
def execute(request: RequestContract, claims: dict[str, Any] = Depends(actor)) -> JSONResponse:
    require_role(claims, "ai-client")
    # References are untrusted until future workflow authorization resolves them.
    # No private data is fetched and no inference is attempted in this milestone.
    return unavailable("grounded_question_answering")
