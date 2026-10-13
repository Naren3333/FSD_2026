"""The one AsyncOpenAI client per process, with our own retries: embeddings, chat (plain, streamed or
structured) and the ingest's vision calls. Ported from fsd_test's shared/openai_client.py.

SDK retries are off (max_retries=0) so the loop here counts attempts. Retried: 429, 5xx and connection
errors, twice, with full-jitter backoff; Retry-After is honoured up to 10s and anything longer fails at
once. Not retried: timeouts (a retry would outlast the caller, and the call may still be billed) and 429
insufficient_quota (billing, not load). Errors are raised as the SDK's own exceptions. A stream is retried
only around create(), before the first token: a retry after it would repeat text the caller already relayed.
"""

import asyncio
import functools
import logging
import random
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

import openai
from openai import AsyncOpenAI

from settings import get_settings

log = logging.getLogger("ai-core.openai_client")

TIMEOUT = 30.0  # per call; httpx applies it per read
EMBED_BATCH = 128  # inputs per request; OpenAI also caps a request at 300k tokens in total

MAX_ATTEMPTS = 3  # first try + 2 retries
MAX_DELAY = 10.0  # a longer Retry-After fails at once instead of spending the caller's time
BASE_DELAY, BACKOFF_CAP = 0.5, 8.0

_sleep = asyncio.sleep  # tests patch this
_client: AsyncOpenAI | None = None


class NotConfiguredError(Exception):
    """OPENAI_API_KEY is unset. The process still starts; only the calls fail."""


class StructuredOutputError(Exception):
    """A refusal, or a cut-off or filtered reply, in JSON-schema mode. Not an OpenAIError, so never retried."""


@dataclass(frozen=True)
class Usage:
    """OpenAI's token counts for one call; None when the stream ended without its usage chunk."""

    tokens_in: int | None
    tokens_out: int | None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        key = get_settings().openai_api_key
        if key is None:
            raise NotConfiguredError("OPENAI_API_KEY is not set")
        _client = AsyncOpenAI(api_key=key, max_retries=0, timeout=TIMEOUT)
    return _client


async def embed(texts: list[str]) -> tuple[list[list[float]], int]:
    """Vectors in input order, and the prompt tokens they took. Batches of EMBED_BATCH are sent one after
    another, each retried on its own."""
    if not texts:
        return [], 0
    client, model = get_client(), get_settings().embedding_model
    vectors: list[list[float]] = []
    tokens = 0
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start : start + EMBED_BATCH]
        # encoding_format left unset: the SDK then requests base64 and decodes it to floats itself.
        create = functools.partial(client.embeddings.create, model=model, input=batch, timeout=TIMEOUT)
        result = await call(create)
        # Place by OpenAI's index rather than trusting the order of `data`.
        placed: list[list[float]] = [[] for _ in batch]
        for item in result.data:
            placed[item.index] = item.embedding
        vectors += placed
        tokens += result.usage.prompt_tokens
    log.info(
        "embed: model=%s inputs=%d batches=%d tokens=%d",
        model,
        len(texts),
        -(-len(texts) // EMBED_BATCH),
        tokens,
    )
    return vectors, tokens


async def stream_chat(messages: list[Any], max_tokens: int) -> AsyncGenerator[str | Usage, None]:
    """Text deltas in order, then one Usage. Breaking out early closes the HTTP stream."""
    client, settings = get_client(), get_settings()
    stream = await call(
        lambda: client.chat.completions.create(
            model=settings.chat_model,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            max_completion_tokens=max_tokens,
            reasoning_effort=cast(Any, settings.chat_reasoning_effort),
            timeout=TIMEOUT,
        )
    )
    usage, finish_reason = None, None
    try:
        async for chunk in stream:
            if not chunk.choices:  # the usage chunk, after the last delta
                usage = chunk.usage or usage
                continue
            choice = chunk.choices[0]
            finish_reason = choice.finish_reason or finish_reason
            if choice.delta.content:
                yield choice.delta.content
    finally:
        await stream.close()
    if usage is None:
        log.warning("stream_chat: model=%s ended without a usage chunk", settings.chat_model)
        yield Usage(None, None)
        return
    log.info(
        "stream_chat: model=%s tokens_in=%d tokens_out=%d finish_reason=%s",
        settings.chat_model,
        usage.prompt_tokens,
        usage.completion_tokens,
        finish_reason,
    )
    yield Usage(usage.prompt_tokens, usage.completion_tokens)


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage
    finish_reason: str | None


async def generate(
    messages: list[Any], max_tokens: int, schema: dict | None = None, *, name: str = ""
) -> Completion:
    """One reply. With `schema`, OpenAI's strict JSON-schema mode: a refusal or a cut-off or filtered reply then
    raises StructuredOutputError, and the caller re-validates the JSON. `name` is the prompt version, e.g.
    "query_rewrite.v1" (OpenAI rejects dots in schema names, so they become underscores)."""
    client, settings = get_client(), get_settings()
    extra: dict[str, Any] = {}
    if schema is not None:
        extra["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": (name or "reply").replace(".", "_"), "strict": True, "schema": schema},
        }
    result = await call(
        lambda: client.chat.completions.create(
            model=settings.chat_model,
            messages=messages,
            max_completion_tokens=max_tokens,
            reasoning_effort=cast(Any, settings.chat_reasoning_effort),
            timeout=TIMEOUT,
            **extra,
        )
    )
    choice = result.choices[0]
    if schema is not None:
        if choice.message.refusal:
            raise StructuredOutputError(f"{name}: refused")
        if choice.finish_reason in ("length", "content_filter"):
            raise StructuredOutputError(f"{name}: finish_reason={choice.finish_reason}")
    usage = (
        Usage(result.usage.prompt_tokens, result.usage.completion_tokens)
        if result.usage
        else Usage(None, None)
    )
    log.info(
        "generate: %s model=%s tokens_in=%s tokens_out=%s finish_reason=%s",
        name or "-",
        settings.chat_model,
        usage.tokens_in,
        usage.tokens_out,
        choice.finish_reason,
    )
    return Completion(choice.message.content or "", usage, choice.finish_reason)


async def call[T](fn: Callable[[], Awaitable[T]]) -> T:
    """Run one SDK call with this module's retries (see the module docstring)."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return await fn()
        except openai.OpenAIError as exc:
            if attempt == MAX_ATTEMPTS or not _retryable(exc):
                raise
            delay = _delay(exc, attempt)
            if delay > MAX_DELAY:
                raise
            log.warning(
                "OpenAI call failed (attempt %d/%d), retrying in %.2fs: %s", attempt, MAX_ATTEMPTS, delay, exc
            )
            await _sleep(delay)
    raise AssertionError("unreachable")


def deterministic_options(model: str) -> dict[str, Any]:
    """Request options for repeatable answers: low reasoning effort for reasoning models, otherwise temperature 0
    (reasoning models reject a temperature)."""
    if model.startswith(("gpt-5", "gpt-6", "o")):
        return {"reasoning_effort": "low"}
    return {"temperature": 0}


def _retryable(exc: openai.OpenAIError) -> bool:
    match exc:
        case openai.APITimeoutError():  # subclass of APIConnectionError, so it must come first
            return False
        case openai.APIConnectionError():
            return True
        case openai.RateLimitError() if exc.code == "insufficient_quota":
            return False
        case openai.APIStatusError():
            return exc.status_code == 429 or exc.status_code >= 500
        case _:
            return False


def _delay(exc: openai.OpenAIError, attempt: int) -> float:
    """OpenAI's Retry-After when given, otherwise exponential backoff with full jitter."""
    if isinstance(exc, openai.APIStatusError):
        headers = exc.response.headers
        try:
            if (ms := headers.get("retry-after-ms")) is not None:
                return float(ms) / 1000
            if (s := headers.get("retry-after")) is not None:
                return float(s)
        except ValueError:  # e.g. the HTTP-date form, which OpenAI doesn't send
            pass
    return random.uniform(0, min(BACKOFF_CAP, BASE_DELAY * 2 ** (attempt - 1)))
