"""reranking through voyage ai's hosted rerank api."""

import logging
import time

import httpx2
from fastapi import HTTPException

import budget
from settings import get_settings

log = logging.getLogger("ai-core.rerank")

RERANK_TIMEOUT = 2.0  # one attempt
# both attempts together: content waits 4 s for the whole rerank. a retry starts only with MIN_RETRY left.
RERANK_BUDGET, MIN_RETRY = 3.0, 0.5

_now = time.monotonic  # tests patch this

_client: httpx2.AsyncClient | None = None


class RerankNotConfiguredError(Exception):
    """VOYAGE_API_KEY is unset."""


class RerankError(Exception):
    """voyage timed out, answered with an error, or sent a body without a score for every document."""


def get_client() -> httpx2.AsyncClient:
    global _client
    if _client is None:
        settings = get_settings()
        if settings.voyage_api_key is None:
            raise RerankNotConfiguredError("VOYAGE_API_KEY is not set")
        _client = httpx2.AsyncClient(
            base_url=settings.voyage_base_url,
            timeout=RERANK_TIMEOUT,
            headers={"Authorization": f"Bearer {settings.voyage_api_key}"},
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def rerank(query: str, documents: list[str]) -> tuple[list[float], int | None]:
    """the relevance score of each document, in the documents' order, and the tokens voyage counted."""
    client, model = get_client(), get_settings().reranker_model
    deadline = _now() + RERANK_BUDGET

    async def attempt() -> httpx2.Response:
        resp: httpx2.Response = await budget.guarded(
            lambda: client.post(
                "/rerank",
                json={"query": query, "documents": documents, "model": model, "truncation": True},
                timeout=min(RERANK_TIMEOUT, max(deadline - _now(), 0.1)),
            ),
            model,
            [query + d for d in documents],
        )
        return resp

    try:
        try:
            resp = await attempt()
            if resp.status_code == 429 or resp.status_code >= 500:  # rate-limited or a server error
                resp.raise_for_status()
        except (httpx2.TransportError, httpx2.HTTPStatusError) as first:
            # one retry, inside the same budget.
            if deadline - _now() < MIN_RETRY:
                raise
            log.warning("rerank_retrying", extra={"error": type(first).__name__})
            resp = await attempt()
        resp.raise_for_status()
        body = resp.json()
        scores: list[float | None] = [None] * len(documents)
        for item in body["data"]:
            scores[int(item["index"])] = float(item["relevance_score"])
    except httpx2.TimeoutException:
        raise RerankError(f"timed out after {RERANK_TIMEOUT:.1f}s") from None
    except httpx2.HTTPStatusError as e:
        raise RerankError(f"HTTP {e.response.status_code}") from None
    except HTTPException:
        raise
    except Exception as e:  # unreachable, or a body that isn't voyage's shape
        raise RerankError(f"{type(e).__name__}") from None
    missing = scores.count(None)
    if missing:
        raise RerankError(f"{missing} of {len(documents)} documents have no score")
    usage = body.get("usage")  # voyage may send null: the caller then estimates the tokens
    tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    log.info("rerank", extra={"model": model, "documents": len(documents), "tokens": tokens})
    return [s for s in scores if s is not None], tokens if isinstance(tokens, int) else None
