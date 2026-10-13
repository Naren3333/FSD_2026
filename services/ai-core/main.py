import asyncio
import base64
import json
import logging
import os
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NoReturn

import openai
import openai_client
import rerank
from contracts import (
    IngestChunk,
    IngestFigure,
    IngestResponse,
    RequestContract,
    RerankRequest,
    RerankResponse,
    ResponseContract,
)
from fastapi import Depends, Query, Request
from fastapi.responses import StreamingResponse
from ingest import IngestFailure, costs, ingest, vision_config
from learning_transport import ErrorResponse, actor, application, failure, require_role
from settings import get_settings

log = logging.getLogger("ai-core")

MAX_PDF_BYTES = 50 * 1024 * 1024
INGEST_CONCURRENCY = 2  # PDFs at once; more callers wait for a slot
ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 413, 415, 422, 502, 503)
}

_ingest_slots: asyncio.Semaphore | None = None


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    global _ingest_slots
    for name in ("OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_JWKS_URL"):
        if not os.environ.get(name):
            raise RuntimeError(f"Missing configuration: {name}")
    _ingest_slots = asyncio.Semaphore(INGEST_CONCURRENCY)
    yield
    await rerank.aclose()


app = application("ai-core", lifespan=lifespan)


@app.get("/health/ready")
def ready() -> dict[str, str]:
    # Only whether keys are configured: probing paid APIs on every health check would cost money and latency.
    settings = get_settings()
    return {
        "status": "ready",
        "openai": "configured" if settings.openai_api_key else "not_configured",
        "reranker": "configured" if settings.voyage_api_key else "not_configured",
    }


def provider_error(error: Exception) -> NoReturn:
    """Provider failures as errors the caller can retry or fall back on; never a made-up result."""
    if isinstance(error, openai_client.NotConfigured | rerank.RerankNotConfigured):
        raise failure(503, "AI_PROVIDER_NOT_CONFIGURED", "The AI provider is not configured.")
    if isinstance(error, openai_client.StructuredOutputError):
        raise failure(502, "AI_OUTPUT_REJECTED", "The model did not return a usable reply.")
    log.warning(
        json.dumps({"service": "ai-core", "error_category": "provider", "error": type(error).__name__})
    )
    raise failure(502, "AI_PROVIDER_FAILED", "The AI provider could not complete the request.")


PROVIDER_ERRORS = (
    openai.OpenAIError,
    openai_client.NotConfigured,
    openai_client.StructuredOutputError,
    rerank.RerankNotConfigured,
    rerank.RerankFailed,
)


@app.post("/v1/inference", response_model=ResponseContract, responses=ERRORS)
async def execute(request: RequestContract, claims: dict[str, Any] = Depends(actor)) -> Any:
    require_role(claims, "ai-client")
    settings = get_settings()
    try:
        if request.operation == "embed":
            vectors, tokens = await openai_client.embed([request.input or ""])
            return ResponseContract(
                vectors=vectors, model_version=settings.embedding_model, usage_tokens=tokens
            )
        messages = (
            [m.model_dump() for m in request.messages]
            if request.messages
            else [{"role": "user", "content": request.input}]
        )
        if request.stream:
            # Opened before the response starts, so a provider failure is still a plain error status.
            stream = openai_client.stream_chat(messages, request.max_tokens)
            first = await anext(stream)
            return StreamingResponse(events(first, stream), media_type="text/event-stream")
        reply = await openai_client.generate(
            messages, request.max_tokens, request.response_schema, name=request.schema_name or ""
        )
    except PROVIDER_ERRORS as error:
        provider_error(error)
    return ResponseContract(
        output=reply.text,
        model_version=settings.chat_model,
        usage_tokens=total(reply.usage),
        finish_reason=reply.finish_reason,
    )


def total(usage: openai_client.Usage) -> int | None:
    if usage.tokens_in is None or usage.tokens_out is None:
        return None
    return usage.tokens_in + usage.tokens_out


def event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def events(
    first: str | openai_client.Usage, stream: AsyncGenerator[str | openai_client.Usage, None]
) -> AsyncIterator[str]:
    """`delta` events with the text, then `done` with the usage; `error` if the provider fails mid-stream."""
    model = get_settings().chat_model
    item: str | openai_client.Usage | None = first
    try:
        while item is not None:
            if isinstance(item, openai_client.Usage):
                yield event("done", {"model_version": model, "usage_tokens": total(item)})
                return
            yield event("delta", {"text": item})
            item = await anext(stream, None)
    except openai.OpenAIError as error:
        log.warning(
            json.dumps({"service": "ai-core", "error_category": "provider", "error": type(error).__name__})
        )
        yield event("error", {"code": "AI_PROVIDER_FAILED", "message": "The AI provider stopped responding."})
    finally:
        await stream.aclose()


@app.post("/v1/rerank", response_model=RerankResponse, responses=ERRORS)
async def rerank_documents(request: RerankRequest, claims: dict[str, Any] = Depends(actor)) -> RerankResponse:
    require_role(claims, "ai-client")
    try:
        scores, tokens = await rerank.rerank(request.query, request.documents)
    except PROVIDER_ERRORS as error:
        provider_error(error)
    return RerankResponse(scores=scores, model_version=get_settings().reranker_model, usage_tokens=tokens)


PDF_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
    }
}


@app.post("/v1/ingest", response_model=IngestResponse, responses=ERRORS, openapi_extra=PDF_BODY)
async def ingest_pdf(
    request: Request,
    title: str = Query(min_length=1, max_length=300),
    claims: dict[str, Any] = Depends(actor),
) -> IngestResponse:
    """One PDF (the raw body) → chunks with embeddings and the figure crops they point at. Stateless: the caller
    stores the result. A document problem is a `failed` outcome; a provider outage is an error to retry."""
    require_role(claims, "ai-client")
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/pdf":
        raise failure(415, "UNSUPPORTED_MEDIA_TYPE", "Send the PDF as application/pdf.")
    assert _ingest_slots is not None
    try:
        openai_client.get_client()  # without a key every model call would fail: refuse before any work
    except openai_client.NotConfigured as error:
        provider_error(error)
    with tempfile.TemporaryDirectory(prefix="ingest-") as folder:
        path = Path(folder) / "document.pdf"
        await receive(request, path)
        with costs.recording(getattr(request.state, "correlation_id", "-")) as record:
            record.outcome = (
                "error"  # until it ends otherwise; an unexpected error is a 500 for the caller to retry
            )
            try:
                async with _ingest_slots:
                    with costs.stage("total"):
                        result = await ingest(path, title, vision_config(get_settings()))
                record.outcome = "indexed"
            except IngestFailure as e:
                record.outcome, record.failure_reason = "failed", e.reason
                return IngestResponse(outcome="failed", failure_reason=e.reason, failure_detail=str(e)[:2000])  # type: ignore[arg-type]
            except PROVIDER_ERRORS as error:
                provider_error(error)
            finally:
                costs.write(record, None)
    return IngestResponse(
        outcome="indexed",
        page_count=result.pages,
        ocr_page_count=result.ocr_pages,
        embedding_model=result.embedding_model,
        chunks=[
            IngestChunk(
                ordinal=c.ordinal,
                content=c.content,
                embed_text=text,
                section_path=c.section_path,
                page_start=c.page_start,
                page_end=c.page_end,
                token_count=c.token_count,
                kind=c.kind,  # type: ignore[arg-type]
                source=c.source,  # type: ignore[arg-type]
                image=c.image,
                label=c.label,
                cites=list(c.cites),
                embedding=vector,
            )
            for c, text, vector in zip(result.chunks, result.embed_texts, result.vectors, strict=True)
        ],
        figures=[
            IngestFigure(image=name, png_base64=base64.b64encode(png).decode())
            for name, png in result.figures.items()
        ],
    )


async def receive(request: Request, path: Path) -> None:
    """Stream the body to disk, refusing more than MAX_PDF_BYTES without reading it all into memory."""
    size = 0
    with path.open("wb") as out:
        async for part in request.stream():
            size += len(part)
            if size > MAX_PDF_BYTES:
                raise failure(413, "PAYLOAD_TOO_LARGE", "The PDF is larger than 50 MiB.")
            out.write(part)
    if size == 0:
        raise failure(422, "VALIDATION_ERROR", "The PDF is empty.")
