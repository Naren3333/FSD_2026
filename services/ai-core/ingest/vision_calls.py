"""One vision model call: a JSON answer about an image, cached on disk and rate limited.

Results are cached on disk by (page content, model, prompt version): a reindex is free and gives the same
answer, and so does an unchanged page in another file (a revised deck). Calls run a few at
a time per event loop, and back off on the per-model rate limit beyond the shared client's own retries."""

import asyncio
import base64
import json
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

CONCURRENCY = 4  # vision calls at once, across all ingests in the process


RATE_RETRIES = 8  # a batch of pages hits the per-model TPM limit; the shared client's 2 retries aren't enough


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
    if path.exists():
        costs.hit(kind, path.parent.name)
        return json.loads(path.read_text(encoding="utf-8"))
    with costs.calling(kind):
        try:
            result = await make()
        except Exception:
            costs.failed()
            raise
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(result), encoding="utf-8")
    os.replace(tmp, path)
    return result


async def ask(
    prompt: str,
    image: bytes | Callable[[], Awaitable[bytes]],
    model: str,
    slots: asyncio.Semaphore | None = None,
) -> dict:
    """One JSON-mode vision call, with backoff on the per-model rate limit. `image` is the PNG, or an async
    function making it (called once a slot is free, so only the pages being sent are held in memory).
    An answer that isn't a JSON object raises, so it is never cached."""
    client = openai_client.get_client()
    extra = openai_client.deterministic_options(model)
    async with slots or _semaphore():
        png = image if isinstance(image, bytes) else await image()
        url = f"data:image/png;base64,{base64.b64encode(png).decode()}"
        messages: Any = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
                ],
            }
        ]
        attempts, started = 0, time.monotonic()

        def create() -> Any:
            nonlocal attempts
            attempts += 1  # the shared client's retries call this again
            return client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                timeout=TIMEOUT,
                **extra,
            )

        for attempt in range(RATE_RETRIES):
            try:
                resp = await openai_client.call(create)
                break
            except openai.RateLimitError as e:
                if e.code == "insufficient_quota" or attempt == RATE_RETRIES - 1:
                    costs.retried(attempts - 1)
                    raise
                await asyncio.sleep(2 + attempt * 3)
            except Exception:
                costs.retried(attempts - 1)
                raise
    costs.answered(model, resp.usage, time.monotonic() - started, attempts - 1)
    out = json.loads(resp.choices[0].message.content or "")
    if not isinstance(out, dict):
        raise ValueError(f"the model answered {type(out).__name__}, not a JSON object")
    return out
