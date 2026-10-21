import asyncio
import base64
import contextlib
import logging
import os
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NoReturn

import openai
from fastapi import Depends, HTTPException, Query, Request
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
from starlette.types import Receive, Scope, Send

import budget
import openai_client
import rerank
import spend
from billing import Billing, Slot, StreamBill, ingest_charges, ledger_unavailable, priced, reply_charge
from contracts import (
    IngestChunk,
    IngestFigure,
    IngestResponse,
    RequestContract,
    RerankRequest,
    RerankResponse,
    ResponseContract,
)
from ingest import IngestError, IngestResult, costs, ingest, passage_text, vision_config
from ingest.native import FORMATS, MARKDOWN
from settings import get_settings

log = logging.getLogger("ai-core")

MAX_PDF_BYTES = 50 * 1024 * 1024
INGEST_CONCURRENCY = 2  # PDFs at once; more callers wait for a slot
DISCONNECT_POLL = 2.0  # seconds between checks that an ingest's caller is still there
ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 413, 415, 422, 429, 502, 503)
}


@asynccontextmanager
async def lifespan(app: Any) -> AsyncIterator[None]:
    require_env(*OIDC_SETTINGS)
    app.state.ingest_slots = asyncio.Semaphore(INGEST_CONCURRENCY)
    opened = getattr(app.state, "ledger", None) is None
    if opened:
        require_env("DATABASE_URL")
        app.state.ledger = await spend.PostgresLedger.open(os.environ["DATABASE_URL"])
    app.state.billing = Billing(app.state.ledger)
    if isinstance(app.state.ledger, spend.PostgresLedger):
        app.state.metric_gauges = app.state.ledger.metrics
    yield
    await rerank.aclose()
    await app.state.billing.drain()
    if opened:
        await app.state.ledger.close()
        del app.state.ledger


app = application("ai-core", lifespan=lifespan)

# Only service accounts holding ai-client call ai-core; each call names the user it is billed to.
SERVICE = requires(AI_CLIENT)


@app.get("/health/ready", responses={503: {"model": ErrorResponse}})
async def ready(request: Request) -> dict[str, str]:
    # The ledger must answer (no call runs without it); for the providers only whether keys are configured:
    # probing paid APIs on every health check would cost money and latency.
    try:
        await asyncio.wait_for(billing_of(request).ledger.ping(), 2)
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


def billing_of(request: Request) -> Billing:
    billing: Billing = request.app.state.billing
    return billing


def correlation_of(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", "-"))


def total(usage: openai_client.Usage) -> int | None:
    if usage.tokens_in is None or usage.tokens_out is None:
        return None
    return usage.tokens_in + usage.tokens_out


def message_text(messages: list[dict[str, Any]]) -> str:
    return "".join(str(m.get("content", "")) for m in messages)


@app.get("/v1/usage", response_model=spend.Usage, responses=ERRORS)
async def usage_today(
    request: Request,
    user_id: str = Query(min_length=1, max_length=200),
    org_id: str = Query(min_length=1, max_length=200),
    role: spend.Role = Query(),
    _caller: Claims = Depends(SERVICE),
) -> spend.Usage:
    """A user's spend today, their daily limit and when it resets, for a service showing it to that user."""
    try:
        return await billing_of(request).ledger.today(spend.Billed(user_id=user_id, org_id=org_id, role=role))
    except Exception:
        raise ledger_unavailable() from None


@app.post("/v1/inference", response_model=ResponseContract, responses=ERRORS)
async def execute(request: RequestContract, http: Request, _caller: Claims = Depends(SERVICE)) -> Any:
    return await budgeted(http, request.billed_to, lambda: execute_reserved(request, http))


async def execute_reserved(request: RequestContract, http: Request) -> Any:
    slot = billing_of(http).claim(request.billed_to)
    try:
        result = await infer(request, http, slot)
    except BaseException:
        slot.release()
        raise
    if not isinstance(result, ClosingStream):
        slot.release()  # a stream frees its slot when it ends
    return result


async def infer(request: RequestContract, http: Request, slot: Slot) -> Any:
    settings = get_settings()
    billed, billing, correlation = request.billed_to, billing_of(http), correlation_of(http)
    await billing.check(billed)
    try:
        if request.operation == "embed":
            vectors, tokens = await openai_client.embed([request.input or ""])
            await billing.charge(billed, correlation, priced("embed", settings.embedding_model, tokens))
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
            bill = StreamBill(billing, billed, correlation, settings.chat_model, messages, request.max_tokens)

            async def finish() -> None:
                try:
                    await bill.close()
                finally:
                    try:
                        await stream.aclose()
                    finally:
                        slot.release()

            return ClosingStream(events(first, stream, bill), finish)
        reply = await openai_client.generate(
            messages, request.max_tokens, request.response_schema, name=request.schema_name or ""
        )
    except openai_client.StructuredOutputError as error:
        # OpenAI bills a refused or cut-off reply like any other: charge it, then refuse it.
        prompt = message_text(messages)
        await billing.charge(
            billed,
            correlation,
            reply_charge("generate", settings.chat_model, prompt, error.text, error.usage),
        )
        provider_error(error)
    except openai.APITimeoutError as error:
        # OpenAI may still finish, and bill, a reply nobody waited for: charge what it could cost (ADR 0003).
        if request.operation == "embed":
            embedded = [{"content": request.input}]
            await billing.charge_timeout(billed, correlation, "embed", settings.embedding_model, embedded, 0)
        else:
            operation = "stream" if request.stream else "generate"
            await billing.charge_timeout(
                billed, correlation, operation, settings.chat_model, messages, request.max_tokens
            )
        provider_error(error)
    except PROVIDER_ERRORS as error:
        provider_error(error)
    charge = reply_charge("generate", settings.chat_model, message_text(messages), reply.text, reply.usage)
    await billing.charge(billed, correlation, charge)
    return ResponseContract(
        output=reply.text,
        model_version=settings.chat_model,
        usage_tokens=total(reply.usage),
        finish_reason=reply.finish_reason,
    )


class ClosingStream(StreamingResponse):
    """Server-sent events that run `finish` however the response ends. A caller hanging up before the body starts
    means the events generator never runs, so its own `finally` can't bill the reply or close the provider
    stream."""

    def __init__(self, content: AsyncIterator[str], finish: Callable[[], Awaitable[None]]) -> None:
        super().__init__(content, media_type="text/event-stream")
        self.finish = finish

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.finish()


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
                    await bill.close(item)
                yield sse_event("done", {"model_version": model, "usage_tokens": total(item)})
                return
            if bill:
                bill.text.append(item)
            yield sse_event("delta", {"text": item})
            item = await anext(stream, None)
    except openai.OpenAIError as error:
        log.warning("provider_failed", extra={"error": type(error).__name__})
        if bill and isinstance(error, openai.APITimeoutError):
            await bill.timed_out()  # charged as a non-streamed timeout is
        yield sse_event(
            "error", {"code": "AI_PROVIDER_FAILED", "message": "The AI provider stopped responding."}
        )
    finally:
        try:
            if bill:
                await bill.close()
        finally:
            await stream.aclose()


@app.post("/v1/rerank", response_model=RerankResponse, responses=ERRORS)
async def rerank_documents(
    request: RerankRequest, http: Request, _caller: Claims = Depends(SERVICE)
) -> RerankResponse:
    return await budgeted(http, request.billed_to, lambda: rerank_reserved(request, http))


async def rerank_reserved(request: RerankRequest, http: Request) -> RerankResponse:
    billing = billing_of(http)
    slot = billing.claim(request.billed_to)
    try:
        return await rerank_billed(request, http, billing)
    finally:
        slot.release()  # after the charge, so the user's next call is checked against it


async def rerank_billed(request: RerankRequest, http: Request, billing: Billing) -> RerankResponse:
    await billing.check(request.billed_to)
    documents = [passage_text(p.title, p.section_path, p.content, p.source) for p in request.passages]
    try:
        scores, tokens = await rerank.rerank(request.query, documents)
    except PROVIDER_ERRORS as error:
        provider_error(error)
    model = get_settings().reranker_model
    if tokens is None:
        # Voyage counts the query once per document.
        guess = sum(spend.estimate_tokens(request.query) + spend.estimate_tokens(d) for d in documents)
        charge = priced("rerank", model, guess, estimated=True)
    else:
        charge = priced("rerank", model, tokens)
    await billing.charge(request.billed_to, correlation_of(http), charge)
    return RerankResponse(scores=scores, model_version=model, usage_tokens=tokens)


PDF_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            mime: {"schema": {"type": "string", "format": "binary"}} for mime in ["application/pdf", *FORMATS]
        },
    }
}


@app.post("/v1/ingest", response_model=IngestResponse, responses=ERRORS, openapi_extra=PDF_BODY)
async def ingest_pdf(
    request: Request,
    title: str = Query(min_length=1, max_length=300),
    billed_user: str = Query(min_length=1, max_length=200),
    billed_org: str = Query(min_length=1, max_length=200),
    billed_role: spend.Role = Query(),
    _caller: Claims = Depends(SERVICE),
) -> IngestResponse:
    """One supported document (the raw body) → chunks with embeddings and the figure crops they point at. Stateless: the caller
    stores the result. A document problem is a `failed` outcome; a provider outage is an error to retry. The
    model calls are charged to `billed_user` (the teacher who uploaded the document), whatever the outcome."""
    billed = spend.Billed(user_id=billed_user, org_id=billed_org, role=billed_role)
    return await budgeted(request, billed, lambda: ingest_reserved(request, title, billed))


async def ingest_reserved(request: Request, title: str, billed: spend.Billed) -> IngestResponse:
    mime = request.headers.get("content-type", "").split(";")[0].strip()
    if mime not in {"application/pdf", *FORMATS}:
        raise failure(415, "UNSUPPORTED_MEDIA_TYPE", "Send a PDF, Markdown, PPTX or DOCX document.")
    try:
        openai_client.get_client()  # without a key every model call would fail: refuse before any work
    except openai_client.NotConfiguredError as error:
        provider_error(error)
    billing = billing_of(request)
    await billing.check(billed)
    with tempfile.TemporaryDirectory(prefix="ingest-") as folder:
        path = Path(folder) / "document"
        await receive(request, path, 5 * 1024 * 1024 if mime == MARKDOWN else None)
        with costs.recording(correlation_of(request)) as record:
            # "error" until it ends otherwise: an unexpected error is a 500 for the caller to retry
            record.outcome = "error"
            try:
                result = await unless_disconnected(
                    request,
                    _ingest(request.app.state.ingest_slots, path, title, lambda: billing.check(billed), mime),
                )
                record.outcome = "indexed"
            except IngestError as e:
                record.outcome, record.failure_reason = "failed", e.reason
                return IngestResponse(outcome="failed", failure_reason=e.reason, failure_detail=str(e)[:2000])  # type: ignore[arg-type]
            except PROVIDER_ERRORS as error:
                provider_error(error)
            except HTTPException:
                record.outcome = "skipped"  # refused by the allowance check once it had a slot
                raise
            except CallerGoneError:
                record.outcome = "abandoned"
                log.info("ingest_abandoned")
                raise failure(
                    503, "CALLER_GONE", "The caller disconnected before the ingest finished."
                ) from None
            finally:
                costs.write(record, None)
                await billing.charge(billed, correlation_of(request), *ingest_charges(record))
    return indexed_response(result)


async def budgeted(http: Request, billed: spend.Billed, work: Callable[[], Awaitable[Any]]) -> Any:
    reservation = budget.Budget(billing_of(http).ledger, billed)
    try:
        await reservation.start()
    except HTTPException:
        raise
    except Exception:
        raise ledger_unavailable() from None
    token = budget.current.set(reservation)
    streamed = False

    async def close() -> None:
        try:
            await reservation.close()
        except Exception:
            log.error("reservation_release_failed")

    try:
        result = await work()
        if isinstance(result, ClosingStream):
            streamed = True
            finish = result.finish

            async def finish_budget() -> None:
                try:
                    await finish()
                finally:
                    await close()

            result.finish = finish_budget
        return result
    except asyncio.CancelledError:
        reservation.failed_accounting = True  # provider work may have been paid before cancellation
        raise
    finally:
        budget.current.reset(token)
        if not streamed:
            await asyncio.shield(close())


def indexed_response(result: IngestResult) -> IngestResponse:
    """The ingest result as the contract sends it: chunks with their embed text and vector, and figure crops."""
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


async def _ingest(
    slots: asyncio.Semaphore,
    path: Path,
    title: str,
    check: Callable[[], Awaitable[None]],
    mime: str = "application/pdf",
) -> IngestResult:
    """The ingest, once one of the slots is free. The allowance is checked again then: the check before the wait
    may be long out of date, as other uploads by the same user ran (and were charged) in the meantime."""
    with costs.stage("queued"):
        await slots.acquire()
    try:
        await check()
        with costs.stage("total"):
            return await ingest(path, title, vision_config(get_settings()), mime)
    finally:
        slots.release()


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


async def receive(request: Request, path: Path, max_bytes: int | None = None) -> None:
    """Stream the body to disk, refusing more than MAX_PDF_BYTES without reading it all into memory."""
    size = 0
    with path.open("wb") as out:
        async for part in request.stream():
            size += len(part)
            if size > min(MAX_PDF_BYTES, max_bytes if max_bytes is not None else MAX_PDF_BYTES):
                raise failure(413, "PAYLOAD_TOO_LARGE", "The document exceeds its upload size limit.")
            await asyncio.to_thread(out.write, part)
    if size == 0:
        raise failure(422, "VALIDATION_ERROR", "The document is empty.")
