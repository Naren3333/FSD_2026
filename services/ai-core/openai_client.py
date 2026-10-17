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
    """A refusal, or a cut-off or filtered reply, in JSON-schema mode. Not an OpenAIError, so never retried. OpenAI
    still bills the reply, so it carries the reply's `usage` and `text` for the caller to charge."""

    def __init__(self, message: str, usage: "Usage | None" = None, text: str = "") -> None:
        super().__init__(message)
        self.usage, self.text = usage, text


@dataclass(frozen=True)
class Usage:
    """OpenAI's token counts for one call; None when the stream ended without its usage chunk. `cached_in` is the
    part of `tokens_in` served from OpenAI's prompt cache, which is billed at the cached rate."""

    tokens_in: int | None
    tokens_out: int | None
    cached_in: int = 0


def usage_of(usage: Any) -> Usage:
    """The token counts of an SDK response's (or stream chunk's) usage object, including cached prompt tokens;
    Usage(None, None) when there is none."""
    if usage is None:
        return Usage(None, None)
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) or 0
    return Usage(usage.prompt_tokens, usage.completion_tokens, cached)


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        key = get_settings().openai_api_key
        if key is None:
            raise NotConfiguredError("OPENAI_API_KEY is not set")
        _client = AsyncOpenAI(api_key=key, max_retries=0, timeout=TIMEOUT)
    return _client


@dataclass
class Tally:
    """The prompt tokens of the embedding batches answered so far: what the caller owes even when a later batch
    fails."""

    tokens: int = 0


async def embed(texts: list[str], tally: Tally | None = None) -> tuple[list[list[float]], int]:
    """Vectors in input order, and the prompt tokens they took. Batches of EMBED_BATCH are sent one after
    another, each retried on its own; `tally` counts the tokens of each batch as it is answered."""
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
        if tally is not None:
            tally.tokens += result.usage.prompt_tokens
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
    counts = usage_of(usage)
    log.info(
        "stream_chat: model=%s tokens_in=%d cached_in=%d tokens_out=%d finish_reason=%s",
        settings.chat_model,
        counts.tokens_in,
        counts.cached_in,
        counts.tokens_out,
        finish_reason,
    )
    yield counts


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
    usage = usage_of(result.usage)
    if schema is not None:
        text = choice.message.content or ""
        if choice.message.refusal:
            raise StructuredOutputError(f"{name}: refused", usage, choice.message.refusal)
        if choice.finish_reason in ("length", "content_filter"):
            raise StructuredOutputError(f"{name}: finish_reason={choice.finish_reason}", usage, text)
    log.info(
        "generate: %s model=%s tokens_in=%s cached_in=%s tokens_out=%s finish_reason=%s",
        name or "-",
        settings.chat_model,
        usage.tokens_in,
        usage.cached_in,
        usage.tokens_out,
        choice.finish_reason,
    )
    return Completion(choice.message.content or "", usage, choice.finish_reason)


@dataclass
class Attempts:
    """How many times call has tried its function so far: attempts beyond the first are retries."""

    count: int = 0


async def call[T](
    fn: Callable[[], Awaitable[T]],
    *,
    delay: Callable[[openai.OpenAIError, int], float | None] | None = None,
    attempts: Attempts | None = None,
) -> T:
    """Run one SDK call with retries. `delay` gives the seconds to wait after a failed attempt (numbered from 1)
    before the next, or None to raise the error; the default is this module's policy (see the module docstring).
    Errors that aren't OpenAI's are never retried. `attempts`, when given, counts the attempts made."""
    wait = delay or default_delay
    if attempts is None:
        attempts = Attempts()
    while True:
        attempts.count += 1
        try:
            return await fn()
        except openai.OpenAIError as exc:
            seconds = wait(exc, attempts.count)
            if seconds is None:
                raise
            log.warning(
                "OpenAI call failed (attempt %d), retrying in %.2fs: %s", attempts.count, seconds, exc
            )
            await _sleep(seconds)


def default_delay(exc: openai.OpenAIError, attempt: int) -> float | None:
    """This module's policy: MAX_ATTEMPTS attempts at retryable errors, after OpenAI's Retry-After when it sends
    one (up to MAX_DELAY) and otherwise exponential backoff with full jitter."""
    if attempt >= MAX_ATTEMPTS or not retryable(exc):
        return None
    after = retry_after(exc)
    if after is None:
        return backoff(attempt)
    return after if after <= MAX_DELAY else None


def deterministic_options(model: str) -> dict[str, Any]:
    """Request options for repeatable answers: low reasoning effort for reasoning models, otherwise temperature 0
    (reasoning models reject a temperature)."""
    if model.startswith(("gpt-5", "gpt-6", "o")):
        return {"reasoning_effort": "low"}
    return {"temperature": 0}


def retryable(exc: openai.OpenAIError) -> bool:
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


def retry_after(exc: openai.OpenAIError) -> float | None:
    """OpenAI's Retry-After, in seconds, when the error carries one."""
    if isinstance(exc, openai.APIStatusError):
        headers = exc.response.headers
        try:
            if (ms := headers.get("retry-after-ms")) is not None:
                return float(ms) / 1000
            if (s := headers.get("retry-after")) is not None:
                return float(s)
        except ValueError:  # e.g. the HTTP-date form, which OpenAI doesn't send
            pass
    return None


def backoff(attempt: int) -> float:
    """Exponential backoff with full jitter."""
    return random.uniform(0, min(BACKOFF_CAP, BASE_DELAY * 2 ** (attempt - 1)))
