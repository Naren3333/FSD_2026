"""Reranking through Voyage AI's hosted rerank API. A CPU cross-encoder took 3-9s for 30 passages in fsd_test, so
reranking is outsourced. Not retried: one attempt fits the 2s budget, and the caller (Content search) keeps its
RRF order when this fails. Failures are raised, never turned into made-up scores.
"""

import logging

import httpx2

from settings import get_settings

log = logging.getLogger("ai-core.rerank")

RERANK_TIMEOUT = 2.0

_client: httpx2.AsyncClient | None = None


class RerankNotConfiguredError(Exception):
    """VOYAGE_API_KEY is unset."""


class RerankError(Exception):
    """Voyage timed out, answered with an error, or sent a body without a score for every document."""


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
    """The relevance score of each document, in the documents' order, and the tokens Voyage counted."""
    client, model = get_client(), get_settings().reranker_model
    try:
        resp = await client.post(
            "/rerank", json={"query": query, "documents": documents, "model": model, "truncation": True}
        )
        resp.raise_for_status()
        body = resp.json()
        scores: list[float | None] = [None] * len(documents)
        for item in body["data"]:
            scores[int(item["index"])] = float(item["relevance_score"])
    except httpx2.TimeoutException:
        raise RerankError(f"timed out after {RERANK_TIMEOUT:.1f}s") from None
    except httpx2.HTTPStatusError as e:
        raise RerankError(f"HTTP {e.response.status_code}") from None
    except Exception as e:  # unreachable, or a body that isn't Voyage's shape
        raise RerankError(f"{type(e).__name__}") from None
    missing = scores.count(None)
    if missing:
        raise RerankError(f"{missing} of {len(documents)} documents have no score")
    usage = body.get("usage")  # Voyage may send null: the caller then estimates the tokens
    tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    log.info("rerank: model=%s documents=%d tokens=%s", model, len(documents), tokens)
    return [s for s in scores if s is not None], tokens if isinstance(tokens, int) else None
