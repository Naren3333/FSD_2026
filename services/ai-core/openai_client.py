"""the one AsyncOpenAI client per process, with our own retries: embeddings, chat (plain, streamed or structured) and the ingest's vision calls."""

import asyncio
import functools
import logging
import math
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

import openai
from openai import AsyncOpenAI

import budget
from settings import get_settings

log = logging.getLogger("ai-core.openai_client")

TIMEOUT = 30.0  # per read: embeddings and a stream's chunks; also the shortest generation budget
# scale generation timeouts with output size within the configured bounds.
GENERATE_BASE, GENERATE_RATE, GENERATE_CAP = 20.0, 60.0, 150.0
MIN_ATTEMPT = 10.0  # a generation retry starts only with at least this many seconds of its budget left
STREAM_OPEN_BUDGET = 45.0  # seconds by which a stream has opened or failed, retries included
EMBED_BATCH = 128  # inputs per request; openai also caps a request at 300k tokens in total

MAX_ATTEMPTS = 3  # first try + 2 retries
TIMEOUT_ATTEMPTS = 2  # a timed-out attempt is retried once, and only within the call's deadline
MAX_DELAY = 10.0  # a longer retry-after fails at once instead of spending the caller's time
BASE_DELAY, BACKOFF_CAP = 0.5, 8.0

# max_completion_tokens counts reasoning tokens too: each effort gets this multiple of the reply's own
# allowance, up to the contract's largest reply (adr 0023). an unknown effort gets the largest multiple.
REASONING_ALLOWANCE = {"none": 1.0, "minimal": 1.25, "low": 1.5, "medium": 2.5, "high": 4.0}
MAX_COMPLETION = 8000

_sleep = asyncio.sleep  # tests patch this
_now = time.monotonic  # and this
_client: AsyncOpenAI | None = None


class NotConfiguredError(Exception):
    """OPENAI_API_KEY is unset."""


class StructuredOutputError(Exception):
    """a refusal, or a cut-off or filtered reply, in json-schema mode."""

    def __init__(self, message: str, usage: "Usage | None" = None, text: str = "") -> None:
        super().__init__(message)
        self.usage, self.text = usage, text


@dataclass(frozen=True)
class Usage:
    """openai's token counts for one call; none when the stream ended without its usage chunk. `cached_in` is the part of `tokens_in` served from openai's prompt cache, which is billed at the cached rate."""

    tokens_in: int | None
    tokens_out: int | None
    cached_in: int = 0


def usage_of(usage: Any) -> Usage:
    """the token counts of an sdk response's (or stream chunk's) usage object, including cached prompt tokens; usage(none, none) when there is none."""
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
class Attempts:
    """how many times call has tried its function so far: attempts beyond the first are retries. `timeouts` counts the attempts that timed out, each of which openai may still finish and bill."""

    count: int = 0
    timeouts: int = 0


@dataclass
class Tally:
    """the prompt tokens of the embedding batches answered so far: what the caller owes even when a later batch fails."""

    tokens: int = 0


async def embed(texts: list[str], tally: Tally | None = None) -> tuple[list[list[float]], int]:
    """vectors in input order, and the prompt tokens they took."""
    if not texts:
        return [], 0
    client, model = get_client(), get_settings().embedding_model
    vectors: list[list[float]] = []
    tokens = 0
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start : start + EMBED_BATCH]
        # encoding_format left unset: the sdk then requests base64 and decodes it to floats itself.
        create = functools.partial(client.embeddings.create, model=model, input=batch, timeout=TIMEOUT)
        result = await call(functools.partial(budget.guarded, create, model, batch))
        # place by openai's index rather than trusting the order of `data`.
        placed: list[list[float]] = [[] for _ in batch]
        for item in result.data:
            placed[item.index] = item.embedding
        vectors += placed
        tokens += result.usage.prompt_tokens
        if tally is not None:
            tally.tokens += result.usage.prompt_tokens
    log.info(
        "embed",
        extra={
            "model": model,
            "inputs": len(texts),
            "batches": -(-len(texts) // EMBED_BATCH),
            "tokens": tokens,
        },
    )
    return vectors, tokens


@dataclass(frozen=True)
class Route:
    """the model, reasoning effort and max_completion_tokens one reply is sent with."""

    model: str
    effort: str
    max_completion: int


def completion_tokens(max_tokens: int, effort: str) -> int:
    """the max_completion_tokens a reply of up to `max_tokens` visible tokens is sent with at `effort`."""
    factor = REASONING_ALLOWANCE.get(effort, max(REASONING_ALLOWANCE.values()))
    return max(max_tokens, min(math.ceil(max_tokens * factor), MAX_COMPLETION))


def route(profile: str, max_tokens: int, effort: str | None = None) -> Route:
    """a non-streamed reply's model, effort and token allowance on `profile` (settings.profile), at `effort` instead of the profile's when given."""
    model, own = get_settings().profile(profile)
    effort = effort or own
    return Route(model, effort, completion_tokens(max_tokens, effort))


def stream_route(max_tokens: int, effort: str | None = None) -> Route:
    """streamed chat: the chat model at CHAT_REASONING_EFFORT, or at `effort` when given."""
    settings = get_settings()
    effort = effort or settings.chat_reasoning_effort
    return Route(settings.chat_model, effort, completion_tokens(max_tokens, effort))


async def stream_chat(
    messages: list[Any], max_tokens: int, attempts: Attempts | None = None, effort: str | None = None
) -> AsyncGenerator[str | Usage, None]:
    """text deltas in order, then one usage; `attempts` counts the tries at opening the stream."""
    client, sent = get_client(), stream_route(max_tokens, effort)
    stream = await call(
        lambda: budget.guarded(
            lambda: client.chat.completions.create(
                model=sent.model,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
                max_completion_tokens=sent.max_completion,
                reasoning_effort=cast(Any, sent.effort),
                timeout=TIMEOUT,
            ),
            sent.model,
            messages,
            sent.max_completion,
        ),
        deadline=_now() + STREAM_OPEN_BUDGET,
        min_attempt=TIMEOUT,
        attempts=attempts,
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
        log.warning("stream_chat_without_usage", extra={"model": sent.model})
        yield Usage(None, None)
        return
    counts = usage_of(usage)
    log.info(
        "stream_chat",
        extra={
            "model": sent.model,
            "tokens_in": counts.tokens_in,
            "cached_in": counts.cached_in,
            "tokens_out": counts.tokens_out,
            "finish_reason": finish_reason,
        },
    )
    yield counts


def generation_budget(max_tokens: int) -> float:
    """seconds a non-streamed reply of up to `max_tokens` may take in all, retries included."""
    return min(max(GENERATE_BASE + max_tokens / GENERATE_RATE, TIMEOUT), GENERATE_CAP)


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage
    finish_reason: str | None


async def generate(  # noqa: PLR0913 - one reply's options
    messages: list[Any],
    max_tokens: int,
    schema: dict | None = None,
    *,
    name: str = "",
    timeout: float | None = None,
    profile: str = "default",
    effort: str | None = None,
    attempts: Attempts | None = None,
) -> Completion:
    """one reply on `profile` (route), at `effort` instead of the profile's when given, given up after `timeout` seconds when that is shorter than its own budget; `attempts` counts the tries."""
    client, sent = get_client(), route(profile, max_tokens, effort)
    extra: dict[str, Any] = {}
    if schema is not None:
        extra["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": (name or "reply").replace(".", "_"), "strict": True, "schema": schema},
        }
    # reasoning tokens take time as well as allowance.
    budget_seconds = generation_budget(sent.max_completion)
    deadline = _now() + (budget_seconds if timeout is None else min(timeout, budget_seconds))
    result = await call(
        lambda: budget.guarded(
            lambda: client.chat.completions.create(
                model=sent.model,
                messages=messages,
                max_completion_tokens=sent.max_completion,
                reasoning_effort=cast(Any, sent.effort),
                timeout=max(deadline - _now(), 1.0),
                **extra,
            ),
            sent.model,
            messages,
            sent.max_completion,
        ),
        deadline=deadline,
        attempts=attempts,
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
        "generate",
        extra={
            "schema_name": name or None,
            "model": sent.model,
            "reasoning_effort": sent.effort,
            "tokens_in": usage.tokens_in,
            "cached_in": usage.cached_in,
            "tokens_out": usage.tokens_out,
            "finish_reason": choice.finish_reason,
        },
    )
    return Completion(choice.message.content or "", usage, choice.finish_reason)


async def call[T](
    fn: Callable[[], Awaitable[T]],
    *,
    delay: Callable[[openai.OpenAIError, int], float | None] | None = None,
    attempts: Attempts | None = None,
    deadline: float | None = None,
    min_attempt: float = MIN_ATTEMPT,
) -> T:
    """retry a provider call using the supplied delay policy; stop when it returns no delay."""
    wait = delay or default_delay
    if attempts is None:
        attempts = Attempts()
    while True:
        attempts.count += 1
        try:
            return await fn()
        except openai.OpenAIError as exc:
            if isinstance(exc, openai.APITimeoutError):
                attempts.timeouts += 1
            seconds = wait(exc, attempts.count)
            # a timeout is retried only against a deadline, so the retry never outlasts the caller's wait.
            untimed = isinstance(exc, openai.APITimeoutError) and deadline is None
            if (
                seconds is None
                or untimed
                or (deadline is not None and deadline - _now() - seconds < min_attempt)
            ):
                raise
            log.warning(
                "openai_call_retrying",
                extra={"attempt": attempts.count, "retry_in_seconds": round(seconds, 2), "error": str(exc)},
            )
            await _sleep(seconds)


def default_delay(exc: openai.OpenAIError, attempt: int) -> float | None:
    """this module's policy: MAX_ATTEMPTS attempts at retryable errors, after openai's retry-after when it sends one (up to MAX_DELAY) and otherwise exponential backoff with full jitter. a first attempt that timed out gets one retry, which call() starts only when its deadline leaves room."""
    if isinstance(exc, openai.APITimeoutError):
        return backoff(attempt) if attempt < TIMEOUT_ATTEMPTS else None
    if attempt >= MAX_ATTEMPTS or not retryable(exc):
        return None
    after = retry_after(exc)
    if after is None:
        return backoff(attempt)
    return after if after <= MAX_DELAY else None


def deterministic_options(model: str) -> dict[str, Any]:
    """request options for repeatable answers: low reasoning effort for reasoning models, otherwise temperature 0 (reasoning models reject a temperature)."""
    if model.startswith(("gpt-5", "gpt-6", "o")):
        return {"reasoning_effort": "low"}
    return {"temperature": 0}


def retryable(exc: openai.OpenAIError) -> bool:
    """errors worth retrying whatever the time left; a timeout is not one (default_delay retries it once, against a deadline)."""
    match exc:
        case openai.APITimeoutError():  # subclass of apiconnectionerror, so it must come first
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
    """openai's retry-after, in seconds, when the error carries one."""
    if isinstance(exc, openai.APIStatusError):
        headers = exc.response.headers
        try:
            if (ms := headers.get("retry-after-ms")) is not None:
                return float(ms) / 1000
            if (s := headers.get("retry-after")) is not None:
                return float(s)
        except ValueError:  # e.g. the http-date form, which openai doesn't send
            pass
    return None


def backoff(attempt: int) -> float:
    """exponential backoff with full jitter."""
    return random.uniform(0, min(BACKOFF_CAP, BASE_DELAY * 2 ** (attempt - 1)))
