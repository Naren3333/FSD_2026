import asyncio
import base64
import contextlib
import logging
import os
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NoReturn

import openai
from fastapi import Depends, Query, Request
from fastapi.responses import StreamingResponse
from learning_transport import (
    AI_CLIENT,
    OIDC_SETTINGS,
    Claims,
    ErrorResponse,
    application,
    failure,
    require_env,
    requires,
    sse_event,
)

import openai_client
import rerank
import spend
from contracts import (
    IngestChunk,
    IngestFigure,
    IngestResponse,
    RequestContract,
    RerankRequest,
    RerankResponse,
    ResponseContract,
)
from ingest import IngestError, costs, ingest, vision_config
from settings import get_settings

log = logging.getLogger("ai-core")

MAX_PDF_BYTES = 50 * 1024 * 1024
INGEST_CONCURRENCY = 2  # PDFs at once; more callers wait for a slot
DISCONNECT_POLL = 2.0  # seconds between checks that an ingest's caller is still there
ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 413, 415, 422, 429, 502, 503)
}

_ingest_slots: asyncio.Semaphore | None = None
_recordings: set[asyncio.Future[None]] = set()  # ledger writes that must outlive a closed stream


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    global _ingest_slots
    require_env(*OIDC_SETTINGS)
    _ingest_slots = asyncio.Semaphore(INGEST_CONCURRENCY)
    opened = getattr(app.state, "ledger", None) is None
    if opened:
        require_env("DATABASE_URL")
        app.state.ledger = await spend.PostgresLedger.open(os.environ["DATABASE_URL"])
    yield
    await rerank.aclose()
    if _recordings:
        await asyncio.wait(_recordings, timeout=10)
    if opened:
        await app.state.ledger.close()
        del app.state.ledger


app = application("ai-core", lifespan=lifespan)


@app.get("/health/ready")
async def ready(request: Request) -> dict[str, str]:
    # Only whether keys are configured: probing paid APIs on every health check would cost money and latency.
    try:
        await asyncio.wait_for(ledger_of(request).ping(), 2)
    except Exception:
        raise failure(503, "DEPENDENCY_UNAVAILABLE", "The usage ledger is unavailable.") from None
    settings = get_settings()
    return {
        "status": "ready",
        "openai": "configured" if settings.openai_api_key else "not_configured",
        "reranker": "configured" if settings.voyage_api_key else "not_configured",
    }


def provider_error(error: Exception) -> NoReturn:
    """Provider failures as errors the caller can retry or fall back on; never a made-up result."""
    if isinstance(error, openai_client.NotConfiguredError | rerank.RerankNotConfiguredError):
        raise failure(503, "AI_PROVIDER_NOT_CONFIGURED", "The AI provider is not configured.")
    if isinstance(error, openai_client.StructuredOutputError):
        raise failure(502, "AI_OUTPUT_REJECTED", "The model did not return a usable reply.")
    log.warning("provider_failed", extra={"error": type(error).__name__})
    raise failure(502, "AI_PROVIDER_FAILED", "The AI provider could not complete the request.")


PROVIDER_ERRORS = (
    openai.OpenAIError,
    openai_client.NotConfiguredError,
    openai_client.StructuredOutputError,
    rerank.RerankNotConfiguredError,
    rerank.RerankError,
)


def ledger_of(request: Request) -> spend.Ledger:
    ledger: spend.Ledger = request.app.state.ledger
    return ledger


async def allowance(request: Request, billed: spend.Billed) -> None:
    """Refuse before any paid work once the user's daily allowance is used up. Without a working ledger nothing
    runs: a call nobody can account for is not made."""
    try:
        usage = await ledger_of(request).today(billed)
    except Exception:
        log.error("ledger_unavailable")
        raise failure(503, "USAGE_LEDGER_UNAVAILABLE", "AI usage cannot be checked right now.") from None
    if spend.over_limit(usage):
        raise spend.limit_reached(usage)


def charge(request: Request, billed: spend.Billed, *charges: spend.Charge) -> None:
    """Record what a call cost. The write runs as its own task so a caller hanging up (a closed stream) cannot
    cancel it; a failed write is logged, since the money is already spent."""
    if not charges:
        return
    ledger, correlation = ledger_of(request), getattr(request.state, "correlation_id", "-")

    async def write() -> None:
        for c in charges:
            try:
                await ledger.record(billed, c, correlation)
            except Exception:
                log.error("ledger_write_failed", extra={"operation": c.operation, "cost_usd": c.cost_usd})

    task = asyncio.ensure_future(write())
    _recordings.add(task)
    task.add_done_callback(_recordings.discard)


def priced(
    operation: str, model: str, tokens_in: int, tokens_out: int = 0, estimated: bool = False
) -> spend.Charge:
    return spend.Charge(
        operation, model, tokens_in, tokens_out, spend.cost(model, tokens_in, tokens_out), estimated
    )


def message_text(messages: list[dict[str, Any]]) -> str:
    return "".join(str(m.get("content", "")) for m in messages)


@app.get("/v1/usage", response_model=spend.Usage, responses=ERRORS)
async def usage_today(
    request: Request,
    user_id: str = Query(min_length=1, max_length=200),
    org_id: str = Query(min_length=1, max_length=200),
    role: spend.Role = Query(),
    _caller: Claims = Depends(requires(AI_CLIENT)),
) -> spend.Usage:
    """A user's spend today, their daily limit and when it resets, for a service showing it to that user."""
    try:
        return await ledger_of(request).today(spend.Billed(user_id=user_id, org_id=org_id, role=role))
    except Exception:
        raise failure(503, "USAGE_LEDGER_UNAVAILABLE", "AI usage cannot be checked right now.") from None


@app.post("/v1/inference", response_model=ResponseContract, responses=ERRORS)
async def execute(
    request: RequestContract, http: Request, _caller: Claims = Depends(requires(AI_CLIENT))
) -> Any:
    settings = get_settings()
    billed = request.billed_to
    await allowance(http, billed)
    try:
        if request.operation == "embed":
            vectors, tokens = await openai_client.embed([request.input or ""])
            charge(http, billed, priced("embed", settings.embedding_model, tokens))
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
            bill = StreamBill(http, billed, settings.chat_model, message_text(messages))
            return StreamingResponse(events(first, stream, bill), media_type="text/event-stream")
        reply = await openai_client.generate(
            messages, request.max_tokens, request.response_schema, name=request.schema_name or ""
        )
    except PROVIDER_ERRORS as error:
        provider_error(error)
    tokens_in, tokens_out = reply.usage.tokens_in, reply.usage.tokens_out
    charge(
        http,
        billed,
        priced(
            "generate",
            settings.chat_model,
            spend.estimate_tokens(message_text(messages)) if tokens_in is None else tokens_in,
            spend.estimate_tokens(reply.text) if tokens_out is None else tokens_out,
            tokens_in is None or tokens_out is None,
        ),
    )
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


class StreamBill:
    """Charges a streamed reply once, however it ends: with OpenAI's counts when the usage chunk arrived, or an
    estimate from the text when the stream broke or the caller hung up first."""

    def __init__(self, request: Request, billed: spend.Billed, model: str, prompt: str) -> None:
        self.request, self.billed, self.model, self.prompt = request, billed, model, prompt
        self.text: list[str] = []
        self.done = False

    def close(self, usage: openai_client.Usage | None = None) -> None:
        if self.done:
            return
        self.done = True
        tokens_in = usage.tokens_in if usage else None
        tokens_out = usage.tokens_out if usage else None
        exact = tokens_in is not None and tokens_out is not None
        charge(
            self.request,
            self.billed,
            priced(
                "stream",
                self.model,
                spend.estimate_tokens(self.prompt) if tokens_in is None else tokens_in,
                spend.estimate_tokens("".join(self.text)) if tokens_out is None else tokens_out,
                not exact,
            ),
        )


async def events(
    first: str | openai_client.Usage,
    stream: AsyncGenerator[str | openai_client.Usage, None],
    bill: StreamBill | None = None,
) -> AsyncIterator[str]:
    """`delta` events with the text, then `done` with the usage; `error` if the provider fails mid-stream."""
    model = get_settings().chat_model
    item: str | openai_client.Usage | None = first
    try:
        while item is not None:
            if isinstance(item, openai_client.Usage):
                if bill:
                    bill.close(item)
                yield sse_event("done", {"model_version": model, "usage_tokens": total(item)})
                return
            if bill:
                bill.text.append(item)
            yield sse_event("delta", {"text": item})
            item = await anext(stream, None)
    except openai.OpenAIError as error:
        log.warning("provider_failed", extra={"error": type(error).__name__})
        yield sse_event(
            "error", {"code": "AI_PROVIDER_FAILED", "message": "The AI provider stopped responding."}
        )
    finally:
        if bill:
            bill.close()
        await stream.aclose()


@app.post("/v1/rerank", response_model=RerankResponse, responses=ERRORS)
async def rerank_documents(
    request: RerankRequest, http: Request, _caller: Claims = Depends(requires(AI_CLIENT))
) -> RerankResponse:
    await allowance(http, request.billed_to)
    try:
        scores, tokens = await rerank.rerank(request.query, request.documents)
    except PROVIDER_ERRORS as error:
        provider_error(error)
    model = get_settings().reranker_model
    if tokens is None:
        # Voyage counts the query once per document.
        guess = sum(
            spend.estimate_tokens(request.query) + spend.estimate_tokens(d) for d in request.documents
        )
        charge(http, request.billed_to, priced("rerank", model, guess, estimated=True))
    else:
        charge(http, request.billed_to, priced("rerank", model, tokens))
    return RerankResponse(scores=scores, model_version=model, usage_tokens=tokens)


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
    billed_user: str = Query(min_length=1, max_length=200),
    billed_org: str = Query(min_length=1, max_length=200),
    billed_role: spend.Role = Query(),
    _caller: Claims = Depends(requires(AI_CLIENT)),
) -> IngestResponse:
    """One PDF (the raw body) → chunks with embeddings and the figure crops they point at. Stateless: the caller
    stores the result. A document problem is a `failed` outcome; a provider outage is an error to retry. The
    model calls are charged to `billed_user` (the teacher who uploaded the document), whatever the outcome."""
    billed = spend.Billed(user_id=billed_user, org_id=billed_org, role=billed_role)
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/pdf":
        raise failure(415, "UNSUPPORTED_MEDIA_TYPE", "Send the PDF as application/pdf.")
    assert _ingest_slots is not None
    try:
        openai_client.get_client()  # without a key every model call would fail: refuse before any work
    except openai_client.NotConfiguredError as error:
        provider_error(error)
    await allowance(request, billed)
    with tempfile.TemporaryDirectory(prefix="ingest-") as folder:
        path = Path(folder) / "document.pdf"
        await receive(request, path)
        with costs.recording(getattr(request.state, "correlation_id", "-")) as record:
            record.outcome = (
                "error"  # until it ends otherwise; an unexpected error is a 500 for the caller to retry
            )
            try:
                result = await unless_disconnected(request, _ingest(path, title))
                record.outcome = "indexed"
            except IngestError as e:
                record.outcome, record.failure_reason = "failed", e.reason
                return IngestResponse(outcome="failed", failure_reason=e.reason, failure_detail=str(e)[:2000])  # type: ignore[arg-type]
            except PROVIDER_ERRORS as error:
                provider_error(error)
            except CallerGoneError:
                record.outcome = "abandoned"
                log.info("ingest_abandoned")
                raise failure(
                    503, "CALLER_GONE", "The caller disconnected before the ingest finished."
                ) from None
            finally:
                costs.write(record, None)
                charge(request, billed, *ingest_charges(record))
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


def ingest_charges(record: costs.IngestCost) -> list[spend.Charge]:
    """An ingest's model calls, one charge per model: the vision passes and the chunk embeddings."""
    by_model: dict[str, list[int]] = {}
    for calls in record.kinds.values():
        if calls.model and calls.calls:
            totals = by_model.setdefault(calls.model, [0, 0, 0])
            totals[0] += calls.prompt_tokens
            totals[1] += calls.cached_tokens
            totals[2] += calls.completion_tokens
    out = [
        spend.Charge("ingest", model, t_in, t_out, spend.cost(model, t_in, t_out, cached))
        for model, (t_in, cached, t_out) in by_model.items()
    ]
    if record.embed_tokens:
        model = get_settings().embedding_model
        out.append(
            spend.Charge("ingest", model, record.embed_tokens, 0, spend.cost(model, record.embed_tokens))
        )
    return out


async def _ingest(path: Path, title: str) -> Any:
    assert _ingest_slots is not None
    async with _ingest_slots:
        with costs.stage("total"):
            return await ingest(path, title, vision_config(get_settings()))


class CallerGoneError(Exception):
    """The caller hung up before the work finished."""


async def unless_disconnected[T](
    request: Request, work: Coroutine[Any, Any, T], poll: float = DISCONNECT_POLL
) -> T:
    """Await `work`, cancelling it if the caller disconnects first. Content gives up on an ingest after 20 minutes
    and retries it later; without this the abandoned run would keep a slot and keep paying for model calls."""
    task = asyncio.ensure_future(work)
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=poll)
            if not task.done() and await request.is_disconnected():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise CallerGoneError()
        return task.result()
    finally:
        task.cancel()


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
