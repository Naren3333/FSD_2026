"""Calls to the other services. Identity and Content get the user's own token, so their access rules decide what
the chat may see; ai-core gets this agent's service-account token (role ai-client), never the user's. The transport
is learning_transport.agents."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from learning_transport import STUDENT, Claims, UpstreamError, read_events
from learning_transport.agents import AgentClients, billed_to

from contracts import Usage

SEARCH_TOP_K = 8
SEARCH_TIMEOUT = 15.0
# Above ai-core's worst cases, retries included (openai_client): 30 s for the 300-token query rewrite; and for the
# stream, which is read with a per-read timeout, 45 s to open plus 30 s for a chunk.
GENERATE_TIMEOUT = 40.0
STREAM_TIMEOUT = httpx.Timeout(90, connect=5)
USAGE_TIMEOUT = 5.0


def billed(claims: Claims, relation: str | None = None) -> dict[str, str]:
    """Whose daily AI allowance pays for a call: the signed-in user, at the allowance of their relation to the
    turn's classroom (teacher or student), or of their token's role when no classroom is involved."""
    return billed_to(claims, relation or claims.person_role or STUDENT)


@dataclass(frozen=True)
class Done:
    """The end of a streamed reply."""

    model: str | None
    usage_tokens: int | None


class Services(AgentClients):
    generate_timeout = GENERATE_TIMEOUT

    # ---- user-token calls

    async def search(
        self, authorization: str, classroom_id: str, query: str, correlation: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """The top results, reranked, and whether the rerank fell back (then `score` is an RRF score, not
        comparable to the relevance floor)."""
        body = {"classroom_id": classroom_id, "query": query, "top_k": SEARCH_TOP_K, "rerank": True}
        return await self.search_content(authorization, body, correlation, SEARCH_TIMEOUT)

    # ---- ai-core, as this agent

    async def stream(
        self, messages: list[dict[str, str]], max_tokens: int, correlation: str, payer: dict[str, str]
    ) -> AsyncIterator[str | Done]:
        """The reply's text as it arrives, then Done, billed to `payer` (billed()). A failure before the first word
        or mid-stream raises."""
        body = {
            "operation": "generate",
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
            "billed_to": payer,
        }
        headers = await self.ai_core_headers(correlation)
        try:
            async with self.http.stream(
                "POST",
                f"{self.settings.ai_core_url}/v1/inference",
                json=body,
                headers=headers,
                timeout=STREAM_TIMEOUT,
            ) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise self.ai_core_refused(resp)
                async for event, data in read_events(resp.aiter_lines()):
                    if event == "delta":
                        yield data["text"]
                    elif event == "done":
                        yield Done(data.get("model_version"), data.get("usage_tokens"))
                        return
                    elif event == "error":
                        raise UpstreamError("ai-core", 502, data.get("code", "AI_PROVIDER_FAILED"))
        except httpx.HTTPError:
            raise UpstreamError("ai-core", 0) from None
        raise UpstreamError("ai-core", 502, "STREAM_ENDED_EARLY")

    async def usage(self, payer: dict[str, str], correlation: str) -> Usage:
        """The user's AI spend today, their daily limit and when it resets, from ai-core's ledger. A reply without
        them is a 502 UpstreamError."""
        resp = await self.ai_core("GET", "/v1/usage", correlation, USAGE_TIMEOUT, params=payer)
        try:
            data = resp.json()
            return Usage.model_validate({k: data[k] for k in ("spent_usd", "limit_usd", "resets_at")})
        except (ValueError, KeyError, TypeError):  # ValidationError is a ValueError
            raise UpstreamError("ai-core", 502) from None
