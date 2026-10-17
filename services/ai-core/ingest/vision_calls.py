"""One vision model call: a JSON answer about an image, cached on disk and rate limited.

Results are cached on disk by (page content, model, prompt version): a reindex is free and gives the same
answer, and so does an unchanged page in another file (a revised deck). Calls run a few at
a time per event loop, and back off on the per-model rate limit longer than the shared client would."""

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import openai

import openai_client
from ingest import costs
from ingest.prompts import PROMPT_VERSION

log = logging.getLogger("ai-core.ingest.vision_calls")

CONCURRENCY = 4  # vision calls at once, across all ingests in the process


RATE_RETRIES = 8  # attempts at a rate-limited call: a batch of pages hits the per-model TPM limit


# the longest Retry-After waited for: a longer one fails the call, which only costs the page its tables or figures
MAX_WAIT = 60.0


TIMEOUT = 120.0


FIGURE_CONCURRENCY = 2  # description calls at once (FIGURE_MODEL may be the chat model: keep it gentle)


KEY_CHARS = 20  # of a page key, in cache file names


_slots: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Semaphore] = {}


def _semaphore(name: str = "page", size: int = CONCURRENCY) -> asyncio.Semaphore:
    """One limit per event loop: the service has one, but scripts and tests run several in turn."""
    loop = asyncio.get_running_loop()
    if (loop, name) not in _slots:
        for old in [k for k in _slots if k[0].is_closed()]:
            del _slots[old]
        _slots[(loop, name)] = asyncio.Semaphore(size)
    return _slots[(loop, name)]


def cache_file(cache_dir: Path, model: str, key: str, kind: str = "page") -> Path:
    """`key` is the page's `page_key`; `kind` names the call ("page", "ocr-v1", "f0-d3", ...)."""
    return cache_dir / model / f"pg-{key[:KEY_CHARS]}-{kind}-v{PROMPT_VERSION}.json"


async def cached(path: Path, make: Callable[[], Awaitable[dict]], kind: str) -> dict:
    """The cached answer, or a new one, saved atomically (a crash mid-write can't leave a broken entry). `kind`
    names the call in the ingest's cost record (ingest.costs); the cache folder is the model's name."""
    if (answer := await asyncio.to_thread(_read, path)) is not None:
        costs.hit(kind, path.parent.name)
        return answer
    with costs.calling(kind):
        try:
            result = await make()
        except Exception:
            costs.failed()
            raise
    await asyncio.to_thread(_write, path, result)
    return result


def _read(path: Path) -> dict | None:
    """The cached answer; None when there is none, or it is unreadable (it is then asked for again)."""
    try:
        answer = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:
        log.warning("unreadable cache entry %s: asking again", path.name)
        return None
    return answer if isinstance(answer, dict) else None


def _write(path: Path, answer: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(answer), encoding="utf-8")
    os.replace(tmp, path)


async def ask(
    prompt: str,
    image: bytes | Callable[[], Awaitable[bytes]],
    model: str,
    slots: asyncio.Semaphore | None = None,
) -> dict:
    """One JSON-mode vision call, retried by openai_client.call with this module's longer policy (_retry_in): a 429
    up to RATE_RETRIES attempts, other retryable errors up to openai_client.MAX_ATTEMPTS, after OpenAI's Retry-After
    when it sends one (a wait longer than MAX_WAIT fails the call). Each attempt takes a slot and the wait happens
    outside it, so other pages' calls go on meanwhile. `image` is the PNG, or an async function making it (called
    once a slot is first free, so only the pages being sent, or waiting to be sent again, are held in memory). An answer that isn't a JSON object
    raises, so it is never cached."""
    client = openai_client.get_client()
    extra = openai_client.deterministic_options(model)
    slots = slots or _semaphore()
    png = image if isinstance(image, bytes) else None
    started = time.monotonic()

    async def attempt() -> Any:
        nonlocal png
        async with slots:
            if png is None:
                assert not isinstance(image, bytes)
                png = await image()
            return await client.chat.completions.create(
                model=model,
                messages=_messages(prompt, png),
                response_format={"type": "json_object"},
                timeout=TIMEOUT,
                **extra,
            )

    attempts = openai_client.Attempts()
    try:
        resp = await openai_client.call(attempt, delay=_retry_in, attempts=attempts)
    except Exception:
        costs.retried(attempts.count - 1)
        raise
    costs.answered(model, resp.usage, time.monotonic() - started, attempts.count - 1)
    out = json.loads(resp.choices[0].message.content or "")
    if not isinstance(out, dict):
        raise ValueError(f"the model answered {type(out).__name__}, not a JSON object")
    return out


def _messages(prompt: str, png: bytes) -> Any:
    url = f"data:image/png;base64,{base64.b64encode(png).decode()}"
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
            ],
        }
    ]


def _retry_in(error: openai.OpenAIError, attempt: int) -> float | None:
    """Seconds to wait before attempt `attempt + 1`, or None to give up."""
    if not openai_client.retryable(error):
        return None
    limited = isinstance(error, openai.RateLimitError)
    if attempt >= (RATE_RETRIES if limited else openai_client.MAX_ATTEMPTS):
        return None
    after = openai_client.retry_after(error)
    if after is not None:
        return after if after <= MAX_WAIT else None
    # a batch of pages hits the per-model TPM limit: back off further each time
    return 2.0 + (attempt - 1) * 3 if limited else openai_client.backoff(attempt)
